"""Check whether local tools required by overleaf-agent are available."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from overleaf_agent.shell import CommandResult, run_command  # noqa: E402


@dataclass(frozen=True)
class ToolCheck:
    name: str
    command: tuple[str, ...]


TOOLS = (
    ToolCheck("perl", ("perl", "-v")),
    ToolCheck("miktex", ("miktex", "--version")),
    ToolCheck("latexmk", ("latexmk", "-v")),
    ToolCheck("pdflatex", ("pdflatex", "--version")),
    ToolCheck("xelatex", ("xelatex", "--version")),
    ToolCheck("biber", ("biber", "--version")),
)

VERSION_LINE_HINTS = {
    "perl": ("this is perl",),
    "miktex": ("miktex",),
    "latexmk": ("latexmk",),
    "pdflatex": ("pdftex", "pdflatex"),
    "xelatex": ("xetex", "xelatex"),
    "biber": ("biber",),
}


def main() -> int:
    print("Checking local environment...")
    print()

    results: list[tuple[ToolCheck, CommandResult]] = []
    for tool in TOOLS:
        result = run_command(tool.command, cwd=REPO_ROOT, timeout=20)
        results.append((tool, result))
        _print_result(tool, result)

    available = sum(1 for _, result in results if result.ok)
    total = len(results)

    print()
    print(f"Summary: {available}/{total} tools available.")
    if available == total:
        print("Environment looks ready.")
        return 0

    print("Environment is missing required tools.")
    return 1


def _print_result(tool: ToolCheck, result: CommandResult) -> None:
    if result.ok:
        version_line = _first_output_line(result)
        print(f"[OK] {tool.name}: {version_line}")
        return

    print(f"[FAIL] {tool.name}")
    print(f"  command: {' '.join(result.command)}")
    print(f"  returncode: {result.returncode}")
    print(f"  stderr: {_failure_message(result)}")


def _first_output_line(result: CommandResult) -> str:
    output = result.stdout.strip() or result.stderr.strip()
    hints = VERSION_LINE_HINTS.get(result.command[0], ())
    for line in output.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if stripped and any(hint in lower for hint in hints):
            return stripped

    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped

    return "(no output)"


def _failure_message(result: CommandResult) -> str:
    if result.not_found:
        return f"Command not found: {result.command[0]}"

    if result.timed_out:
        return result.stderr.strip() or "Command timed out."

    return result.stderr.strip() or result.stdout.strip() or "(no error output)"


if __name__ == "__main__":
    raise SystemExit(main())
