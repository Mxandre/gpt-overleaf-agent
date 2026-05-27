"""Parse LaTeX log files into structured errors and warnings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


ERROR_SEVERITY = "error"
WARNING_SEVERITY = "warning"

FILE_LINE_RE = re.compile(r"^(?P<file>.+?\.tex):(?P<line>\d+):\s*(?P<message>.+)$")
MISSING_FILE_RE = re.compile(r"File `(?P<file>[^']+)' not found\.", re.IGNORECASE)
LATEX_ERROR_RE = re.compile(r"^!\s*LaTeX Error:\s*(?P<message>.+)$")
PACKAGE_ERROR_RE = re.compile(r"^!\s*Package (?P<package>\S+) Error:\s*(?P<message>.+)$")
GENERIC_BANG_ERROR_RE = re.compile(r"^!\s*(?P<message>.+)$")
WARNING_RE = re.compile(
    r"(?P<kind>LaTeX Warning|Package \S+ Warning|Overfull \\hbox|Underfull \\hbox)"
    r"(?P<message>.*)"
)


@dataclass(frozen=True)
class LatexLogIssue:
    """A single structured error or warning extracted from a LaTeX log."""

    severity: str
    kind: str
    message: str
    file: str | None
    line: int | None
    context: str


@dataclass(frozen=True)
class LatexLogParseResult:
    """Structured result for a parsed LaTeX log."""

    log_path: str | None
    has_errors: bool
    errors: list[LatexLogIssue]
    warnings: list[LatexLogIssue]
    raw_excerpt: str


def parse_latex_log(
    log_path: str | Path | None = None,
    *,
    log_text: str | None = None,
    context_lines: int = 3,
    raw_excerpt_lines: int = 80,
) -> LatexLogParseResult:
    """Parse a LaTeX log file or log text into errors and warnings.

    Provide either ``log_path`` or ``log_text``. When ``log_path`` is used, the
    file is read with tolerant encoding fallback because LaTeX logs on Windows
    can contain mixed encodings.
    """

    if log_path is None and log_text is None:
        raise ValueError("Either log_path or log_text must be provided")

    if log_path is not None and log_text is not None:
        raise ValueError("Provide only one of log_path or log_text")

    normalized_log_path = str(Path(log_path).expanduser().resolve()) if log_path else None
    text = _read_log_text(Path(log_path)) if log_path else log_text or ""
    lines = text.splitlines()

    errors: list[LatexLogIssue] = []
    warnings: list[LatexLogIssue] = []

    for index, line in enumerate(lines):
        error = _parse_error_line(lines, index, context_lines)
        if error is not None:
            errors.append(error)
            continue

        warning = _parse_warning_line(lines, index, context_lines)
        if warning is not None:
            warnings.append(warning)

    return LatexLogParseResult(
        log_path=normalized_log_path,
        has_errors=bool(errors),
        errors=errors,
        warnings=warnings,
        raw_excerpt="\n".join(lines[:raw_excerpt_lines]),
    )


def _read_log_text(log_path: Path) -> str:
    path = log_path.expanduser()
    if not path.exists():
        raise FileNotFoundError(f"log_path does not exist: {path}")

    if not path.is_file():
        raise FileNotFoundError(f"log_path is not a file: {path}")

    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="replace")


def _parse_error_line(
    lines: list[str],
    index: int,
    context_lines: int,
) -> LatexLogIssue | None:
    line = lines[index].strip()
    file_name: str | None = None
    line_number: int | None = None

    file_line_match = FILE_LINE_RE.match(line)
    if file_line_match:
        file_name = file_line_match.group("file")
        line_number = int(file_line_match.group("line"))
        message = file_line_match.group("message").strip()
        kind = _classify_error(message)
        return LatexLogIssue(
            severity=ERROR_SEVERITY,
            kind=kind,
            message=message,
            file=file_name,
            line=line_number,
            context=_context(lines, index, context_lines),
        )

    missing_file_match = MISSING_FILE_RE.search(line)
    if missing_file_match:
        missing_file = missing_file_match.group("file")
        return LatexLogIssue(
            severity=ERROR_SEVERITY,
            kind="Missing file",
            message=f"File `{missing_file}` not found.",
            file=missing_file,
            line=None,
            context=_context(lines, index, context_lines),
        )

    package_error_match = PACKAGE_ERROR_RE.match(line)
    if package_error_match:
        package_name = package_error_match.group("package")
        message = package_error_match.group("message").strip()
        return LatexLogIssue(
            severity=ERROR_SEVERITY,
            kind=f"Package {package_name} Error",
            message=message,
            file=None,
            line=None,
            context=_context(lines, index, context_lines),
        )

    latex_error_match = LATEX_ERROR_RE.match(line)
    if latex_error_match:
        message = latex_error_match.group("message").strip()
        missing_file_match = MISSING_FILE_RE.search(message)
        if missing_file_match:
            missing_file = missing_file_match.group("file")
            return LatexLogIssue(
                severity=ERROR_SEVERITY,
                kind="Missing file",
                message=f"File `{missing_file}` not found.",
                file=missing_file,
                line=None,
                context=_context(lines, index, context_lines),
            )

        return LatexLogIssue(
            severity=ERROR_SEVERITY,
            kind="LaTeX Error",
            message=message,
            file=None,
            line=None,
            context=_context(lines, index, context_lines),
        )

    generic_error_match = GENERIC_BANG_ERROR_RE.match(line)
    if generic_error_match:
        message = generic_error_match.group("message").strip()
        kind = _classify_error(message)
        if kind is None:
            return None

        return LatexLogIssue(
            severity=ERROR_SEVERITY,
            kind=kind,
            message=message,
            file=None,
            line=None,
            context=_context(lines, index, context_lines),
        )

    return None


def _parse_warning_line(
    lines: list[str],
    index: int,
    context_lines: int,
) -> LatexLogIssue | None:
    line = lines[index].strip()
    warning_match = WARNING_RE.search(line)
    if warning_match is None:
        return None

    kind = warning_match.group("kind").strip()
    message = line
    file_name, line_number = _extract_warning_location(line)

    return LatexLogIssue(
        severity=WARNING_SEVERITY,
        kind=kind,
        message=message,
        file=file_name,
        line=line_number,
        context=_context(lines, index, context_lines),
    )


def _classify_error(message: str) -> str | None:
    lower = message.lower()

    if "undefined control sequence" in lower:
        return "Undefined control sequence"
    if "missing $ inserted" in lower:
        return "Missing $ inserted"
    if "extra }, or forgotten" in lower:
        return "Extra brace or forgotten group"
    if "emergency stop" in lower:
        return "Emergency stop"
    if "latex error" in lower:
        return "LaTeX Error"
    if "package" in lower and "error" in lower:
        return "Package Error"

    return None


def _extract_warning_location(line: str) -> tuple[str | None, int | None]:
    location_match = re.search(r"on input line (?P<line>\d+)", line)
    if location_match is None:
        return None, None

    return None, int(location_match.group("line"))


def _context(lines: list[str], index: int, context_lines: int) -> str:
    start = max(0, index - context_lines)
    end = min(len(lines), index + context_lines + 1)
    return "\n".join(lines[start:end])
