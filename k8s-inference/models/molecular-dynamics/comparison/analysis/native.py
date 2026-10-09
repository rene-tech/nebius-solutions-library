"""Read native frames without inventing missing time/step metadata."""
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from geometry import ValidationError, finite, minimum_image, validate_cell


@dataclass
class Frame:
    index: int
    positions: np.ndarray
    cell: np.ndarray
    time_ps: float
    step: int | None
    time_source: str
    velocities: np.ndarray | None = None
    cell_origin: np.ndarray | None = None


def lammps_frames(path, timestep_ps):
    """Strict native dump reader: real units, periodic orthogonal/restricted cells.

    Atom IDs must be precisely 1..N, then are sorted into native topology order.
    Unwrapped/scaled coordinates are supported, but general triclinic abc output
    is explicitly unsupported. No permissive truncated-last-frame behavior.
    """
    with Path(path).open() as stream:
        index = 0

        def line():
            value = stream.readline()
            if not value:
                raise ValidationError("truncated LAMMPS frame")
            return value.strip()

        for header in stream:
            if header.strip() != "ITEM: TIMESTEP":
                raise ValidationError(f"unexpected LAMMPS record {header.strip()!r}")
            step = int(line())
            if line() != "ITEM: NUMBER OF ATOMS":
                raise ValidationError("LAMMPS NUMBER OF ATOMS missing")
            n = int(line())
            if n <= 0:
                raise ValidationError("empty LAMMPS frame")
            bounds_header = line().split()
            if bounds_header[:3] != ["ITEM:", "BOX", "BOUNDS"] or bounds_header[-3:] != ["pp"] * 3:
                raise ValidationError("LAMMPS dump requires periodic BOX BOUNDS")
            triclinic = bounds_header[3:6] == ["xy", "xz", "yz"]
            if bounds_header[3:-3] not in ([], ["xy", "xz", "yz"]):
                raise ValidationError("unsupported LAMMPS cell representation")
            bounds = finite([[float(x) for x in line().split()] for _ in range(3)], "bounds")
            if bounds.shape != (3, 3 if triclinic else 2):
                raise ValidationError("bad LAMMPS cell field count")
            lo, hi = bounds[:, 0].copy(), bounds[:, 1].copy()
            xy, xz, yz = bounds[:, 2] if triclinic else (0., 0., 0.)
            lo[0] -= min(0, xy, xz, xy + xz)
            hi[0] -= max(0, xy, xz, xy + xz)
            lo[1] -= min(0, yz)
            hi[1] -= max(0, yz)
            cell = validate_cell([[hi[0] - lo[0], 0, 0], [xy, hi[1] - lo[1], 0], [xz, yz, hi[2] - lo[2]]])
            atom_header = line().split()
            if atom_header[:2] != ["ITEM:", "ATOMS"]:
                raise ValidationError("LAMMPS ATOMS missing")
            columns = atom_header[2:]
            if len(set(columns)) != len(columns) or "id" not in columns:
                raise ValidationError("LAMMPS unique columns and atom IDs required")
            xyz = next((names for names in (("x", "y", "z"), ("xu", "yu", "zu"), ("xs", "ys", "zs"), ("xsu", "ysu", "zsu")) if set(names) <= set(columns)), None)
            if xyz is None:
                raise ValidationError("LAMMPS xyz coordinates missing")
            data = finite([[float(x) for x in line().split()] for _ in range(n)], "LAMMPS atom records")
            if data.shape != (n, len(columns)):
                raise ValidationError("LAMMPS atom field count differs")
            ids = data[:, columns.index("id")]
            order = np.argsort(ids)
            if not np.array_equal(ids[order], np.arange(1, n + 1)):
                raise ValidationError("LAMMPS atom IDs are not exactly 1..N")
            positions = data[order][:, [columns.index(c) for c in xyz]]
            if xyz[0] in ("xs", "xsu"):
                positions = positions @ cell + lo
            velocities = data[order][:, [columns.index(c) for c in ("vx", "vy", "vz")]] if {"vx", "vy", "vz"} <= set(columns) else None
            yield Frame(index, positions, cell, step * timestep_ps, step, "native dump step × declared protocol timestep", velocities, lo)
            index += 1


def lammps_segment_frames(paths, timestep_ps, boundary_receipts=None, boundary_policy=None):
    """Strict boundaries; an explicit exact-file SHAKE policy is a separate gate."""
    if not paths or len({str(Path(path).resolve()) for path in paths}) != len(paths):
        raise ValidationError("distinct ordered native trajectory segment paths required")
    previous_step, previous_positions, previous_cell, index = None, None, None, 0
    previous = None
    for segment, path in enumerate(paths):
        count = 0
        for frame in lammps_frames(path, timestep_ps):
            count += 1
            if previous_step is not None and frame.step == previous_step and frame.index == 0:
                if frame.positions.shape != previous_positions.shape or not np.allclose(frame.cell, previous_cell, atol=1e-7, rtol=0):
                    raise ValidationError("closed segment boundary has different positions or cell")
                raw_delta = frame.positions - previous_positions
                periodic_delta = minimum_image(raw_delta, previous_cell)
                projection = None
                if np.abs(periodic_delta).max() > 1e-7:
                    if boundary_policy is None:
                        raise ValidationError("closed segment boundary has different periodic atom positions")
                    from shake_boundary import verify_projection
                    projection = verify_projection(previous, frame, boundary_policy)
                if boundary_receipts is not None:
                    entry = {"step": frame.step, "next_segment_index": segment, "next_segment_file": str(Path(path).resolve()), "maximum_raw_position_difference_A": float(np.abs(raw_delta).max()), "maximum_periodic_position_difference_A": float(np.abs(periodic_delta).max()), "periodically_rewrapped_atoms": int(np.count_nonzero(np.any(np.abs(raw_delta - periodic_delta) > 1e-7, axis=1))), "maximum_cell_difference_A": float(np.abs(frame.cell - previous_cell).max()), "action": "omit next segment's identical periodic initial state only; legitimate cell-image changes recorded; raw files preserved"}
                    if projection:
                        entry.update(action=projection["action"], verified_SHAKE_setup_projection=projection)
                    boundary_receipts.append(entry)
                continue
            if previous_step is not None and frame.step <= previous_step:
                raise ValidationError("native segment steps are duplicated or out of order")
            previous_step = frame.step
            # Consumers discard large frame arrays after analysis. Keep a
            # private boundary copy instead of retaining their mutable Frame.
            previous_positions, previous_cell = frame.positions.copy(), frame.cell.copy()
            previous = Frame(frame.index, previous_positions, previous_cell, frame.time_ps, frame.step, frame.time_source, None if frame.velocities is None else frame.velocities.copy(), None if frame.cell_origin is None else frame.cell_origin.copy())
            frame.index = index
            index += 1
            yield frame
        if count == 0:
            raise ValidationError("empty native trajectory segment")


def frames(path, engine, timestep_ps, trajectory_format=None, *, boundary_receipts=None, boundary_policy=None):
    if isinstance(path, list):
        if engine != "lammps" or trajectory_format not in (None, "LAMMPSDUMP"):
            raise ValidationError("multi-file native comparison currently supports only LAMMPS dumps")
        yield from lammps_segment_frames(path, timestep_ps, boundary_receipts, boundary_policy)
        return
    if engine == "lammps":
        if trajectory_format not in (None, "LAMMPSDUMP"):
            raise ValidationError("LAMMPS comparison requires its native text dump")
        yield from lammps_frames(path, timestep_ps)
        return
    allowed = {"gromacs": {"XTC", "TRR"}, "namd": {"DCD"}, "amber": {"NCDF"}}
    fmt = trajectory_format or {"gromacs": "XTC", "namd": "DCD", "amber": "NCDF"}[engine]
    if fmt not in allowed[engine]:
        raise ValidationError(f"unsupported {engine} native format {fmt}")
    if fmt in ("XTC", "TRR"):
        # The low-level read-only stream avoids MDA's hidden offset cache writes
        # next to the preserved raw XTC/TRR. GROMACS native distance unit is nm.
        from MDAnalysis.lib.formats.libmdaxdr import TRRFile, XTCFile
        with (XTCFile if fmt == "XTC" else TRRFile)(str(path), "r") as reader:
            for i, value in enumerate(reader):
                yield Frame(i, finite(value.x * 10., "coordinates"), validate_cell(value.box * 10.), float(value.time), int(value.step), "native GROMACS time and step")
        return
    import MDAnalysis as mda
    from MDAnalysis.lib.mdamath import triclinic_vectors
    with mda.coordinates.core.reader(str(path), format=fmt) as reader:
        header = reader._file.header if fmt == "DCD" else None
        if header:
            # Do not override dt: verify the actual DCD delta/nsavc values.
            if not np.isclose(reader.dt / header["nsavc"], timestep_ps, atol=1e-8, rtol=1e-5):
                raise ValidationError("DCD stored integration timestep differs from protocol")
        for i, ts in enumerate(reader):
            if ts.dimensions is None:
                raise ValidationError(f"{fmt} frame {i} lacks periodic cell")
            step = int(ts.data["step"]) if "step" in ts.data else None
            source = "native trajectory time"
            if header:
                step = int(header["istart"] + i * header["nsavc"])
                source = "native DCD istart/nsavc/delta (AKMA converted to ps)"
            # NCDF may be readable without a time variable, in which case MDA
            # would manufacture frame*dt. Never accept that as measured time.
            if fmt == "NCDF" and "time" not in reader.trjfile.variables:
                raise ValidationError("AMBER NetCDF has no native time variable")
            yield Frame(i, finite(ts.positions.copy(), "coordinates"), validate_cell(triclinic_vectors(ts.dimensions, dtype=np.float64)), float(ts.time), step, source)


def validate_timeline(all_frames, origin_step, origin_time_ps, production_steps, output_every, timestep_ps):
    times = finite([f.time_ps - origin_time_ps for f in all_frames], "frame times")
    if len(times) not in (production_steps // output_every, production_steps // output_every + 1):
        raise ValidationError(f"wrong frame count {len(times)}")
    include_zero = len(times) == production_steps // output_every + 1
    expected_steps = np.arange(0 if include_zero else output_every, production_steps + 1, output_every)
    if not np.allclose(times, expected_steps * timestep_ps, atol=1e-3, rtol=0):
        raise ValidationError("native frame times are missing, duplicated, out of order or inconsistent with production origin")
    for frame, expected in zip(all_frames, expected_steps):
        if frame.step is not None and frame.step - origin_step != expected:
            raise ValidationError(f"native frame {frame.index} step differs from production schedule")
    return expected_steps, include_zero
