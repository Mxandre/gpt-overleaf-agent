"""Validate proposed text edits without modifying files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class TextEditValidationResult:
    """Result of validating one exact text replacement."""

    valid: bool
    project_dir: str
    target_file: str
    file_path: str | None
    old_text_found: bool
    occurrences: int
    start_line: int | None
    end_line: int | None
    target_text: str | None
    message: str


def validate_text_edit(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    old_text: str,
) -> TextEditValidationResult:
    """Validate that ``old_text`` appears exactly once in ``target_file``.

    This function is read-only. It does not create backups, write files, compile
    LaTeX, or call an LLM.
    """

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)

    if not project_path.exists():
        return _failure(project_path, target_file, target_path, "project_dir does not exist")
    if not project_path.is_dir():
        return _failure(project_path, target_file, target_path, "project_dir is not a directory")
    if not _is_relative_to(target_path, project_path):
        return _failure(project_path, target_file, target_path, "target_file must be inside project_dir")
    if not target_path.exists():
        return _failure(project_path, target_file, target_path, "target_file does not exist")
    if not target_path.is_file():
        return _failure(project_path, target_file, target_path, "target_file is not a file")
    if old_text == "":
        return _failure(project_path, target_file, target_path, "old_text must not be empty")

    try:
        content = _read_text(target_path)
    except OSError as exc:
        return _failure(project_path, target_file, target_path, f"file read error: {exc}")

    target_text = old_text
    occurrences = content.count(old_text)
    if occurrences == 0:
        normalized_result = _find_unique_whitespace_normalized_match(content, old_text)
        if normalized_result is None:
            return TextEditValidationResult(
                valid=False,
                project_dir=str(project_path),
                target_file=str(target_file),
                file_path=str(target_path),
                old_text_found=False,
                occurrences=0,
                start_line=None,
                end_line=None,
                target_text=None,
                message="old_text not found",
            )

        target_text, start, end = normalized_result
        return TextEditValidationResult(
            valid=True,
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            old_text_found=True,
            occurrences=1,
            start_line=_line_number(content, start),
            end_line=_end_line_number(content, end),
            target_text=target_text,
            message="old_text found exactly once after whitespace normalization",
        )

    start = content.find(old_text)
    end = start + len(old_text)
    start_line = _line_number(content, start)
    end_line = _end_line_number(content, end)

    if occurrences > 1:
        return TextEditValidationResult(
            valid=False,
            project_dir=str(project_path),
            target_file=str(target_file),
            file_path=str(target_path),
            old_text_found=True,
            occurrences=occurrences,
            start_line=start_line,
            end_line=end_line,
            target_text=None,
            message="old_text matched multiple times; provide a more specific old_text",
        )

    return TextEditValidationResult(
        valid=True,
        project_dir=str(project_path),
        target_file=str(target_file),
        file_path=str(target_path),
        old_text_found=True,
        occurrences=1,
        start_line=start_line,
        end_line=end_line,
        target_text=target_text,
        message="old_text found exactly once",
    )


def _resolve_target_file(project_dir: Path, target_file: str | Path) -> Path:
    path = Path(target_file).expanduser()
    if path.is_absolute():
        return path.resolve()

    return (project_dir / path).resolve()


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


def _find_unique_whitespace_normalized_match(
    content: str,
    old_text: str,
) -> tuple[str, int, int] | None:
    normalized_content, index_map = _normalize_with_index_map(content)
    normalized_old_text = re.sub(r"\s+", " ", old_text).strip()
    if not normalized_old_text:
        return None

    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        index = normalized_content.find(normalized_old_text, start)
        if index == -1:
            break
        matches.append((index, index + len(normalized_old_text)))
        start = index + 1

    if len(matches) != 1:
        return None

    normalized_start, normalized_end = matches[0]
    original_start = index_map[normalized_start]
    original_end = index_map[normalized_end - 1] + 1
    return content[original_start:original_end], original_start, original_end


def _normalize_with_index_map(content: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    index_map: list[int] = []
    in_whitespace = False

    for index, char in enumerate(content):
        if char.isspace():
            if not in_whitespace and normalized_chars:
                normalized_chars.append(" ")
                index_map.append(index)
            in_whitespace = True
            continue

        normalized_chars.append(char)
        index_map.append(index)
        in_whitespace = False

    if normalized_chars and normalized_chars[-1] == " ":
        normalized_chars.pop()
        index_map.pop()

    return "".join(normalized_chars), index_map


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _end_line_number(content: str, end_offset: int) -> int:
    if end_offset <= 0:
        return 1

    return content.count("\n", 0, end_offset - 1) + 1


def _failure(
    project_dir: Path,
    target_file: str | Path,
    file_path: Path | None,
    message: str,
) -> TextEditValidationResult:
    return TextEditValidationResult(
        valid=False,
        project_dir=str(project_dir),
        target_file=str(target_file),
        file_path=str(file_path) if file_path is not None else None,
        old_text_found=False,
        occurrences=0,
        start_line=None,
        end_line=None,
        target_text=None,
        message=message,
    )
