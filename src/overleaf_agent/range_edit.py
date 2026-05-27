"""Validate and apply line-range based text edits."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from overleaf_agent.latex_cleaner import LatexCleanResult, clean_latex_artifacts
from overleaf_agent.latex_compile import LatexCompileResult, compile_latex
from overleaf_agent.log_parser import LatexLogParseResult, parse_latex_log


@dataclass(frozen=True)
class TextRangeValidationResult:
    """Result of validating a line range without modifying files."""

    valid: bool
    project_dir: str
    target_file: str
    file_path: str | None
    start_line: int
    end_line: int
    line_count: int
    target_text: str | None
    target_hash: str | None
    message: str


@dataclass(frozen=True)
class TextRangeEditResult:
    """Result of applying a line-range edit and compiling the project."""

    success: bool
    status: str
    project_dir: str
    target_file: str
    file_path: str | None
    start_line: int
    end_line: int
    expected_hash: str
    actual_hash: str | None
    changed: bool
    compile_result: LatexCompileResult | None
    clean_result: LatexCleanResult | None
    log_parse_result: LatexLogParseResult | None
    restored: bool
    message: str


def validate_text_range(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    start_line: int,
    end_line: int,
) -> TextRangeValidationResult:
    """Read and validate a 1-based inclusive line range."""

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)
    validation_error = _validate_common(project_path, target_path)
    if validation_error is not None:
        return TextRangeValidationResult(
            valid=False,
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            start_line=start_line,
            end_line=end_line,
            line_count=0,
            target_text=None,
            target_hash=None,
            message=validation_error,
        )

    try:
        content = _read_text(target_path)
    except OSError as exc:
        return _range_failure(project_path, target_file, target_path, start_line, end_line, 0, f"file read error: {exc}")

    lines = content.splitlines(keepends=True)
    line_count = len(lines)
    range_error = _validate_line_range(start_line, end_line, line_count)
    if range_error is not None:
        return _range_failure(project_path, target_file, target_path, start_line, end_line, line_count, range_error)

    target_text = "".join(lines[start_line - 1 : end_line])
    return TextRangeValidationResult(
        valid=True,
        project_dir=str(project_path),
        target_file=str(target_file),
        file_path=str(target_path),
        start_line=start_line,
        end_line=end_line,
        line_count=line_count,
        target_text=target_text,
        target_hash=_hash_text(target_text),
        message="line range is valid",
    )


def apply_text_range_and_compile(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    start_line: int,
    end_line: int,
    expected_hash: str,
    new_text: str,
    main_tex: str | Path = "main.tex",
    compiler: str = "pdflatex",
    timeout: float | None = 120,
) -> TextRangeEditResult:
    """Replace a validated line range, compile, and restore on compile failure."""

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)
    validation_error = _validate_common(project_path, target_path)
    if validation_error is not None:
        return _edit_failure(
            project_path,
            target_file,
            target_path,
            start_line,
            end_line,
            expected_hash,
            None,
            validation_error,
        )

    try:
        original_content = _read_text(target_path)
    except OSError as exc:
        return _edit_failure(
            project_path,
            target_file,
            target_path,
            start_line,
            end_line,
            expected_hash,
            None,
            f"file read error: {exc}",
        )

    lines = original_content.splitlines(keepends=True)
    line_count = len(lines)
    range_error = _validate_line_range(start_line, end_line, line_count)
    if range_error is not None:
        return _edit_failure(
            project_path,
            target_file,
            target_path,
            start_line,
            end_line,
            expected_hash,
            None,
            range_error,
        )

    current_text = "".join(lines[start_line - 1 : end_line])
    actual_hash = _hash_text(current_text)
    if actual_hash != expected_hash:
        return _edit_failure(
            project_path,
            target_file,
            target_path,
            start_line,
            end_line,
            expected_hash,
            actual_hash,
            "line range hash mismatch; re-run validate_text_range",
        )

    updated_content = "".join(lines[: start_line - 1]) + new_text + "".join(lines[end_line:])
    try:
        _write_text(target_path, updated_content)
    except OSError as exc:
        return _edit_failure(
            project_path,
            target_file,
            target_path,
            start_line,
            end_line,
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
        return TextRangeEditResult(
            success=False,
            status="compile_start_failed_restored" if restored else "compile_start_failed_restore_failed",
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            start_line=start_line,
            end_line=end_line,
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
        return TextRangeEditResult(
            success=True,
            status="compile_succeeded",
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            start_line=start_line,
            end_line=end_line,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            changed=True,
            compile_result=compile_result,
            clean_result=clean_result,
            log_parse_result=None,
            restored=False,
            message="range edit applied and LaTeX compilation succeeded",
        )

    log_parse_result = parse_latex_log(compile_result.log_path) if compile_result.log_exists else None
    restored = _restore_text(target_path, original_content)
    return TextRangeEditResult(
        success=False,
        status="compile_failed_restored" if restored else "compile_failed_restore_failed",
        project_dir=str(project_path),
        target_file=str(target_file),
        file_path=str(target_path),
        start_line=start_line,
        end_line=end_line,
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


def _validate_common(
    project_dir: Path,
    target_path: Path,
) -> str | None:
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


def _validate_line_range(start_line: int, end_line: int, line_count: int) -> str | None:
    if start_line < 1:
        return "start_line must be greater than or equal to 1"
    if end_line < start_line:
        return "end_line must be greater than or equal to start_line"
    if line_count == 0:
        return "target_file is empty"
    if end_line > line_count:
        return f"end_line exceeds file line count: {line_count}"

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


def _range_failure(
    project_dir: Path,
    target_file: str | Path,
    target_path: Path,
    start_line: int,
    end_line: int,
    line_count: int,
    message: str,
) -> TextRangeValidationResult:
    return TextRangeValidationResult(
        valid=False,
        project_dir=str(project_dir),
        target_file=str(target_file),
        file_path=str(target_path),
        start_line=start_line,
        end_line=end_line,
        line_count=line_count,
        target_text=None,
        target_hash=None,
        message=message,
    )


def _edit_failure(
    project_dir: Path,
    target_file: str | Path,
    target_path: Path,
    start_line: int,
    end_line: int,
    expected_hash: str,
    actual_hash: str | None,
    message: str,
) -> TextRangeEditResult:
    return TextRangeEditResult(
        success=False,
        status="validation_failed",
        project_dir=str(project_dir),
        target_file=str(target_file),
        file_path=str(target_path),
        start_line=start_line,
        end_line=end_line,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        changed=False,
        compile_result=None,
        clean_result=None,
        log_parse_result=None,
        restored=False,
        message=message,
    )
