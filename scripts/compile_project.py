"""Compile a LaTeX project and summarize errors from its log file."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from overleaf_agent.latex_compile import compile_latex  # noqa: E402
from overleaf_agent.log_parser import LatexLogIssue, parse_latex_log  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    print("Compiling LaTeX project...")
    print()
    print(f"Project: {args.project}")
    print(f"Main: {args.main}")
    print(f"Compiler: {args.compiler}")
    print()

    try:
        compile_result = compile_latex(
            project_dir=args.project,
            main_tex=args.main,
            compiler=args.compiler,
            timeout=args.timeout,
        )
    except Exception as exc:
        print("[FAIL] Could not start compilation.")
        print()
        print(f"{type(exc).__name__}: {exc}")
        return 2

    if compile_result.success:
        print("[OK] Compilation succeeded.")
        print()
        print(f"PDF: {compile_result.pdf_path}")
        print(f"LOG: {compile_result.log_path}")
        return 0

    print("[FAIL] Compilation failed.")
    print()
    print(f"Return code: {compile_result.returncode}")
    print(f"PDF: {compile_result.pdf_path}")
    print(f"LOG: {compile_result.log_path}")
    print()

    _print_log_summary(compile_result.log_path)
    return 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile a LaTeX project and parse its log on failure.",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="LaTeX project directory.",
    )
    parser.add_argument(
        "--main",
        default="main.tex",
        help="Main TeX file relative to the project directory. Default: main.tex.",
    )
    parser.add_argument(
        "--compiler",
        choices=("pdflatex", "xelatex"),
        default="pdflatex",
        help="LaTeX compiler to use. Default: pdflatex.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="Compilation timeout in seconds. Default: 120.",
    )
    return parser.parse_args(argv)


def _print_log_summary(log_path: str) -> None:
    path = Path(log_path)
    if not path.exists():
        print("No log file was generated, so no LaTeX errors could be parsed.")
        return

    parse_result = parse_latex_log(path)
    if not parse_result.errors:
        print("No structured LaTeX errors were detected in the log.")
        if parse_result.warnings:
            print()
            _print_issues("Detected warnings:", parse_result.warnings)
        return

    _print_issues("Detected errors:", parse_result.errors)
    if parse_result.warnings:
        print()
        _print_issues("Detected warnings:", parse_result.warnings)


def _print_issues(title: str, issues: list[LatexLogIssue]) -> None:
    print(title)
    print()
    for index, issue in enumerate(issues, start=1):
        print(f"{index}. {issue.kind}")
        if issue.file:
            print(f"   File: {issue.file}")
        if issue.line is not None:
            print(f"   Line: {issue.line}")
        print(f"   Message: {issue.message}")
        if issue.context:
            print()
            print("   Context:")
            for line in issue.context.splitlines():
                print(f"   {line}")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
