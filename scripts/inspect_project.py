"""Inspect a LaTeX project and print its structural index."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from overleaf_agent.project_indexer import ProjectIndex, index_project  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        project_index = index_project(args.project, main_tex=args.main)
    except Exception as exc:
        print("[FAIL] Could not inspect project.")
        print()
        print(f"{type(exc).__name__}: {exc}")
        return 1

    _print_project_index(project_index)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect a LaTeX project and print its structure.",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="LaTeX project directory.",
    )
    parser.add_argument(
        "--main",
        default=None,
        help="Main TeX file relative to the project directory. Default: infer main.tex.",
    )
    return parser.parse_args(argv)


def _print_project_index(project_index: ProjectIndex) -> None:
    print("Inspecting LaTeX project...")
    print()
    print(f"Project: {project_index.project_dir}")
    print(f"Main: {project_index.main_tex}")
    print()

    _print_list("TeX files:", project_index.tex_files)
    _print_list("Included TeX files:", project_index.included_tex_files)
    _print_list("Bibliography files:", project_index.bib_files)
    _print_list("Figure files:", project_index.figure_files)
    _print_list("Table files:", project_index.table_files)
    _print_list("Other files:", project_index.other_files)


def _print_list(title: str, values: list[str]) -> None:
    print(title)
    if values:
        for value in values:
            print(f"  - {value}")
    else:
        print("  (none)")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
