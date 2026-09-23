#!/usr/bin/env python3
"""Render validated real coordinates with one camera, timing and representation."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import numpy as np

from compare import ENGINES, file_receipt, source_identity, write_json
from geometry import ValidationError, finite

COLORS = {"C": "#d9e4e9", "H": "#ffffff", "N": "#5499ff", "O": "#ff655f"}
SETTINGS = {"pixels": 720, "fps": 40, "elevation_degrees": 18, "azimuth_degrees": -55, "half_width_A": 14, "camera_zoom": 1.55, "water_display_radius_A": 13, "projection": "orthographic", "water": "oxygen points, alpha 0.18; fixed 13 A display crop only", "peptide": "all 22 atoms, element-colored sticks", "alignment": "bond-whole peptide, heavy-atom centroid, proper Kabsch to the canonical master; same reference for all engines", "frame_interval_ps": 1, "playback_ps_per_second": 40}


def probe(path):
    result = subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_read_frames,duration,pix_fmt", "-of", "json", str(path)], text=True)
    return json.loads(result)["streams"][0]


def verify_video(path, count, pixels, fps, grid=False):
    result = probe(path)
    size = pixels * (2 if grid else 1)
    if int(result["nb_read_frames"]) != count or (result["width"], result["height"]) != (size, size):
        raise ValidationError("encoded video frame count/dimensions differ")
    numerator, denominator = map(int, result["r_frame_rate"].split("/"))
    if numerator / denominator != fps or abs(float(result["duration"]) - count / fps) > .001:
        raise ValidationError("encoded video duration/frame rate differs")
    return result


def compose_grid(paths, output, count, settings=None):
    settings = settings or SETTINGS
    if len(paths) != 4:
        raise ValidationError("comparison grid requires exactly four videos")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    for path in paths:
        verify_video(path, count, settings["pixels"], settings["fps"])
        command.extend(["-i", str(path)])
    command.extend(["-filter_complex", "[0:v][1:v][2:v][3:v]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[v]", "-map", "[v]", "-c:v", "libx264", "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-threads", "2", str(output)])
    subprocess.run(command, check=True)
    return verify_video(output, count, settings["pixels"], settings["fps"], grid=True)


def render_clip(data, engine, output, *, settings=None, synthetic=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FFMpegWriter
    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    settings = settings or SETTINGS
    peptide, water, times = data["peptide"], data["water"], data["time_ps"]
    for label, value in (("peptide", peptide), ("water", water), ("times", times)):
        finite(value, label)
    if len(peptide) != len(water) or len(peptide) != len(times):
        raise ValidationError("display arrays have different frame counts")
    if len(times) > 1 and not np.allclose(np.diff(times), settings["frame_interval_ps"], atol=.001, rtol=0):
        raise ValidationError("display time interval differs")
    bonds = data["bonds"]
    elements = data["elements"]
    colors = [COLORS.get(e, "#88d968") for e in elements]
    sizes = [9 if e == "H" else 34 for e in elements]
    fig = plt.figure(figsize=(settings["pixels"] / 100, settings["pixels"] / 100), dpi=100, facecolor="#0c1824")
    axis = fig.add_axes((.025, .08, .95, .83), projection="3d", facecolor="#0c1824")
    axis.set_proj_type("ortho")
    axis.view_init(elev=settings["elevation_degrees"], azim=settings["azimuth_degrees"])
    radius = settings["half_width_A"]
    axis.set(xlim=(-radius, radius), ylim=(-radius, radius), zlim=(-radius, radius))
    axis.set_box_aspect((1, 1, 1), zoom=settings["camera_zoom"])
    axis.set_axis_off()
    water_artist = axis.scatter([], [], [], s=3, c="#57b4d5", alpha=.18, edgecolors="none", depthshade=False)
    atoms = axis.scatter(*peptide[0].T, s=sizes, c=colors, edgecolors="none", depthshade=False)
    segments = Line3DCollection([], colors=[colors[int(i)] for bond in bonds for i in bond], linewidths=3, alpha=1.)
    axis.add_collection3d(segments, autolim=False)
    fig.text(.5, .95, engine.upper(), ha="center", color="white", fontsize=21, weight="bold")
    fig.text(.5, .916, "SYNTHETIC geometry fixture · no production simulation" if synthetic else "ACE–ALA–NME · ff14SB / TIP3P · NPT 300 K, 1 bar", ha="center", color="#acbdca", fontsize=10)
    clock = fig.text(.5, .075, "", ha="center", color="white", fontsize=13)
    fig.text(.5, .046, "Encoder/geometry unit test only" if synthetic else "1 ps/frame · 40 ps/s · one shared camera + master reference", ha="center", color="#acbdca", fontsize=9)
    fig.text(.5, .022, "Water: translucent O points within 13 Å · raw trajectories preserved", ha="center", color="#acbdca", fontsize=8)
    if synthetic:
        fig.text(.5, .17, "SYNTHETIC UNIT FIXTURE\nNOT SCIENTIFIC EVIDENCE", ha="center", color="#ffcc66", fontsize=17, alpha=.9)
    writer = FFMpegWriter(fps=settings["fps"], codec="libx264", metadata={"title": f"{engine}: canonical alanine comparison", "comment": "Synthetic unit fixture" if synthetic else "Real native MD coordinates; no interpolation, smoothing or generated frames; 1 ns is not a convergence claim"}, extra_args=["-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-threads", "2"])
    with writer.saving(fig, str(output), dpi=100):
        for index, (p, w, t) in enumerate(zip(peptide, water, times)):
            w = w[np.linalg.norm(w, axis=1) <= settings["water_display_radius_A"]]
            water_artist._offsets3d = tuple(w.T)
            atoms._offsets3d = tuple(p.T)
            half_segments = []
            for a, b in bonds:
                midpoint = (p[a] + p[b]) * .5
                half_segments.extend(((p[a], midpoint), (midpoint, p[b])))
            segments.set_segments(half_segments)
            clock.set_text(f"Synthetic frame {index + 1} / {len(times)}" if synthetic else f"Production {t:7.1f} / 1000 ps")
            writer.grab_frame()
            if index == len(times) // 2:
                fig.savefig(Path(output).with_suffix(".png"), dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    return verify_video(output, len(times), settings["pixels"], settings["fps"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engines", choices=ENGINES, nargs="+", help="explicit partial standalone clips; no 2x2 grid until all four pass")
    args = parser.parse_args()
    analysis_receipt = json.loads((args.analysis / "receipt.json").read_text())
    selected = args.engines or ENGINES
    if len(selected) != len(set(selected)):
        parser.error("engine selection contains duplicates")
    complete = set(selected) == set(ENGINES)
    expected = "analysis-passed" if complete else ("analysis-passed", "partial-analysis-passed; missing engines remain unqualified")
    if analysis_receipt["status"] not in ((expected,) if isinstance(expected, str) else expected):
        parser.error("selected real engine analyses must pass; all four required for comparison grid")
    if not set(selected) <= {run["engine"] for run in analysis_receipt["runs"]}:
        parser.error("selected engine has no passed analysis")
    if args.output.exists():
        parser.error("output directory must not exist")
    args.output.mkdir(parents=True)
    started = time.time()
    receipt = {"status": "incomplete", "evidence_kind": "real-native-md-render", "source": source_identity(), "analysis_receipt": file_receipt(args.analysis / "receipt.json"), "settings": SETTINGS, "videos": {}, "missing_engines": sorted(set(ENGINES) - set(selected)), "scientific_convergence_claimed": False}
    try:
        for record in analysis_receipt["outputs"]:
            if file_receipt(record["path"]) != record:
                raise ValidationError("analysis output changed since receipt")
        for engine in selected:
            with np.load(args.analysis / engine / "display.npz", allow_pickle=False) as data:
                if len(data["time_ps"]) != 1000 or not np.allclose(data["time_ps"], np.arange(1, 1001), atol=.001, rtol=0):
                    raise ValidationError("real rendering requires 1..1000 ps, every 1 ps")
                receipt["videos"][engine] = render_clip(data, engine, args.output / f"{engine}.mp4")
        if complete:
            receipt["videos"]["four-engine-2x2"] = compose_grid([args.output / f"{engine}.mp4" for engine in ENGINES], args.output / "four-engine-2x2.mp4", 1000)
        receipt["status"] = "real-trajectories-rendered-and-encoding-validated" if complete else "partial-real-clips-rendered; four-engine comparison incomplete"
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt["render_wall_seconds"] = time.time() - started
        receipt["outputs"] = [file_receipt(path) for path in sorted(args.output.iterdir()) if path.is_file() and path.name != "receipt.json"]
        write_json(args.output / "receipt.json", receipt)


if __name__ == "__main__":
    main()
