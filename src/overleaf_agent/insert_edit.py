"""Validate and apply line-position based text insertions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from overleaf_agent.latex_cleaner import LatexCleanResult, clean_latex_artifacts
from overleaf_agent.latex_compile import LatexCompileResult, compile_latex
from overleaf_agent.log_parser import LatexLogParseResult, parse_latex_log


@dataclass(frozen=True)
class InsertPositionValidationResult:
    """Result of validating an insertion point."""

    valid: bool
    project_dir: str
    target_file: str
    file_path: str | None
    line: int
    line_count: int
    file_hash: str | None
    before_line: str | None
    after_line: str | None
    message: str


@dataclass(frozen=True)
class InsertEditResult:
    """Result of applying an insertion and compiling the project."""

    success: bool
    status: str
    project_dir: str
    target_file: str
    file_path: str | None
    line: int
    expected_hash: str
    actual_hash: str | None
    changed: bool
    compile_result: LatexCompileResult | None
    clean_result: LatexCleanResult | None
    log_parse_result: LatexLogParseResult | None
    restored: bool
    message: str


def validate_insert_position(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    line: int,
) -> InsertPositionValidationResult:
    """Validate an insertion point.

    ``line`` means "insert after this 1-based line". Use ``line=0`` to insert
    at the beginning of the file.
    """

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)
    validation_error = _validate_common(project_path, target_path)
    if validation_error is not None:
        return _insert_position_failure(
            project_path,
            target_file,
            target_path,
            line,
            0,
            validation_error,
        )

    try:
        content = _read_text(target_path)
    except OSError as exc:
        return _insert_position_failure(
            project_path,
            target_file,
            target_path,
            line,
            0,
            f"file read error: {exc}",
        )

    lines = content.splitlines(keepends=True)
    line_count = len(lines)
    position_error = _validate_insert_line(line, line_count)
    if position_error is not None:
        return _insert_position_failure(
            project_path,
            target_file,
            target_path,
            line,
            line_count,
            position_error,
        )

    return InsertPositionValidationResult(
        valid=True,
        project_dir=str(project_path),
        target_file=str(target_file),
        file_path=str(target_path),
        line=line,
        line_count=line_count,
        file_hash=_hash_text(content),
        before_line=lines[line - 1] if line > 0 and lines else None,
        after_line=lines[line] if line < line_count else None,
        message="insert position is valid",
    )


def apply_insert_and_compile(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    line: int,
    expected_hash: str,
    new_text: str,
    main_tex: str | Path = "main.tex",
    compiler: str = "pdflatex",
    timeout: float | None = 120,
) -> InsertEditResult:
    """Insert text after a validated line, compile, and restore on failure."""

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)
    validation_error = _validate_common(project_path, target_path)
    if validation_error is not None:
        return _insert_edit_failure(
            project_path,
            target_file,
            target_path,
            line,
            expected_hash,
            None,
            validation_error,
        )

    try:
        original_content = _read_text(target_path)
    except OSError as exc:
        return _insert_edit_failure(
            project_path,
            target_file,
            target_path,
            line,
            expected_hash,
            None,
            f"file read error: {exc}",
        )

    actual_hash = _hash_text(original_content)
    if actual_hash != expected_hash:
        return _insert_edit_failure(
            project_path,
            target_file,
            target_path,
            line,
            expected_hash,
            actual_hash,
            "file hash mismatch; re-run validate_insert_position",
        )

    lines = original_content.splitlines(keepends=True)
    line_count = len(lines)
    position_error = _validate_insert_line(line, line_count)
    if position_error is not None:
        return _insert_edit_failure(
            project_path,
            target_file,
            target_path,
            line,
            expected_hash,
            actual_hash,
            position_error,
        )

    insert_text = _prepare_insert_text(new_text, _detect_newline(original_content))
    updated_content = _insert_after_line(lines, line, insert_text)
    try:
        _write_text(target_path, updated_content)
    except OSError as exc:
        return _insert_edit_failure(
            project_path,
            target_file,
            target_path,
            line,
            expected_hash,
            actual_hash,
            f"file write error: {exc}",
        )

    try:
        compile_result = compile_latex(
            project_path,
            main_tex,
            compiler=compiler,
            timeout=timeout,
        )
    except Exception as exc:
        restored = _restore_text(target_path, original_content)
        return InsertEditResult(
            success=False,
            status="compile_start_failed_restored" if restored else "compile_start_failed_restore_failed",
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            line=line,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            changed=True,
            compile_result=None,
            clean_result=None,
            log_parse_result=None,
            restored=restored,
            message=f"compilation could not start: {type(exc).__name__}: {exc}",
        )

    if compile_result.success:
        clean_result = clean_latex_artifacts(project_path, main_tex)
        return InsertEditResult(
            success=True,
            status="compile_succeeded",
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            line=line,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            changed=True,
            compile_result=compile_result,
            clean_result=clean_result,
            log_parse_result=None,
            restored=False,
            message="text inserted and LaTeX compilation succeeded",
        )

    log_parse_result = parse_latex_log(compile_result.log_path) if compile_result.log_exists else None
    restored = _restore_text(target_path, original_content)
    return InsertEditResult(
        success=False,
        status="compile_failed_restored" if restored else "compile_failed_restore_failed",
        project_dir=str(project_path),
        target_file=str(target_file),
        file_path=str(target_path),
        line=line,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        changed=True,
        compile_result=compile_result,
        clean_result=None,
        log_parse_result=log_parse_result,
        restored=restored,
        message="LaTeX compilation failed; original text was restored" if restored else "LaTeX compilation failed; restore failed",
    )


def _resolve_target_file(project_dir: Path, target_file: str | Path) -> Path:
    path = Path(target_file).expanduser()
    if path.is_absolute():
        return path.resolve()

    return (project_dir / path).resolve()


def _validate_common(project_dir: Path, target_path: Path) -> str | None:
    if not project_dir.exists():
        return "project_dir does not exist"
    if not project_dir.is_dir():
        return "project_dir is not a directory"
    if not _is_relative_to(target_path, project_dir):
        return "target_file must be inside project_dir"
    if not target_path.exists():
        return "target_file does not exist"
    if not target_path.is_file():
        return "target_file is not a file"

    return None


def _validate_insert_line(line: int, line_count: int) -> str | None:
    if line < 0:
        return "line must be greater than or equal to 0"
    if line > line_count:
        return f"line exceeds file line count: {line_count}"

    return None


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False

    return True


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as file:
                return file.read()
        except UnicodeDecodeError:
            continue

    with path.open("r", encoding="utf-8", errors="replace", newline="") as file:
        return file.read()


def _write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write(content)


def _restore_text(path: Path, content: str) -> bool:
    try:
        _write_text(path, content)
    except OSError:
        return False

    return True


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _detect_newline(content: str) -> str:
    if "\r\n" in content:
        return "\r\n"
    if "\r" in content:
        return "\r"

    return "\n"


def _prepare_insert_text(new_text: str, newline: str) -> str:
    if not new_text:
        return ""
    if new_text.endswith(("\n", "\r")):
        return new_text

    return f"{new_text}{newline}"


def _insert_after_line(lines: list[str], line: int, insert_text: str) -> str:
    if line == 0:
        return insert_text + "".join(lines)

    prefix = "".join(lines[:line])
    suffix = "".join(lines[line:])
    if lines and line == len(lines) and not lines[-1].endswith(("\n", "\r")):
        return prefix + _detect_newline(prefix) + insert_text + suffix

    return prefix + insert_text + suffix


def _insert_position_failure(
    project_dir: Path,
    target_file: str | Path,
    target_path: Path,
    line: int,
    line_count: int,
    message: str,
) -> InsertPositionValidationResult:
    return InsertPositionValidationResult(
        valid=False,
        project_dir=str(project_dir),
        target_file=str(target_file),
        file_path=str(target_path),
        line=line,
        line_count=line_count,
        file_hash=None,
        before_line=None,
        after_line=None,
        message=message,
    )


def _insert_edit_failure(
    project_dir: Path,
    target_file: str | Path,
    target_path: Path,
    line: int,
    expected_hash: str,
    actual_hash: str | None,
    message: str,
) -> InsertEditResult:
    return InsertEditResult(
        success=False,
        status="validation_failed",
        project_dir=str(project_dir),
        target_file=str(target_file),
        file_path=str(target_path),
        line=line,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        changed=False,
        compile_result=None,
        clean_result=None,
        log_parse_result=None,
        restored=False,
        message=message,
    )
