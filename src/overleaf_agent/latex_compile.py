"""Compile local LaTeX projects with latexmk."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from overleaf_agent.shell import run_command


SUPPORTED_COMPILERS = {
    "pdflatex": "-pdf",
    "xelatex": "-xelatex",
}


@dataclass(frozen=True)
class LatexCompileResult:
    """Structured result for a LaTeX compilation run."""

    success: bool
    project_dir: str
    main_tex: str
    compiler: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    log_path: str
    pdf_path: str
    log_exists: bool
    pdf_exists: bool


def compile_latex(
    project_dir: str | Path,
    main_tex: str | Path,
    *,
    compiler: str = "pdflatex",
    timeout: float | None = 120,
) -> LatexCompileResult:
    """Compile a LaTeX project using latexmk.

    The command runs with ``project_dir`` as its working directory so relative
    paths behave like they do in Overleaf.
    """

    project_path = _validate_project_dir(project_dir)
    main_tex_path = _validate_main_tex(project_path, main_tex)
    compiler_flag = _compiler_flag(compiler)

    command = (
        "latexmk",
        compiler_flag,
        "-interaction=nonstopmode",
        "-file-line-error",
        str(Path(main_tex)),
    )
    result = run_command(command, cwd=project_path, timeout=timeout)

    log_path = main_tex_path.with_suffix(".log")
    pdf_path = main_tex_path.with_suffix(".pdf")

    return LatexCompileResult(
        success=result.returncode == 0,
        project_dir=str(project_path),
        main_tex=str(Path(main_tex)),
        compiler=compiler,
        command=result.command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        log_path=str(log_path),
        pdf_path=str(pdf_path),
        log_exists=log_path.exists(),
        pdf_exists=pdf_path.exists(),
    )


def _validate_project_dir(project_dir: str | Path) -> Path:
    project_path = Path(project_dir).expanduser().resolve()
    if not project_path.exists():
        raise FileNotFoundError(f"project_dir does not exist: {project_path}")

    if not project_path.is_dir():
        raise NotADirectoryError(f"project_dir is not a directory: {project_path}")

    return project_path


def _validate_main_tex(project_dir: Path, main_tex: str | Path) -> Path:
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


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False

    return True


def _compiler_flag(compiler: str) -> str:
    try:
        return SUPPORTED_COMPILERS[compiler]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_COMPILERS))
        raise ValueError(f"unsupported compiler: {compiler}. Expected one of: {supported}") from exc
