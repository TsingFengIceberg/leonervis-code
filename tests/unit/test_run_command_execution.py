from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import leonervis_code.tools.run_command as run_command_module
from leonervis_code.core.contracts import ToolArguments, ToolUse
from leonervis_code.tools.run_command import (
    MAX_COMMAND_STDOUT_BYTES,
    RunCommandOutcome,
    RunCommandTool,
)


def command(argv: list[str], *, cwd: str = ".", timeout: int = 10) -> ToolUse:
    return ToolUse(
        "command-1",
        "run_command",
        ToolArguments.from_mapping({"argv": argv, "cwd": cwd, "timeout_seconds": timeout}),
    )


def payload(result) -> dict[str, object]:
    return json.loads(result.tool_result.content)


def test_direct_execution_captures_stdout_stderr_and_literal_metacharacters(
    tmp_path: Path,
) -> None:
    tool = RunCommandTool(tmp_path, environment={"PATH": "/usr/bin", "SECRET": "hidden"})
    prepared = tool.prepare(
        command(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1]); print('err', file=sys.stderr)",
                "$(touch should-not-exist) | *",
            ]
        )
    )

    result = tool.execute_detailed(prepared)
    data = payload(result)

    assert result.outcome == RunCommandOutcome.SUCCEEDED
    assert result.result_code == "command_succeeded"
    assert data["status"] == "exited"
    assert data["exit_code"] == 0
    assert data["stdout"]["text"] == "$(touch should-not-exist) | *\n"
    assert data["stderr"]["text"] == "err\n"
    assert not (tmp_path / "should-not-exist").exists()


def test_environment_is_closed_and_pwd_tracks_relative_cwd(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    tool = RunCommandTool(
        tmp_path,
        environment={"PATH": "/usr/bin", "VISIBLE": "no", "LANG": "C.UTF-8"},
    )
    code = "import os; print(os.environ.get('VISIBLE')); print(os.environ['PWD'])"

    result = tool.execute_detailed(
        tool.prepare(command([sys.executable, "-c", code], cwd="nested"))
    )
    data = payload(result)

    assert data["stdout"]["text"] == f"None\n{tmp_path / 'nested'}\n"


def test_nonzero_and_missing_executable_are_structured_failures(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path)

    nonzero = tool.execute_detailed(
        tool.prepare(command([sys.executable, "-c", "raise SystemExit(7)"]))
    )
    missing = tool.execute_detailed(
        tool.prepare(command(["definitely-not-a-leonervis-executable"]))
    )

    assert nonzero.outcome == RunCommandOutcome.FAILED
    assert nonzero.result_code == "command_exited_nonzero"
    assert payload(nonzero)["exit_code"] == 7
    assert missing.outcome == RunCommandOutcome.FAILED
    assert missing.result_code == "command_spawn_failed"
    assert payload(missing)["status"] == "spawn-failed"


def test_execute_rechecks_cwd_at_the_spawn_boundary(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    outside = tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    marker = outside / "must-not-exist.txt"
    tool = RunCommandTool(tmp_path)
    prepared = tool.prepare(
        command(
            [sys.executable, "-c", "from pathlib import Path; Path('must-not-exist.txt').touch()"],
            cwd="cwd",
        )
    )
    cwd.rmdir()
    cwd.symlink_to(outside, target_is_directory=True)

    result = tool.execute_detailed(prepared)
    data = payload(result)

    assert result.outcome == RunCommandOutcome.FAILED
    assert result.result_code == "command_cwd_invalid"
    assert data["status"] == "spawn-rejected"
    assert data["cleanup_complete"] is True
    assert not marker.exists()


def test_nonzero_exit_with_cleanup_uncertainty_is_partial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_command_module, "_join_readers", lambda readers, timeout: False)
    monkeypatch.setattr(
        RunCommandTool,
        "_terminate_process_group",
        staticmethod(lambda process: False),
    )
    tool = RunCommandTool(tmp_path)

    result = tool.execute_detailed(
        tool.prepare(command([sys.executable, "-c", "raise SystemExit(7)"]))
    )
    data = payload(result)

    assert result.outcome == RunCommandOutcome.PARTIAL
    assert result.result_code == "command_cleanup_incomplete"
    assert data["status"] == "cleanup-incomplete"
    assert data["exit_code"] == 7
    assert data["cleanup_complete"] is False


def test_output_is_bounded_and_invalid_utf8_uses_base64(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path)
    code = (
        f"import os; os.write(1, b'x' * {MAX_COMMAND_STDOUT_BYTES + 100}); os.write(2, b'\\xffbad')"
    )

    result = tool.execute_detailed(tool.prepare(command([sys.executable, "-c", code])))
    data = payload(result)

    assert result.tool_result.truncated
    assert data["stdout"]["bytes_captured"] == MAX_COMMAND_STDOUT_BYTES
    assert data["stdout"]["bytes_total"] == MAX_COMMAND_STDOUT_BYTES + 100
    assert data["stdout"]["truncated"] is True
    assert data["stderr"]["encoding"] == "base64"
    assert data["stderr"]["base64"] == "/2JhZA=="


def test_timeout_terminates_process_group_and_prevents_child_late_effect(tmp_path: Path) -> None:
    marker = tmp_path / "late.txt"
    child = "import pathlib,time; time.sleep(2); pathlib.Path('late.txt').write_text('late')"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(10)"
    )
    tool = RunCommandTool(tmp_path)

    result = tool.execute_detailed(tool.prepare(command([sys.executable, "-c", parent], timeout=1)))
    data = payload(result)

    assert result.outcome == RunCommandOutcome.PARTIAL
    assert result.result_code == "command_timed_out"
    assert data["status"] == "timed-out"
    assert data["cleanup_complete"] is True
    time.sleep(1.5)
    assert not marker.exists()


def test_normal_parent_exit_cleans_background_child_that_keeps_pipes_open(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "late.txt"
    child = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(4); pathlib.Path('late.txt').write_text('late')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(0.2)"
    )
    tool = RunCommandTool(tmp_path)

    started = time.monotonic()
    result = tool.execute_detailed(tool.prepare(command([sys.executable, "-c", parent], timeout=5)))
    elapsed = time.monotonic() - started
    data = payload(result)

    assert elapsed < 4
    assert result.outcome == RunCommandOutcome.SUCCEEDED
    assert result.result_code == "command_succeeded"
    assert data["cleanup_complete"] is True
    time.sleep(0.5)
    assert not marker.exists()


def test_keyboard_interrupt_cancels_and_cleans_process_group(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    original_wait = subprocess.Popen.wait
    first_wait = True

    def interrupt_once(process, timeout=None):
        nonlocal first_wait
        if first_wait:
            first_wait = False
            raise KeyboardInterrupt
        return original_wait(process, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupt_once)
    tool = RunCommandTool(tmp_path)

    result = tool.execute_detailed(
        tool.prepare(command([sys.executable, "-c", "import time; time.sleep(10)"]))
    )
    data = payload(result)

    assert result.outcome == RunCommandOutcome.PARTIAL
    assert result.result_code == "command_cancelled"
    assert data["status"] == "cancelled"
    assert data["cleanup_complete"] is True
