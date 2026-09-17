"""Offline verifier for an FS2 static-PIE/frozen Python execution artifact.

This packaging gate never executes the candidate Python.  It parses ELF64
directly, verifies the exact frozen payload against a complete canonical module
inventory, joins reproducible build provenance, and verifies normalized
Platform Security Ed25519 review with an independently pinned static OpenSSL.
Runtime launchers separately re-hash all four reviewed ELF sections before any
Python instruction executes. Neither verifier treats those sections as opaque:
the canonical inventory is parsed record by record, each record is joined to
its payload digest and the actual CPython frozen table/relocations, and the
detached signature is checked against a fixed root-owned policy/key authority
that is not selected by the build invocation.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


SECTIONS = {
    "inventory": ".fs2_frozen_module_inventory",
    "payload": ".fs2_frozen_module_payload",
    "provenance": ".fs2_frozen_build_provenance",
    "attestation": ".fs2_frozen_review_attestation",
    "table": ".fs2_frozen_cpython_table",
}
REVIEW_KEY = Path("/etc/fs2/public-edge-frozen-runtime-review-key.bin")
VERIFIER_PATH = Path("/usr/local/libexec/fs2-verify-public-edge-frozen-runtime.py")
VERIFIER_LAUNCHER_PATH = Path(
    "/usr/local/libexec/fs2-verify-public-edge-frozen-runtime"
)
VERIFIER_RUNTIME_PATH = Path(
    "/usr/local/libexec/fs2-public-edge-frozen-verifier-python"
)
OPENSSL_PATH = Path("/usr/local/libexec/fs2-public-edge-package-openssl-static")
VERIFIER_POLICY = Path("/etc/fs2/public-edge-frozen-runtime-verifier.json")
SHA256 = __import__("re").compile(r"^[a-f0-9]{64}$")


class VerificationError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise VerificationError(message)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"{label} must contain exactly {sorted(keys)}")
    return value


def digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None or value == "0" * 64:
        fail(f"{label} must be a nonzero lowercase SHA-256 digest")
    return value


def canonical_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not UTF-8 JSON") from exc
    if canonical(value) + b"\n" != raw:
        fail(f"{label} must be canonical JSON plus one newline")
    if not isinstance(value, dict):
        fail(f"{label} must be an object")
    return value


def read_regular(path: Path, label: str, maximum: int = 512 * 1024 * 1024) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        fail(f"{label} must be an absolute non-symlink path")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > maximum:
            fail(f"{label} is not a bounded regular file")
        raw = os.pread(descriptor, before.st_size, 0)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or len(raw) != before.st_size:
            fail(f"{label} changed while read")
        return raw
    finally:
        os.close(descriptor)


def read_root_authority(
    path: Path, label: str, *, mode: int, maximum: int
) -> bytes:
    current = Path("/")
    for part in path.parent.parts[1:]:
        current /= part
        details = os.stat(current, follow_symlinks=False)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            fail(f"{label} parent chain is not root-owned and protected")
    details = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or details.st_gid != 0
        or stat.S_IMODE(details.st_mode) != mode
    ):
        fail(f"{label} is not the fixed root-owned authority")
    return read_regular(path, label, maximum)


def require_fully_static_elf(raw: bytes, label: str) -> None:
    if len(raw) < 64 or raw[:6] != b"\x7fELF\x02\x01":
        fail(f"{label} is not a supported ELF64 executable")
    elf_type, machine = struct.unpack_from("<HH", raw, 16)
    program_offset = struct.unpack_from("<Q", raw, 32)[0]
    entry_size, entry_count = struct.unpack_from("<HH", raw, 54)
    if (
        elf_type != 2
        or machine != 62
        or entry_size < 56
        or entry_count < 1
        or program_offset + entry_size * entry_count > len(raw)
    ):
        fail(f"{label} has a malformed program-header table")
    program_types = {
        struct.unpack_from("<I", raw, program_offset + index * entry_size)[0]
        for index in range(entry_count)
    }
    if 2 in program_types or 3 in program_types:
        fail(
            f"{label} static ET_EXEC must have neither PT_DYNAMIC nor PT_INTERP"
        )


def require_static_pie_elf(raw: bytes, label: str) -> None:
    """Accept only interpreter-free ET_DYN with bounded self-relocation."""

    try:
        sections, _headers, programs = elf_sections(raw)
        verify_static_pie(raw, sections, programs)
    except VerificationError as exc:
        raise VerificationError(
            f"{label} is not an exact dependency-free static PIE: {exc}"
        ) from exc


def verifier_authority() -> tuple[bytes, bytes]:
    policy_raw = read_root_authority(
        VERIFIER_POLICY,
        "frozen-runtime verifier policy",
        mode=0o444,
        maximum=128 * 1024,
    )
    policy = exact(
        canonical_json(policy_raw, "frozen-runtime verifier policy"),
        {
            "schema",
            "role",
            "verifier_path",
            "verifier_sha256",
            "launcher_path",
            "launcher_sha256",
            "runtime_path",
            "runtime_sha256",
            "openssl_path",
            "openssl_sha256",
            "review_key_sha256",
        },
        "frozen-runtime verifier policy",
    )
    if (
        policy["schema"]
        != "fs2-serve.nebius.ai/public-edge-frozen-runtime-verifier/v1"
        or policy["role"] != "platform-security-frozen-runtime-verifier"
        or policy["verifier_path"] != str(VERIFIER_PATH)
        or policy["launcher_path"] != str(VERIFIER_LAUNCHER_PATH)
        or policy["runtime_path"] != str(VERIFIER_RUNTIME_PATH)
        or policy["openssl_path"] != str(OPENSSL_PATH)
        or Path(__file__) != VERIFIER_PATH
    ):
        fail("frozen-runtime verifier is not the fixed independently installed authority")
    verifier_raw = read_root_authority(
        VERIFIER_PATH,
        "frozen-runtime verifier",
        mode=0o444,
        maximum=4 * 1024 * 1024,
    )
    launcher_raw = read_root_authority(
        VERIFIER_LAUNCHER_PATH,
        "frozen-runtime verifier launcher",
        mode=0o555,
        maximum=4 * 1024 * 1024,
    )
    runtime_raw = read_root_authority(
        VERIFIER_RUNTIME_PATH,
        "frozen-runtime verifier interpreter",
        mode=0o555,
        maximum=512 * 1024 * 1024,
    )
    openssl_raw = read_root_authority(
        OPENSSL_PATH,
        "frozen-runtime verification OpenSSL",
        mode=0o555,
        maximum=64 * 1024 * 1024,
    )
    review_key = read_root_authority(
        REVIEW_KEY,
        "frozen-runtime reviewer key",
        mode=0o444,
        maximum=32,
    )
    if (
        hashlib.sha256(verifier_raw).hexdigest()
        != digest(policy["verifier_sha256"], "accepted verifier digest")
        or hashlib.sha256(launcher_raw).hexdigest()
        != digest(policy["launcher_sha256"], "accepted verifier-launcher digest")
        or hashlib.sha256(runtime_raw).hexdigest()
        != digest(policy["runtime_sha256"], "accepted verifier-runtime digest")
        or hashlib.sha256(openssl_raw).hexdigest()
        != digest(policy["openssl_sha256"], "accepted OpenSSL digest")
        or hashlib.sha256(review_key).hexdigest()
        != digest(policy["review_key_sha256"], "accepted review-key digest")
    ):
        fail("frozen-runtime verifier authority differs from its root-owned policy")
    runtime_identity = os.stat(VERIFIER_RUNTIME_PATH, follow_symlinks=False)
    process_identity = os.stat("/proc/self/exe")
    if (
        not stat.S_ISREG(runtime_identity.st_mode)
        or (runtime_identity.st_dev, runtime_identity.st_ino)
        != (process_identity.st_dev, process_identity.st_ino)
        or not sys.flags.isolated
        or not sys.flags.no_user_site
        or sys.dont_write_bytecode is not True
        or sys.path
    ):
        fail("verifier did not start in the exact isolated frozen runtime")
    require_static_pie_elf(launcher_raw, "frozen-runtime verifier launcher")
    require_fully_static_elf(openssl_raw, "frozen-runtime verification OpenSSL")
    return review_key, openssl_raw


def elf_sections(raw: bytes) -> tuple[dict[str, tuple[int, tuple[int, ...], bytes]], list[tuple[int, ...]], list[tuple[int, ...]]]:
    if raw[:4] != b"\x7fELF" or raw[4:6] != b"\x02\x01":
        fail("frozen runtime is not little-endian ELF64")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", raw, 0)
    _ident, elf_type, machine, _version, _entry, phoff, shoff, _flags, ehsize, phentsize, phnum, shentsize, shnum, shstrndx = header
    if elf_type != 3 or machine != 62 or phentsize != 56 or shentsize != 64 or shnum < 1 or shstrndx >= shnum:
        fail("frozen runtime ELF header is unsupported")
    programs = []
    for index in range(phnum):
        program = struct.unpack_from("<IIQQQQQQ", raw, phoff + index * phentsize)
        programs.append(program)
        p_type = program[0]
        if p_type == 3:  # PT_INTERP
            fail("frozen runtime is not an interpreter-free static PIE")
    headers = [struct.unpack_from("<IIQQQQIIQQ", raw, shoff + index * shentsize) for index in range(shnum)]
    names_header = headers[shstrndx]
    names = raw[names_header[4] : names_header[4] + names_header[5]]
    result: dict[str, tuple[int, tuple[int, ...], bytes]] = {}
    for index, section in enumerate(headers):
        name_end = names.find(b"\0", section[0])
        if name_end < 0:
            fail("ELF section name is unterminated")
        name = names[section[0] : name_end].decode("ascii")
        content = raw[section[4] : section[4] + section[5]]
        if name in result:
            fail(f"ELF contains duplicate section {name}")
        result[name] = (index, section, content)
    for metadata_name in (SECTIONS["provenance"], SECTIONS["attestation"]):
        metadata = result.get(metadata_name)
        if metadata is None:
            continue
        metadata_index, section, _content = metadata
        section_start, section_end = section[4], section[4] + section[5]
        if section[1] != 1 or section[2] != 0 or section[3] != 0:
            fail("frozen provenance/attestation must be non-ALLOC metadata PROGBITS")
        control_ranges = [
            (0, ehsize),
            (phoff, phoff + phnum * phentsize),
            (shoff, shoff + shnum * shentsize),
        ]
        if any(not (section_end <= start or section_start >= end) for start, end in control_ranges):
            fail("frozen provenance/attestation overlaps an ELF control table")
        for other_index, other in enumerate(headers):
            if other_index == metadata_index or other[1] == 8 or other[5] == 0:
                continue
            if not (section_end <= other[4] or section_start >= other[4] + other[5]):
                fail("frozen provenance/attestation overlaps another ELF section")
        for program in programs:
            if program[0] == 1 and not (section_end <= program[2] or section_start >= program[2] + program[5]):
                fail("frozen provenance/attestation overlaps a PT_LOAD segment")
    return result, headers, programs


def require_linkage(
    raw: bytes,
    sections: dict[str, tuple[int, tuple[int, ...], bytes]],
    headers: list[tuple[int, ...]],
    relocations: dict[int, tuple[int, int]],
) -> tuple[list[tuple[int, ...]], tuple[int, ...], int, int]:
    symtab = sections.get(".symtab")
    if symtab is None or symtab[1][9] != 24 or symtab[1][6] >= len(headers):
        fail("frozen runtime omits its exact linkage symbol table")
    strings_header = headers[symtab[1][6]]
    strings = raw[strings_header[4] : strings_header[4] + strings_header[5]]
    symbols: dict[str, tuple[int, int, int]] = {}
    content = symtab[2]
    for offset in range(0, len(content), 24):
        name_offset, info, _other, section_index, value, size = struct.unpack_from(
            "<IBBHQQ", content, offset
        )
        end = strings.find(b"\0", name_offset)
        if end < 0:
            fail("frozen runtime symbol name is unterminated")
        name = strings[name_offset:end].decode("ascii")
        if name in {
            "__fs2_frozen_inventory_start",
            "__fs2_frozen_inventory_end",
            "__fs2_frozen_payload_start",
            "__fs2_frozen_payload_end",
            "__fs2_frozen_table_start",
            "__fs2_frozen_table_end",
            "PyImport_FrozenModules",
            "_PyImport_FrozenBootstrap",
            "_PyImport_FrozenStdlib",
            "_PyImport_FrozenTest",
            "PyImport_Inittab",
        }:
            if name in symbols or info >> 4 != 1:
                fail("frozen runtime linkage symbol is duplicate or non-global")
            symbols[name] = (value, size, section_index)
    inventory = sections[SECTIONS["inventory"]]
    payload = sections[SECTIONS["payload"]]
    table = sections[SECTIONS["table"]]
    expected = {
        "__fs2_frozen_inventory_start": (inventory[1][3], 0),
        "__fs2_frozen_inventory_end": (inventory[1][3] + inventory[1][5], 0),
        "__fs2_frozen_payload_start": (payload[1][3], 0),
        "__fs2_frozen_payload_end": (payload[1][3] + payload[1][5], 0),
        "__fs2_frozen_table_start": (table[1][3], 0),
        "__fs2_frozen_table_end": (table[1][3] + table[1][5], 0),
    }
    if set(symbols) != {
        *expected,
        "PyImport_FrozenModules",
        "_PyImport_FrozenBootstrap",
        "_PyImport_FrozenStdlib",
        "_PyImport_FrozenTest",
        "PyImport_Inittab",
    }:
        fail("frozen runtime omits an exact CPython frozen-table linkage symbol")
    for name, (value, size) in expected.items():
        if symbols[name][:2] != (value, size):
            fail(f"frozen runtime linkage symbol {name} has the wrong extent")
    pointer_storages: list[tuple[tuple[int, ...], int, int, int]] = []
    pointer_names = (
        "PyImport_FrozenModules",
        "_PyImport_FrozenBootstrap",
        "_PyImport_FrozenStdlib",
        "_PyImport_FrozenTest",
    )
    pointer_ranges: list[tuple[int, int]] = []
    for name in pointer_names:
        pointer_value, pointer_size, pointer_section = symbols[name]
        if pointer_size != 8 or pointer_section >= len(headers):
            fail(f"{name} is not one defined CPython pointer variable")
        storage = headers[pointer_section]
        if (
            storage[1] != 1
            or pointer_value < storage[3]
            or pointer_value + 8 > storage[3] + storage[5]
            or relocations.get(pointer_value) != (8, table[1][3])
        ):
            fail(f"{name} does not point to the exact reviewed frozen table")
        if any(
            not (location + 8 <= pointer_value or pointer_value + 8 <= location)
            and location != pointer_value
            for location in relocations
        ):
            fail(f"an unexpected relocation overlaps {name}")
        if any(
            not (pointer_value + 8 <= start or end <= pointer_value)
            for start, end in pointer_ranges
        ):
            fail("CPython frozen pointer variables overlap")
        pointer_ranges.append((pointer_value, pointer_value + 8))
        pointer_storages.append(
            (
                storage,
                storage[4] + pointer_value - storage[3],
                pointer_value,
                8,
            )
        )
    builtin_address, builtin_size, builtin_section = symbols["PyImport_Inittab"]
    if (
        builtin_size < 16
        or builtin_size % 16
        or builtin_size > 512 * 16
        or builtin_section >= len(headers)
    ):
        fail("PyImport_Inittab is not one bounded direct _inittab array")
    builtin_storage = headers[builtin_section]
    if (
        builtin_storage[1] != 1
        or builtin_address < builtin_storage[3]
        or builtin_address + builtin_size > builtin_storage[3] + builtin_storage[5]
    ):
        fail("PyImport_Inittab array is outside its defined ELF section")
    return (
        pointer_storages,
        (
            builtin_storage,
            builtin_storage[4] + builtin_address - builtin_storage[3],
            builtin_address,
            builtin_size,
        ),
        builtin_address,
        builtin_size,
    )


def bytes_at_virtual_address(
    raw: bytes,
    programs: list[tuple[int, ...]],
    address: int,
    size: int,
) -> bytes:
    matches = [
        program
        for program in programs
        if program[0] == 1
        and address >= program[3]
        and address + size <= program[3] + program[5]
    ]
    if len(matches) != 1:
        fail("reviewed runtime address is not backed by one exact PT_LOAD file range")
    program = matches[0]
    offset = program[2] + address - program[3]
    return raw[offset : offset + size]


def verify_builtin_table(
    raw: bytes,
    programs: list[tuple[int, ...]],
    relocations: dict[int, tuple[int, int]],
    table_address: int,
    table_size: int,
) -> set[str]:
    names: set[str] = set()
    row_count = table_size // 16
    for index in range(row_count):
        row_address = table_address + index * 16
        row = bytes_at_virtual_address(raw, programs, row_address, 16)
        name_pointer, function_pointer = struct.unpack("<QQ", row)
        name_relocation = relocations.get(row_address)
        function_relocation = relocations.get(row_address + 8)
        if (
            name_pointer == 0
            and function_pointer == 0
            and name_relocation is None
            and function_relocation is None
        ):
            if index != row_count - 1:
                fail("PyImport_Inittab has data after its zero sentinel")
            break
        if (
            name_relocation is None
            or name_relocation[0] != 8
            or function_relocation is None
            or function_relocation[0] not in {8, 37}
            or load_membership(
                programs,
                function_relocation[1],
                1,
                required_flags=5,
                forbidden_flags=2,
            )
            != 1
        ):
            fail("PyImport_Inittab row is not bound to reviewed name/code mappings")
        name_bytes = bytearray()
        for offset in range(256):
            value = bytes_at_virtual_address(
                raw, programs, name_relocation[1] + offset, 1
            )[0]
            if value == 0:
                break
            name_bytes.append(value)
        else:
            fail("PyImport_Inittab name is unterminated")
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise VerificationError("PyImport_Inittab name is not ASCII") from exc
        if (
            not name
            or any(not (character.isalnum() or character in "_.") for character in name)
            or name in names
        ):
            fail("PyImport_Inittab contains an invalid or duplicate module")
        names.add(name)
    else:
        fail("PyImport_Inittab lacks a bounded zero sentinel")
    required = {
        "_ctypes",
        "_hashlib",
        "_imp",
        "_io",
        "_socket",
        "_thread",
        "_warnings",
        "_weakref",
        "array",
        "atexit",
        "binascii",
        "builtins",
        "errno",
        "faulthandler",
        "fcntl",
        "grp",
        "marshal",
        "math",
        "posix",
        "pwd",
        "select",
        "sys",
        "time",
    }
    if not required.issubset(names):
        fail("PyImport_Inittab omits a bootstrap-required native module")
    return names


def require_readonly_mapping(
    section: tuple[int, ...],
    programs: list[tuple[int, ...]],
    label: str,
    *,
    allow_relro: bool = False,
) -> None:
    if (
        section[5] < 1
        or section[2] not in ({2, 3} if allow_relro else {2})
    ):
        fail(f"{label} is not read-only non-executable ALLOC PROGBITS")
    matches = 0
    for program in programs:
        if program[0] != 1:
            continue
        file_contains = section[4] >= program[2] and section[4] + section[5] <= program[2] + program[5]
        virtual_contains = section[3] >= program[3] and section[3] + section[5] <= program[3] + program[6]
        if file_contains or virtual_contains:
            relro_matches = sum(
                1
                for candidate in programs
                if candidate[0] == 0x6474E552
                and section[3] >= candidate[3]
                and section[3] + section[5] <= candidate[3] + candidate[6]
            )
            runtime_readonly = program[1] == 4 or (
                allow_relro and program[1] == 6 and relro_matches == 1
            )
            if not file_contains or not virtual_contains or not runtime_readonly or section[4] - program[2] != section[3] - program[3]:
                fail(f"{label} file bytes and virtual mapping are not congruent read-only data")
            matches += 1
    if matches != 1:
        fail(f"{label} is not covered by one exact read-only PT_LOAD")


def require_disjoint(sections: list[tuple[int, ...]]) -> None:
    for left_index, left in enumerate(sections):
        for right in sections[left_index + 1 :]:
            file_disjoint = left[4] + left[5] <= right[4] or right[4] + right[5] <= left[4]
            virtual_disjoint = left[3] + left[5] <= right[3] or right[3] + right[5] <= left[3]
            if not file_disjoint or not virtual_disjoint:
                fail("frozen authority sections overlap in file or virtual memory")


def require_authority_storage_disjoint(
    core_sections: list[tuple[int, ...]],
    authorities: list[tuple[tuple[int, ...], int, int, int]],
) -> None:
    """Bind every executable import authority to distinct verified bytes.

    Stock CPython may place several pointer variables and PyImport_Inittab in
    the same read-only-after-relocation section.  Equal section headers are
    therefore de-duplicated, but distinct storage sections may not overlap and
    every exact symbol object must remain pairwise disjoint in both the file
    image and virtual address space.
    """

    unique_storage_sections: list[tuple[int, ...]] = []
    exact_ranges: list[tuple[int, int, int, int]] = []
    for section, file_start, virtual_start, size in authorities:
        if size <= 0:
            fail("CPython authority storage has an empty exact range")
        if (
            file_start < section[4]
            or file_start + size > section[4] + section[5]
            or virtual_start < section[3]
            or virtual_start + size > section[3] + section[5]
            or file_start - section[4] != virtual_start - section[3]
        ):
            fail("CPython authority storage range is not congruent with its section")
        for prior_file, prior_virtual, prior_size, _prior_index in exact_ranges:
            if not (
                file_start + size <= prior_file
                or prior_file + prior_size <= file_start
            ) or not (
                virtual_start + size <= prior_virtual
                or prior_virtual + prior_size <= virtual_start
            ):
                fail("CPython authority storage objects overlap")
        exact_ranges.append((file_start, virtual_start, size, len(exact_ranges)))
        if section not in unique_storage_sections:
            unique_storage_sections.append(section)

    require_disjoint(core_sections + unique_storage_sections)
    for _section, file_start, virtual_start, size in authorities:
        for core in core_sections:
            file_disjoint = (
                file_start + size <= core[4]
                or core[4] + core[5] <= file_start
            )
            virtual_disjoint = (
                virtual_start + size <= core[3]
                or core[3] + core[5] <= virtual_start
            )
            if not file_disjoint or not virtual_disjoint:
                fail("CPython authority object overlaps reviewed frozen data")


def load_membership(
    programs: list[tuple[int, ...]],
    address: int,
    size: int,
    *,
    required_flags: int,
    forbidden_flags: int,
) -> int:
    return sum(
        1
        for program in programs
        if program[0] == 1
        and address >= program[3]
        and size <= program[6]
        and address - program[3] <= program[6] - size
        and program[1] & required_flags == required_flags
        and program[1] & forbidden_flags == 0
    )


def verify_static_pie(
    raw: bytes,
    sections: dict[str, tuple[int, tuple[int, ...], bytes]],
    programs: list[tuple[int, ...]],
) -> dict[int, tuple[int, int]]:
    dynamics = [entry for entry in sections.values() if entry[1][1] == 6]
    relas = [
        entry
        for entry in sections.values()
        if entry[1][1] == 4 and entry[1][2] & 2
    ]
    if len(dynamics) != 1 or len(relas) != 1:
        fail("static PIE must contain one exact dynamic relocation closure")
    dynamic = dynamics[0][1]
    rela = relas[0][1]
    if (
        dynamic[9] != 16
        or dynamic[5] < 16
        or dynamic[5] % 16
        or rela[9] != 24
        or rela[5] < 24
        or rela[5] % 24
    ):
        fail("static-PIE dynamic or RELA section is not exactly bounded")
    require_readonly_mapping(dynamic, programs, "dynamic table", allow_relro=True)
    require_readonly_mapping(rela, programs, "RELA closure")
    dynamic_segments = [program for program in programs if program[0] == 2]
    if len(dynamic_segments) != 1 or (
        dynamic_segments[0][2],
        dynamic_segments[0][3],
        dynamic_segments[0][5],
        dynamic_segments[0][6],
    ) != (dynamic[4], dynamic[3], dynamic[5], dynamic[5]):
        fail("PT_DYNAMIC does not exactly describe the reviewed dynamic section")

    required: dict[int, int] = {}
    flags = 0
    seen_flags = False
    flags_1: int | None = None
    terminated = False
    forbidden = {
        1,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        22,
        23,
        29,
        0x6FFFFEFB,
        0x6FFFFEFC,
        0x7FFFFFFD,
        0x7FFFFFFF,
    }
    for offset in range(0, len(dynamics[0][2]), 16):
        tag, value = struct.unpack_from("<qQ", dynamics[0][2], offset)
        if terminated:
            if tag != 0 or value != 0:
                fail("static-PIE dynamic table has data after its terminator")
            continue
        if tag == 0:
            terminated = True
        elif tag in forbidden:
            fail("static-PIE runtime declares an external or non-RELA dependency")
        elif tag in {7, 8, 9, 0x6FFFFFF9}:
            if tag in required:
                fail("static-PIE dynamic contract contains a duplicate relocation tag")
            required[tag] = value
        elif tag == 30:
            if seen_flags:
                fail("static-PIE dynamic contract contains duplicate DT_FLAGS")
            seen_flags = True
            flags = value
        elif tag == 0x6FFFFFFB:
            if flags_1 is not None:
                fail("static-PIE dynamic contract contains duplicate DT_FLAGS_1")
            flags_1 = value
    expected_tags = {7, 8, 9, 0x6FFFFFF9}
    if (
        not terminated
        or set(required) != expected_tags
        or required[7] != rela[3]
        or required[8] != rela[5]
        or required[9] != 24
        or required[0x6FFFFFF9] > rela[5] // 24
        or flags & 4
        or flags_1 is None
        or flags_1 & 0x08000000 == 0
    ):
        fail("static-PIE dynamic contract does not bind exact self-relocation")

    relocations: dict[int, tuple[int, int]] = {}
    relative_count = required[0x6FFFFFF9]
    for index in range(rela[5] // 24):
        location, info, addend = struct.unpack_from("<QQq", raw, rela[4] + index * 24)
        relocation_type = info & 0xFFFFFFFF
        symbol = info >> 32
        if (
            symbol != 0
            or location in relocations
            or addend < 0
            or load_membership(
                programs,
                location,
                8,
                required_flags=6,
                forbidden_flags=1,
            )
            != 1
        ):
            fail("static-PIE relocation target is ambiguous or outside writable data")
        if index < relative_count:
            if relocation_type != 8 or load_membership(
                programs,
                addend,
                1,
                required_flags=4,
                forbidden_flags=0,
            ) != 1:
                fail("static-PIE relative relocation escapes the reviewed image")
        elif relocation_type != 37 or load_membership(
            programs,
            addend,
            1,
            required_flags=5,
            forbidden_flags=2,
        ) != 1:
            fail("static-PIE resolver relocation is not reviewed executable code")
        relocations[location] = (relocation_type, addend)
    if list(relocations) != sorted(relocations):
        fail("static-PIE relocation targets are not strictly ordered")
    return relocations


def verify_inventory(inventory: bytes, payload: bytes) -> list[tuple[str, int, int, int, int]]:
    if len(inventory) < 16 or inventory[:8] != b"FS2FRZ1\0":
        fail("frozen module inventory header is unsupported")
    count, reserved = struct.unpack_from("<II", inventory, 8)
    if reserved != 0 or count < 16 or count > 65535:
        fail("frozen module inventory count is outside its bounds")
    records: list[tuple[str, int, int, int, int]] = []
    cursor = 16
    payload_cursor = 0
    for index in range(count):
        if cursor + 52 > len(inventory):
            fail("frozen module inventory is truncated")
        name_length, flags, record_reserved, offset, size = struct.unpack_from(
            "<HBBQQ", inventory, cursor
        )
        expected_digest = inventory[cursor + 20 : cursor + 52]
        cursor += 52
        if cursor + name_length + 1 > len(inventory):
            fail("frozen module name exceeds the inventory")
        try:
            name = inventory[cursor : cursor + name_length].decode("ascii")
        except UnicodeDecodeError as exc:
            raise VerificationError("frozen module name is not ASCII") from exc
        if (
            not name
            or any(not (character.isalnum() or character in "_.") for character in name)
            or flags & ~1
            or record_reserved != 0
            or inventory[cursor + name_length] != 0
            or offset != payload_cursor
            or size < 1
            or offset + size > len(payload)
            or hashlib.sha256(payload[offset : offset + size]).digest()
            != expected_digest
        ):
            fail(f"frozen module record {index} is malformed or not payload-bound")
        records.append((name, flags, offset, size, cursor))
        cursor += name_length + 1
        payload_cursor += size
    names = [record[0] for record in records]
    if cursor != len(inventory) or payload_cursor != len(payload) or names != sorted(set(names)):
        fail("frozen module inventory is not a complete sorted payload closure")
    mandatory = {
        "__main__",
        "_frozen_importlib",
        "_frozen_importlib_external",
        "argparse",
        "base64",
        "codecs",
        "collections.abc",
        "contextlib",
        "ctypes",
        "datetime",
        "encodings",
        "hashlib",
        "http.client",
        "http.server",
        "importlib",
        "json",
        "os",
        "pathlib",
        "re",
        "signal",
        "socket",
        "stat",
        "struct",
        "subprocess",
        "tempfile",
        "threading",
        "types",
        "typing",
        "urllib.error",
        "urllib.parse",
        "urllib.request",
        "uuid",
    }
    if not mandatory.issubset(names):
        fail("frozen module inventory omits a launcher-required module")
    return records


def verify_cpython_table(
    raw: bytes,
    sections: dict[str, tuple[int, tuple[int, ...], bytes]],
    records: list[tuple[str, int, int, int, int]],
    programs: list[tuple[int, ...]],
    pointer_storage: tuple[int, ...],
    relocations: dict[int, tuple[int, int]],
) -> None:
    inventory = sections[SECTIONS["inventory"]]
    payload = sections[SECTIONS["payload"]]
    table = sections[SECTIONS["table"]]
    mapping_sections = [inventory[1], payload[1], table[1], pointer_storage]
    for label, section, allow_relro in zip(
        ("inventory", "payload", "CPython table", "PyImport_FrozenModules storage"),
        mapping_sections,
        (False, False, True, True),
    ):
        require_readonly_mapping(section, programs, label, allow_relro=allow_relro)
    require_disjoint(mapping_sections)
    if len(table[2]) != (len(records) + 1) * 32:
        fail("actual CPython _frozen table has the wrong row count")
    for index, (_name, flags, offset, size, name_offset) in enumerate(records):
        _name_pointer, _code_pointer, table_size, is_package, get_code = struct.unpack_from(
            "<QQIIQ", table[2], index * 32
        )
        row_address = table[1][3] + index * 32
        if (
            relocations.get(row_address) != (8, inventory[1][3] + name_offset)
            or relocations.get(row_address + 8) != (8, payload[1][3] + offset)
            or any(row_address + field in relocations for field in (16, 20, 24))
            or table_size != size
            or is_package != (flags & 1)
            or get_code != 0
            or size > 0x7FFFFFFF
        ):
            fail("actual CPython _frozen row differs from inventory/payload")
    if any(table[2][-32:]):
        fail("actual CPython _frozen table lacks its zero sentinel")
    sentinel_address = table[1][3] + len(records) * 32
    if any(sentinel_address + field in relocations for field in (0, 8, 16, 24)):
        fail("actual CPython frozen-table sentinel is relocation-targeted")
    for location in relocations:
        if not (
            location + 8 <= table[1][3]
            or table[1][3] + table[1][5] <= location
        ):
            relative = location - table[1][3]
            if relative // 32 >= len(records) or relative % 32 not in {0, 8}:
                fail("an unexpected relocation overlaps the CPython frozen table")


def verify_signature(
    attestation: bytes,
    *,
    python_raw: bytes,
    attestation_offset: int,
    inventory_raw: bytes,
    payload_raw: bytes,
    provenance_raw: bytes,
    review_key: bytes,
    openssl: Path,
) -> dict[str, str]:
    if len(attestation) != 236 or attestation[:8] != b"FS2ATT1\0" or struct.unpack_from("<I", attestation, 8)[0] != 1:
        fail("review attestation has an unsupported exact binary format")
    if hashlib.sha256(review_key).digest() != attestation[12:44]:
        fail("review attestation is not bound to the external reviewer key")
    normalized_artifact = python_raw[:attestation_offset] + python_raw[attestation_offset + len(attestation) :]
    observed = (
        hashlib.sha256(normalized_artifact).digest(),
        hashlib.sha256(inventory_raw).digest(),
        hashlib.sha256(payload_raw).digest(),
        hashlib.sha256(provenance_raw).digest(),
    )
    expected = (attestation[44:76], attestation[76:108], attestation[108:140], attestation[140:172])
    if observed != expected:
        fail("review attestation does not bind the normalized artifact and closure")
    key = bytes.fromhex("302a300506032b6570032100") + review_key
    descriptors = [os.memfd_create(name, os.MFD_CLOEXEC) for name in ("key", "payload", "signature")]
    try:
        for descriptor, content in zip(descriptors, (key, attestation[:172], attestation[172:236])):
            os.write(descriptor, content)
            os.lseek(descriptor, 0, os.SEEK_SET)
        result = subprocess.run(
            [str(openssl), "pkeyutl", "-verify", "-pubin", "-keyform", "DER", "-inkey", f"/proc/self/fd/{descriptors[0]}", "-rawin", "-in", f"/proc/self/fd/{descriptors[1]}", "-sigfile", f"/proc/self/fd/{descriptors[2]}"],
            env={
                "HOME": "/nonexistent",
                "PATH": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "OPENSSL_CONF": "/dev/null",
                "OPENSSL_MODULES": "/nonexistent",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            pass_fds=tuple(descriptors),
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if result.returncode != 0:
        fail("frozen-runtime review signature is invalid")
    return {
        "artifact_sha256": observed[0].hex(),
        "inventory_sha256": observed[1].hex(),
        "payload_sha256": observed[2].hex(),
        "provenance_sha256": observed[3].hex(),
        "attestation_sha256": hashlib.sha256(attestation).hexdigest(),
    }


def read_review_key() -> bytes:
    for parent in (Path("/etc"), Path("/etc/fs2")):
        details = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
            fail("frozen-runtime reviewer key parent is not protected")
    details = os.stat(REVIEW_KEY, follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != 0 or details.st_gid != 0 or stat.S_IMODE(details.st_mode) != 0o444 or details.st_size != 32:
        fail("frozen-runtime reviewer key is not root-owned mode 0444")
    return read_regular(REVIEW_KEY, "frozen-runtime reviewer key", 32)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--expected-python-sha256", required=True)
    args = parser.parse_args()
    review_key, _openssl_raw = verifier_authority()
    python_raw = read_regular(args.python, "frozen Python")
    if hashlib.sha256(python_raw).hexdigest() != digest(
        args.expected_python_sha256, "expected frozen Python digest"
    ):
        fail("candidate Python differs from the packaging-request digest")
    sections, headers, programs = elf_sections(python_raw)
    required = {key: sections.get(name) for key, name in SECTIONS.items()}
    if any(value is None for value in required.values()):
        fail("frozen runtime omits an exact inventory/payload/provenance/attestation section")
    inventory_raw = required["inventory"][2]
    payload_raw = required["payload"][2]
    provenance_raw = required["provenance"][2]
    attestation_raw = required["attestation"][2]
    records = verify_inventory(inventory_raw, payload_raw)
    relocations = verify_static_pie(python_raw, sections, programs)
    pointer_authorities, builtin_authority, builtin_table_address, builtin_table_size = require_linkage(
        python_raw, sections, headers, relocations
    )
    core_authorities = [
        required["inventory"][1],
        required["payload"][1],
        required["table"][1],
    ]
    require_authority_storage_disjoint(
        core_authorities,
        [*pointer_authorities, builtin_authority],
    )
    verify_cpython_table(
        python_raw,
        sections,
        records,
        programs,
        pointer_authorities[0][0],
        relocations,
    )
    for index, (pointer_storage, _file_start, _virtual_start, _size) in enumerate(
        pointer_authorities
    ):
        require_readonly_mapping(
            pointer_storage,
            programs,
            f"CPython frozen pointer storage {index}",
            allow_relro=True,
        )
    require_readonly_mapping(
        builtin_authority[0],
        programs,
        "PyImport_Inittab array storage",
        allow_relro=True,
    )
    builtin_names = verify_builtin_table(
        python_raw,
        programs,
        relocations,
        builtin_table_address,
        builtin_table_size,
    )
    names = [record[0] for record in records]
    if set(names) & builtin_names:
        fail("a module is ambiguously executable through frozen and builtin tables")
    provenance = exact(canonical_json(provenance_raw, "build provenance"), {"schema", "source_repository", "source_commit", "source_tree", "source_archive_sha256", "compiler_sha256", "linker_sha256", "build_recipe_sha256", "runtime_patch_sha256", "inventory_sha256", "payload_sha256", "module_count", "reproducible_build_sha256", "entrypoint_closures", "entrypoint_source_sha256"}, "build provenance")
    if provenance["schema"] != "fs2-serve.nebius.ai/frozen-runtime-build-provenance/v1" or provenance["inventory_sha256"] != hashlib.sha256(inventory_raw).hexdigest() or provenance["payload_sha256"] != hashlib.sha256(payload_raw).hexdigest() or provenance["module_count"] != len(names):
        fail("build provenance does not bind the complete frozen closure")
    for key in ("source_archive_sha256", "compiler_sha256", "linker_sha256", "build_recipe_sha256", "runtime_patch_sha256", "reproducible_build_sha256"):
        digest(provenance[key], f"provenance {key}")
    entrypoints = {
        "capsule-bootstrap",
        "capsule-installer",
        "frozen-runtime-verifier",
        "inference-stack",
        "internal-edge-acceptance",
        "internal-edge-proxy",
        "public-edge-node-verifier",
    }
    closures = exact(
        provenance["entrypoint_closures"], entrypoints, "entrypoint closures"
    )
    source_digests = exact(
        provenance["entrypoint_source_sha256"],
        entrypoints,
        "entrypoint source digests",
    )
    for entrypoint in sorted(entrypoints):
        closure = closures[entrypoint]
        if (
            not isinstance(closure, list)
            or closure != sorted(set(closure))
            or closure != names
        ):
            fail(
                f"{entrypoint} is not bound to the complete executable frozen closure"
            )
        digest(source_digests[entrypoint], f"{entrypoint} source digest")
    result = verify_signature(
        attestation_raw,
        python_raw=python_raw,
        attestation_offset=required["attestation"][1][4],
        inventory_raw=inventory_raw,
        payload_raw=payload_raw,
        provenance_raw=provenance_raw,
        review_key=review_key,
        openssl=OPENSSL_PATH,
    )
    print(json.dumps({"verdict": "PASS", **result}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(f"frozen-runtime verification failed: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1) from exc
