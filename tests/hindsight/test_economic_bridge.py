from __future__ import annotations

import subprocess
from pathlib import Path

from hindsight import cli
from hindsight.economic import run_native


def test_missing_native_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HINDSIGHT_NATIVE_BIN", raising=False)
    assert run_native(["demo"], repo_root=tmp_path) == 2
    assert "cargo build --locked" in capsys.readouterr().err


def test_native_arguments_are_not_shell_interpolated(tmp_path, monkeypatch):
    binary = tmp_path / "engine"
    binary.write_text("test")
    monkeypatch.setenv("HINDSIGHT_NATIVE_BIN", str(binary))
    calls = []

    def execute(args, *, check):
        calls.append(args)
        assert check is False
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_native(["demo", "path with spaces; literal"]) == 7
    assert calls == [[str(binary), "demo", "path with spaces; literal"]]


def test_native_empty_arguments_request_help(tmp_path, monkeypatch):
    binary = tmp_path / "engine"
    binary.write_text("test")
    monkeypatch.setenv("HINDSIGHT_NATIVE_BIN", str(binary))

    def execute(args, *, check):
        assert args[-1] == "--help"
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_native([]) == 0


def test_native_execution_failure_is_explicit(tmp_path, monkeypatch, capsys):
    binary = tmp_path / "engine"
    binary.write_text("test")
    monkeypatch.setenv("HINDSIGHT_NATIVE_BIN", str(binary))

    def execute(*args, **kwargs):
        raise OSError("not executable")

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_native(["demo"]) == 2
    assert "not executable" in capsys.readouterr().err


def test_existing_cli_routes_without_changing_legacy_commands(monkeypatch):
    import hindsight.economic

    seen = []
    monkeypatch.setattr(hindsight.economic, "run_native", lambda args: seen.extend(args) or 0)
    assert cli.main(["economic", "demo", "reports/new-run"]) == 0
    assert seen == ["demo", "reports/new-run"]
    assert cli.build_parser().parse_args(["demo"]).command == "demo"


def test_default_binary_location(tmp_path, monkeypatch):
    monkeypatch.delenv("HINDSIGHT_NATIVE_BIN", raising=False)
    binary = tmp_path / "target" / "debug" / "hindsight-economic"
    binary.parent.mkdir(parents=True)
    binary.write_text("test")
    monkeypatch.setattr(subprocess, "run", lambda args, check: subprocess.CompletedProcess(args, 0))
    assert run_native(["help"], repo_root=Path(tmp_path)) == 0
