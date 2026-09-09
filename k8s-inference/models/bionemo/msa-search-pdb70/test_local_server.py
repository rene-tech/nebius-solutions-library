from __future__ import annotations

import runpy
from pathlib import Path


def test_short_valid_search_result_is_successful(monkeypatch) -> None:
    module = runpy.run_path(str(Path(__file__).with_name("local_server.py")))
    commands: list[list[str]] = []

    def run(command, *, cwd, **_kwargs):
        commands.append(command)
        if command[1] == "unpackdb":
            (Path(cwd) / "unpack" / "0.a3m").write_text(
                ">query\nACDEFG\n>one-homolog\nAC-EFG\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(module["subprocess"], "run", run)
    result = module["_search_local"]("ACDEFG", 500)
    assert result == ">query\nACDEFG\n>one-homolog\nAC-EFG\n"
    search = next(command for command in commands if command[1] == "search")
    assert search[search.index("--max-seqs") + 1] == "128"


def test_requested_alignment_count_is_an_upper_bound(monkeypatch) -> None:
    module = runpy.run_path(str(Path(__file__).with_name("local_server.py")))

    def run(command, *, cwd, **_kwargs):
        if command[1] == "unpackdb":
            (Path(cwd) / "unpack" / "0.a3m").write_text(
                ">query\nACDEFG\n>first\nAC-EFG\n>second\nACD-FG\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(module["subprocess"], "run", run)
    assert module["_search_local"]("ACDEFG", 1) == ">query\nACDEFG\n"
