"""Build a lightweight structural index for local LaTeX projects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


INPUT_INCLUDE_RE = re.compile(r"\\(?:input|include)\s*\{(?P<path>[^}]+)\}")
BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{(?P<paths>[^}]+)\}")
ADDBIBRESOURCE_RE = re.compile(r"\\addbibresource(?:\[[^\]]*\])?\s*\{(?P<path>[^}]+)\}")

FIGURE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"}
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
IGNORED_FILE_SUFFIXES = {
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
    ".bak",
}


@dataclass(frozen=True)
class MainTexDetectionResult:
    """Result of trying to infer the main LaTeX file."""

    main_tex: str | None
    candidates: list[str]
    reason: str


@dataclass(frozen=True)
class ProjectIndex:
    """A structural index of a LaTeX project."""

    project_dir: str
    main_tex: str
    tex_files: list[str]
    included_tex_files: list[str]
    bib_files: list[str]
    figure_files: list[str]
    table_files: list[str]
    other_files: list[str]


def index_project(
    project_dir: str | Path,
    main_tex: str | Path | None = None,
) -> ProjectIndex:
    """Scan a LaTeX project and return a structural index."""

    project_path = _validate_project_dir(project_dir)
    all_files = _list_project_files(project_path)
    tex_files = sorted(_relative(file, project_path) for file in all_files if file.suffix == ".tex")

    main_tex_path = _resolve_main_tex(project_path, tex_files, main_tex)
    main_text = _read_text(main_tex_path)

    included_tex_files = _extract_included_tex_files(main_text)
    bib_files = _extract_bib_files(project_path, main_text, all_files)
    figure_files = _classify_figure_files(project_path, all_files)
    table_files = _classify_table_files(project_path, all_files)

    known = set(tex_files) | set(bib_files) | set(figure_files) | set(table_files)
    other_files = sorted(
        _relative(file, project_path)
        for file in all_files
        if _relative(file, project_path) not in known
    )

    return ProjectIndex(
        project_dir=str(project_path),
        main_tex=_relative(main_tex_path, project_path),
        tex_files=tex_files,
        included_tex_files=included_tex_files,
        bib_files=bib_files,
        figure_files=figure_files,
        table_files=table_files,
        other_files=other_files,
    )


def detect_main_tex(project_dir: str | Path) -> MainTexDetectionResult:
    """Infer a project's main TeX file.

    Rules:
    1. Return root ``main.tex`` when it exists.
    2. Scan all ``.tex`` files.
    3. Select files containing both ``\\documentclass`` and ``\\begin{document}``.
    4. Return the file when exactly one candidate is found.
    5. Return candidates when multiple files match so the caller can ask the user.
    """

    project_path = _validate_project_dir(project_dir)
    all_files = _list_project_files(project_path)
    tex_files = sorted(file for file in all_files if file.suffix == ".tex")

    root_main = project_path / "main.tex"
    if root_main.exists() and root_main.is_file():
        return MainTexDetectionResult(
            main_tex=_relative(root_main.resolve(), project_path),
            candidates=[_relative(root_main.resolve(), project_path)],
            reason="found root main.tex",
        )

    candidates = [
        _relative(file, project_path)
        for file in tex_files
        if _looks_like_main_tex(file)
    ]

    if len(candidates) == 1:
        return MainTexDetectionResult(
            main_tex=candidates[0],
            candidates=candidates,
            reason="found exactly one .tex file containing \\documentclass and \\begin{document}",
        )

    if len(candidates) > 1:
        return MainTexDetectionResult(
            main_tex=None,
            candidates=candidates,
            reason="multiple .tex files contain \\documentclass and \\begin{document}; specify main_tex",
        )

    return MainTexDetectionResult(
        main_tex=None,
        candidates=[],
        reason="no .tex file contains both \\documentclass and \\begin{document}",
    )


def _validate_project_dir(project_dir: str | Path) -> Path:
    project_path = Path(project_dir).expanduser().resolve()
    if not project_path.exists():
        raise FileNotFoundError(f"project_dir does not exist: {project_path}")

    if not project_path.is_dir():
        raise NotADirectoryError(f"project_dir is not a directory: {project_path}")

    return project_path


def _list_project_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in project_dir.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.relative_to(project_dir).parts):
            continue
        if _is_ignored_generated_file(path):
            continue
        if path.is_file():
            files.append(path)

    return files


def _resolve_main_tex(
    project_dir: Path,
    tex_files: list[str],
    main_tex: str | Path | None,
) -> Path:
    if main_tex is not None:
        main_tex_path = Path(main_tex)
        if main_tex_path.is_absolute():
            raise ValueError("main_tex must be relative to project_dir")

        resolved = (project_dir / main_tex_path).resolve()
        if not _is_relative_to(resolved, project_dir):
            raise ValueError("main_tex must be inside project_dir")

        if not resolved.exists():
            raise FileNotFoundError(f"main_tex does not exist: {resolved}")
        if not resolved.is_file():
            raise FileNotFoundError(f"main_tex is not a file: {resolved}")
        return resolved

    if not tex_files:
        raise FileNotFoundError(f"no .tex files found in project_dir: {project_dir}")

    detection = detect_main_tex(project_dir)
    if detection.main_tex is not None:
        return (project_dir / detection.main_tex).resolve()

    if detection.candidates:
        candidates = ", ".join(detection.candidates)
        raise FileNotFoundError(
            f"could not infer main_tex because multiple candidates were found: {candidates}. "
            "Pass main_tex explicitly."
        )

    raise FileNotFoundError(
        "could not infer main_tex. Pass main_tex explicitly, add a root main.tex, "
        "or use a .tex file containing both \\documentclass and \\begin{document}"
    )


def _extract_included_tex_files(main_text: str) -> list[str]:
    included: list[str] = []
    for match in INPUT_INCLUDE_RE.finditer(main_text):
        rel_path = _normalize_tex_reference(match.group("path"))
        included.append(_normalize_relative_string(rel_path))

    return sorted(dict.fromkeys(included))


def _extract_bib_files(project_dir: Path, main_text: str, all_files: list[Path]) -> list[str]:
    bib_files = {_relative(file, project_dir) for file in all_files if file.suffix == ".bib"}

    for match in BIBLIOGRAPHY_RE.finditer(main_text):
        for raw_path in match.group("paths").split(","):
            path = raw_path.strip()
            if path:
                bib_files.add(_normalize_relative_string(_ensure_suffix(Path(path), ".bib")))

    for match in ADDBIBRESOURCE_RE.finditer(main_text):
        path = match.group("path").strip()
        if path:
            bib_files.add(_normalize_relative_string(_ensure_suffix(Path(path), ".bib")))

    return sorted(bib_files)


def _classify_figure_files(project_dir: Path, all_files: list[Path]) -> list[str]:
    figure_files: list[str] = []
    for file in all_files:
        rel = file.relative_to(project_dir)
        parts = {part.lower() for part in rel.parts}
        if file.suffix.lower() in FIGURE_EXTENSIONS and (
            "figures" in parts or "figure" in parts or "images" in parts or "img" in parts
        ):
            figure_files.append(_relative(file, project_dir))

    return sorted(figure_files)


def _classify_table_files(project_dir: Path, all_files: list[Path]) -> list[str]:
    table_files: list[str] = []
    for file in all_files:
        rel = file.relative_to(project_dir)
        parts = {part.lower() for part in rel.parts}
        if "tables" in parts or "table" in parts:
            table_files.append(_relative(file, project_dir))

    return sorted(table_files)


def _normalize_tex_reference(reference: str) -> Path:
    path = Path(reference.strip())
    return _ensure_suffix(path, ".tex")


def _ensure_suffix(path: Path, suffix: str) -> Path:
    if path.suffix:
        return path

    return path.with_suffix(suffix)


def _normalize_relative_string(path: Path) -> str:
    return path.as_posix()


def _relative(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False

    return True


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="replace")


def _looks_like_main_tex(path: Path) -> bool:
    text = _read_text(path)
    return "\\documentclass" in text and "\\begin{document}" in text


def _is_ignored_generated_file(path: Path) -> bool:
    name = path.name.lower()
    if any(name.endswith(suffix) for suffix in IGNORED_FILE_SUFFIXES):
        return True

    return path.suffix.lower() == ".pdf" and path.with_suffix(".tex").exists()
