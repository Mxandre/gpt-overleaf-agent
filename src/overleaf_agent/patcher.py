"""Conservative text patching utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil


@dataclass(frozen=True)
class PatchResult:
    """Structured result for a patch or restore operation."""

    success: bool
    file_path: str
    backup_path: str | None
    old_text_found: bool
    occurrences: int
    message: str
    changed: bool


@dataclass(frozen=True)
class TextReplacement:
    """One exact text replacement."""

    old_text: str
    new_text: str
    rationale: str = ""


@dataclass(frozen=True)
class ReplacementCheck:
    """Validation details for one replacement in a batch."""

    old_text: str
    new_text: str
    rationale: str
    old_text_found: bool
    occurrences: int
    start: int | None
    end: int | None
    message: str


@dataclass(frozen=True)
class ManyPatchResult:
    """Structured result for an atomic multi-replacement patch."""

    success: bool
    file_path: str
    backup_path: str | None
    replacements: tuple[ReplacementCheck, ...]
    message: str
    changed: bool


def create_backup(file_path: str | Path) -> str:
    """Create a timestamped backup next to ``file_path`` and return its path."""

    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"file does not exist: {path}")

    if not path.is_file():
        raise FileNotFoundError(f"path is not a file: {path}")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    shutil.copy2(path, backup_path)
    return str(backup_path)


def replace_text(
    file_path: str | Path,
    old_text: str,
    new_text: str,
    *,
    encoding: str = "utf-8",
    create_backup_file: bool = True,
) -> PatchResult:
    """Replace exactly one occurrence of ``old_text`` with ``new_text``."""

    path = Path(file_path).expanduser().resolve()
    validation_error = _validate_patch_input(path, old_text)
    if validation_error is not None:
        return validation_error

    try:
        content = path.read_text(encoding=encoding)
    except UnicodeError as exc:
        return _failure(path, None, f"encoding error: {exc}")
    except OSError as exc:
        return _failure(path, None, f"permission or file read error: {exc}")

    occurrences = content.count(old_text)
    if occurrences == 0:
        return PatchResult(
            success=False,
            file_path=str(path),
            backup_path=None,
            old_text_found=False,
            occurrences=0,
            message="old_text not found",
            changed=False,
        )

    if occurrences > 1:
        return PatchResult(
            success=False,
            file_path=str(path),
            backup_path=None,
            old_text_found=True,
            occurrences=occurrences,
            message="old_text matched multiple times; provide a more specific old_text",
            changed=False,
        )

    backup_path = None
    if create_backup_file:
        try:
            backup_path = create_backup(path)
        except OSError as exc:
            return PatchResult(
                success=False,
                file_path=str(path),
                backup_path=None,
                old_text_found=True,
                occurrences=occurrences,
                message=f"could not create backup: {exc}",
                changed=False,
            )

    new_content = content.replace(old_text, new_text, 1)
    try:
        path.write_text(new_content, encoding=encoding)
    except UnicodeError as exc:
        return PatchResult(
            success=False,
            file_path=str(path),
            backup_path=backup_path,
            old_text_found=True,
            occurrences=occurrences,
            message=f"encoding error while writing file: {exc}",
            changed=False,
        )
    except OSError as exc:
        return PatchResult(
            success=False,
            file_path=str(path),
            backup_path=backup_path,
            old_text_found=True,
            occurrences=occurrences,
            message=f"permission or file write error: {exc}",
            changed=False,
        )

    return PatchResult(
        success=True,
        file_path=str(path),
        backup_path=backup_path,
        old_text_found=True,
        occurrences=occurrences,
        message="replacement applied",
        changed=True,
    )


def replace_many(
    file_path: str | Path,
    replacements: list[TextReplacement] | tuple[TextReplacement, ...],
    *,
    encoding: str = "utf-8",
) -> ManyPatchResult:
    """Apply multiple independent exact replacements atomically."""

    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return _many_failure(path, None, (), "file does not exist")
    if not path.is_file():
        return _many_failure(path, None, (), "path is not a file")
    if not replacements:
        return _many_failure(path, None, (), "replacements must not be empty")

    try:
        content = path.read_text(encoding=encoding)
    except UnicodeError as exc:
        return _many_failure(path, None, (), f"encoding error: {exc}")
    except OSError as exc:
        return _many_failure(path, None, (), f"permission or file read error: {exc}")

    checks: list[ReplacementCheck] = []
    for replacement in replacements:
        if replacement.old_text == "":
            checks.append(
                ReplacementCheck(
                    old_text=replacement.old_text,
                    new_text=replacement.new_text,
                    rationale=replacement.rationale,
                    old_text_found=False,
                    occurrences=0,
                    start=None,
                    end=None,
                    message="old_text must not be empty",
                )
            )
            continue

        occurrences = content.count(replacement.old_text)
        start = content.find(replacement.old_text) if occurrences == 1 else None
        end = start + len(replacement.old_text) if start is not None else None
        checks.append(
            ReplacementCheck(
                old_text=replacement.old_text,
                new_text=replacement.new_text,
                rationale=replacement.rationale,
                old_text_found=occurrences > 0,
                occurrences=occurrences,
                start=start,
                end=end,
                message=_replacement_check_message(occurrences),
            )
        )

    first_failed = next((check for check in checks if check.occurrences != 1), None)
    if first_failed is not None:
        return _many_failure(path, None, tuple(checks), first_failed.message)

    if _has_overlapping_checks(checks):
        return _many_failure(path, None, tuple(checks), "replacements overlap; refusing to apply batch")

    try:
        backup_path = create_backup(path)
    except OSError as exc:
        return _many_failure(path, None, tuple(checks), f"could not create backup: {exc}")

    new_content = content
    for check in sorted(checks, key=lambda item: item.start or 0, reverse=True):
        assert check.start is not None
        assert check.end is not None
        new_content = new_content[: check.start] + check.new_text + new_content[check.end :]

    try:
        path.write_text(new_content, encoding=encoding)
    except UnicodeError as exc:
        return _many_failure(path, backup_path, tuple(checks), f"encoding error while writing file: {exc}")
    except OSError as exc:
        return _many_failure(path, backup_path, tuple(checks), f"permission or file write error: {exc}")

    return ManyPatchResult(
        success=True,
        file_path=str(path),
        backup_path=backup_path,
        replacements=tuple(checks),
        message=f"{len(checks)} replacements applied",
        changed=True,
    )


def restore_backup(
    original_path: str | Path,
    backup_path: str | Path,
) -> PatchResult:
    """Restore ``backup_path`` over ``original_path``."""

    original = Path(original_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve()

    if not backup.exists() or not backup.is_file():
        return PatchResult(
            success=False,
            file_path=str(original),
            backup_path=str(backup),
            old_text_found=False,
            occurrences=0,
            message=f"backup file does not exist: {backup}",
            changed=False,
        )

    try:
        shutil.copy2(backup, original)
    except OSError as exc:
        return PatchResult(
            success=False,
            file_path=str(original),
            backup_path=str(backup),
            old_text_found=False,
            occurrences=0,
            message=f"could not restore backup: {exc}",
            changed=False,
        )

    return PatchResult(
        success=True,
        file_path=str(original),
        backup_path=str(backup),
        old_text_found=False,
        occurrences=0,
        message="backup restored",
        changed=True,
    )


def _validate_patch_input(path: Path, old_text: str) -> PatchResult | None:
    if not path.exists():
        return _failure(path, None, "file does not exist")

    if not path.is_file():
        return _failure(path, None, "path is not a file")

    if old_text == "":
        return _failure(path, None, "old_text must not be empty")

    return None


def _failure(path: Path, backup_path: str | None, message: str) -> PatchResult:
    return PatchResult(
        success=False,
        file_path=str(path),
        backup_path=backup_path,
        old_text_found=False,
        occurrences=0,
        message=message,
        changed=False,
    )


def _replacement_check_message(occurrences: int) -> str:
    if occurrences == 0:
        return "old_text not found"
    if occurrences > 1:
        return "old_text matched multiple times; provide a more specific old_text"
    return "old_text uniquely matched"


def _has_overlapping_checks(checks: list[ReplacementCheck]) -> bool:
    spans = sorted((check.start, check.end) for check in checks)
    previous_end: int | None = None
    for start, end in spans:
        if start is None or end is None:
            return True
        if previous_end is not None and start < previous_end:
            return True
        previous_end = end

    return False


def _many_failure(
    path: Path,
    backup_path: str | None,
    replacements: tuple[ReplacementCheck, ...],
    message: str,
) -> ManyPatchResult:
    return ManyPatchResult(
        success=False,
        file_path=str(path),
        backup_path=backup_path,
        replacements=replacements,
        message=message,
        changed=False,
    )
