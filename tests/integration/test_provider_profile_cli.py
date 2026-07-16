from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def run_cli(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    return subprocess.run(
        [sys.executable, "-m", "leonervis_code", *arguments],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )


def test_module_entry_persists_profile_lifecycle_across_processes(tmp_path) -> None:
    added = run_cli(
        tmp_path,
        "provider",
        "add",
        "local-dev",
        "--provider",
        "custom",
        "--model",
        "Qwen/Qwen3.5",
        "--protocol",
        "openai-compatible",
        "--base-url",
        "http://127.0.0.1:11434",
    )
    assert added.returncode == 0
    assert added.stdout == "Saved provider profile local-dev.\n"
    assert added.stderr == ""

    selected = run_cli(tmp_path, "provider", "use", "local-dev")
    assert selected.returncode == 0
    assert selected.stdout == "Using provider profile local-dev at project scope.\n"
    assert selected.stderr == ""

    listed = run_cli(tmp_path, "provider", "list")
    assert listed.returncode == 0
    assert listed.stdout == "local-dev *: custom/Qwen/Qwen3.5\n"
    assert listed.stderr == ""

    shown = run_cli(tmp_path, "provider", "show", "local-dev")
    assert shown.returncode == 0
    assert "base URL: http://127.0.0.1:11434/v1" in shown.stdout
    assert "credential: not required" in shown.stdout

    cleared = run_cli(tmp_path, "provider", "clear")
    assert cleared.returncode == 0
    assert cleared.stdout == "Cleared project active provider profile.\n"

    removed = run_cli(tmp_path, "provider", "remove", "local-dev")
    assert removed.returncode == 0
    assert removed.stdout == "Removed provider profile local-dev.\n"


def test_module_entry_profile_configuration_never_renders_key_value(tmp_path) -> None:
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    environment["VENDOR_API_KEY"] = "secret-must-not-render"
    added = subprocess.run(
        [
            sys.executable,
            "-m",
            "leonervis_code",
            "provider",
            "add",
            "vendor",
            "--provider",
            "custom",
            "--model",
            "vendor/model",
            "--protocol",
            "openai-compatible",
            "--base-url",
            "https://gateway.example/v1",
            "--api-key-env",
            "VENDOR_API_KEY",
        ],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert added.returncode == 0

    shown = subprocess.run(
        [sys.executable, "-m", "leonervis_code", "provider", "show", "vendor"],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert shown.returncode == 0
    assert "credential: configured" in shown.stdout
    assert "VENDOR_API_KEY" not in shown.stdout
    assert "secret-must-not-render" not in shown.stdout
    assert "secret-must-not-render" not in shown.stderr
