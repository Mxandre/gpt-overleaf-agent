"""Utilities for running local commands.

This module is intentionally low-level. It does not know whether a command is
LaTeX, MiKTeX, or anything else; it only executes the process and returns a
structured result.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Sequence


COMMAND_NOT_FOUND_RETURN_CODE = 127
TIMEOUT_RETURN_CODE = 124


@dataclass(frozen=True)
class CommandResult:
    """Structured result returned by :func:`run_command`."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    cwd: str | None
    timed_out: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        """Return True when the command completed successfully."""

        return self.returncode == 0


def run_command(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = 60,
) -> CommandResult:
    """Run a command and return stdout, stderr, return code, and status flags.

    Args:
        command: Command and arguments, for example ``["latexmk", "-v"]``.
        cwd: Optional working directory for the command.
        timeout: Optional timeout in seconds. Use ``None`` for no timeout.

    Returns:
        A ``CommandResult``. Missing commands and timeouts are converted into
        normal result objects so callers do not need exception handling for the
        common failure modes.

    Raises:
        ValueError: If ``command`` is empty.
    """

    normalized_command = _normalize_command(command)
    normalized_cwd = _normalize_cwd(cwd)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            normalized_command,
            cwd=normalized_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_command_env(),
            creationflags=creationflags,
        )
        stdout, stderr = process.communicate(timeout=timeout)
    except FileNotFoundError as exc:
        return CommandResult(
            command=normalized_command,
            returncode=COMMAND_NOT_FOUND_RETURN_CODE,
            stdout="",
            stderr=str(exc),
            cwd=normalized_cwd,
            not_found=True,
        )
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _kill_process_tree(process)

        try:
            stdout_after_kill, stderr_after_kill = process.communicate(timeout=0.5) if process is not None else ("", "")
        except subprocess.TimeoutExpired:
            stdout_after_kill, stderr_after_kill = _to_text(exc.stdout), _to_text(exc.stderr)

        stdout = stdout_after_kill or _to_text(exc.stdout)
        stderr = stderr_after_kill or _to_text(exc.stderr)
        if stderr:
            stderr = f"{stderr}\nCommand timed out after {timeout} seconds."
        else:
            stderr = f"Command timed out after {timeout} seconds."

        return CommandResult(
            command=normalized_command,
            returncode=TIMEOUT_RETURN_CODE,
            stdout=stdout,
            stderr=stderr,
            cwd=normalized_cwd,
            timed_out=True,
        )

    return CommandResult(
        command=normalized_command,
        returncode=process.returncode if process is not None else 1,
        stdout=stdout,
        stderr=stderr,
        cwd=normalized_cwd,
    )


def _normalize_command(command: Sequence[str]) -> tuple[str, ...]:
    if not command:
        raise ValueError("command must not be empty")

    return tuple(str(part) for part in command)


def _normalize_cwd(cwd: str | Path | None) -> str | None:
    if cwd is None:
        return None

    return str(Path(cwd))


def _to_text(value: bytes | str | None) -> str:
    if value is None:
        return ""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    return value


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    if os.name == "nt":
        env.setdefault("SystemRoot", r"C:\Windows")
        env.setdefault("WINDIR", env["SystemRoot"])
        env.setdefault("ComSpec", str(Path(env["SystemRoot"]) / "System32" / "cmd.exe"))
        env.setdefault("TEMP", os.environ.get("TEMP", str(Path.home() / "AppData" / "Local" / "Temp")))
        env.setdefault("TMP", env["TEMP"])
        env.setdefault("USERPROFILE", str(Path.home()))
        env.setdefault("HOME", env["USERPROFILE"])

    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    return env


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ("taskkill", "/F", "/T", "/PID", str(process.pid)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        return

    process.kill()
