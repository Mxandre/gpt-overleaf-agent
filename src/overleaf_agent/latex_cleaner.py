"""Clean LaTeX build artifacts while keeping the generated PDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ARTIFACT_SUFFIXES = {
    ".aux",
    ".bbl",
    ".bcf",
    ".blg",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".out",
    ".run.xml",
    ".synctex.gz",
    ".toc",
}


@dataclass(frozen=True)
class LatexCleanResult:
    """Result of cleaning LaTeX build artifacts."""

    success: bool
    project_dir: str
    main_tex: str
    removed_files: list[str]
    kept_files: list[str]
    failed_files: list[str]
    message: str


def clean_latex_artifacts(
    project_dir: str | Path,
    main_tex: str | Path = "main.tex",
) -> LatexCleanResult:
    """Remove LaTeX intermediate files for ``main_tex`` and keep the PDF."""

    project_path = Path(project_dir).expanduser().resolve()
    if not project_path.exists():
        return _failure(project_path, main_tex, "project_dir does not exist")
    if not project_path.is_dir():
        return _failure(project_path, main_tex, "project_dir is not a directory")

    main_tex_path = Path(main_tex)
    if main_tex_path.is_absolute():
        return _failure(project_path, main_tex, "main_tex must be relative to project_dir")

    main_path = (project_path / main_tex_path).resolve()
    if not _is_relative_to(main_path, project_path):
        return _failure(project_path, main_tex, "main_tex must be inside project_dir")
    if not main_path.exists() or not main_path.is_file():
        return _failure(project_path, main_tex, "main_tex does not exist")

    removed_files: list[str] = []
    kept_files: list[str] = []
    failed_files: list[str] = []
    for suffix in sorted(ARTIFACT_SUFFIXES):
        artifact = main_path.with_suffix(suffix)
        if artifact.exists() and artifact.is_file():
            try:
                artifact.chmod(0o666)
                artifact.unlink()
            except OSError as exc:
                failed_files.append(f"{_relative(artifact, project_path)}: {exc}")
            else:
                removed_files.append(_relative(artifact, project_path))

    pdf_path = main_path.with_suffix(".pdf")
    if pdf_path.exists() and pdf_path.is_file():
        kept_files.append(_relative(pdf_path, project_path))

    return LatexCleanResult(
        success=not failed_files,
        project_dir=str(project_path),
        main_tex=_relative(main_path, project_path),
        removed_files=removed_files,
        kept_files=kept_files,
        failed_files=failed_files,
        message=(
            f"removed {len(removed_files)} LaTeX artifact file(s); "
            f"failed to remove {len(failed_files)} file(s); PDF kept"
        ),
    )


def _failure(project_dir: Path, main_tex: str | Path, message: str) -> LatexCleanResult:
    return LatexCleanResult(
        success=False,
        project_dir=str(project_dir),
        main_tex=str(main_tex),
        removed_files=[],
        kept_files=[],
        failed_files=[],
        message=message,
    )


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False

    return True


def _relative(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()
