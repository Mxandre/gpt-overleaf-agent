"""Safe edit pipeline: patch, compile, parse errors, and restore on failure."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from overleaf_agent.latex_cleaner import LatexCleanResult, clean_latex_artifacts
from overleaf_agent.latex_compile import LatexCompileResult, compile_latex
from overleaf_agent.log_parser import LatexLogParseResult, parse_latex_log
from overleaf_agent.patcher import ManyPatchResult, PatchResult, TextReplacement, replace_many, replace_text, restore_backup


STATUS_PATCH_FAILED = "patch_failed"
STATUS_COMPILE_SUCCEEDED = "compile_succeeded"
STATUS_COMPILE_FAILED_RESTORED = "compile_failed_restored"
STATUS_COMPILE_FAILED_RESTORE_FAILED = "compile_failed_restore_failed"
STATUS_COMPILE_FAILED_NO_BACKUP = "compile_failed_no_backup"


@dataclass(frozen=True)
class SafeEditResult:
    """Structured result for a safe text replacement pipeline."""

    success: bool
    status: str
    project_dir: str
    target_file: str
    patch_result: PatchResult | ManyPatchResult
    compile_result: LatexCompileResult | None
    clean_result: LatexCleanResult | None
    log_parse_result: LatexLogParseResult | None
    restore_result: PatchResult | None
    diff: str
    diff_stat: str
    message: str


def apply_text_replacement_and_compile(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    old_text: str,
    new_text: str,
    main_tex: str | Path = "main.tex",
    compiler: str = "pdflatex",
    timeout: float | None = 120,
    create_backup_file: bool = True,
) -> SafeEditResult:
    """Apply one exact text replacement and validate it by compiling LaTeX."""

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)

    patch_result = replace_text(
        target_path,
        old_text,
        new_text,
        create_backup_file=create_backup_file,
    )
    return _compile_after_patch(
        project_path=project_path,
        target_path=target_path,
        patch_result=patch_result,
        main_tex=main_tex,
        compiler=compiler,
        timeout=timeout,
        success_message="replacement applied and LaTeX compilation succeeded",
    )


def apply_many_replacements_and_compile(
    *,
    project_dir: str | Path,
    target_file: str | Path,
    replacements: list[TextReplacement] | tuple[TextReplacement, ...],
    main_tex: str | Path = "main.tex",
    compiler: str = "pdflatex",
    timeout: float | None = 120,
) -> SafeEditResult:
    """Apply multiple exact text replacements and validate by compiling LaTeX."""

    project_path = Path(project_dir).expanduser().resolve()
    target_path = _resolve_target_file(project_path, target_file)

    patch_result = replace_many(target_path, replacements)
    return _compile_after_patch(
        project_path=project_path,
        target_path=target_path,
        patch_result=patch_result,
        main_tex=main_tex,
        compiler=compiler,
        timeout=timeout,
        success_message="replacements applied and LaTeX compilation succeeded",
    )


def _compile_after_patch(
    *,
    project_path: Path,
    target_path: Path,
    patch_result: PatchResult | ManyPatchResult,
    main_tex: str | Path,
    compiler: str,
    timeout: float | None,
    success_message: str,
) -> SafeEditResult:
    if not patch_result.success:
        return SafeEditResult(
            success=False,
            status=STATUS_PATCH_FAILED,
            project_dir=str(project_path),
            target_file=str(target_path),
            patch_result=patch_result,
            compile_result=None,
            clean_result=None,
            log_parse_result=None,
            restore_result=None,
            diff="",
            diff_stat="",
            message=patch_result.message,
        )

    try:
        compile_result = compile_latex(
            project_path,
            main_tex,
            compiler=compiler,
            timeout=timeout,
        )
    except Exception as exc:
        restore_result = _restore_if_possible(patch_result, target_path)
        status = (
            STATUS_COMPILE_FAILED_RESTORED
            if restore_result and restore_result.success
            else STATUS_COMPILE_FAILED_RESTORE_FAILED
        )
        return SafeEditResult(
            success=False,
            status=status,
            project_dir=str(project_path),
            target_file=str(target_path),
            patch_result=patch_result,
            compile_result=None,
            clean_result=None,
            log_parse_result=None,
            restore_result=restore_result,
            diff="",
            diff_stat="",
            message=f"compilation could not start: {type(exc).__name__}: {exc}",
        )

    if compile_result.success:
        clean_result = clean_latex_artifacts(project_path, main_tex)
        return SafeEditResult(
            success=True,
            status=STATUS_COMPILE_SUCCEEDED,
            project_dir=str(project_path),
            target_file=str(target_path),
            patch_result=patch_result,
            compile_result=compile_result,
            clean_result=clean_result,
            log_parse_result=None,
            restore_result=None,
            diff="",
            diff_stat="",
            message=success_message,
        )

    log_parse_result = _parse_log_if_available(compile_result)
    restore_result = _restore_if_possible(patch_result, target_path)
    if restore_result is None:
        status = STATUS_COMPILE_FAILED_NO_BACKUP
    elif restore_result.success:
        status = STATUS_COMPILE_FAILED_RESTORED
    else:
        status = STATUS_COMPILE_FAILED_RESTORE_FAILED

    return SafeEditResult(
        success=False,
        status=status,
        project_dir=str(project_path),
        target_file=str(target_path),
        patch_result=patch_result,
        compile_result=compile_result,
        clean_result=None,
        log_parse_result=log_parse_result,
        restore_result=restore_result,
        diff="",
        diff_stat="",
        message="LaTeX compilation failed; original file was restored"
        if status == STATUS_COMPILE_FAILED_RESTORED
        else "LaTeX compilation failed; restore did not complete cleanly",
    )


def _resolve_target_file(project_dir: Path, target_file: str | Path) -> Path:
    path = Path(target_file).expanduser()
    resolved = path.resolve() if path.is_absolute() else (project_dir / path).resolve()
    if not _is_relative_to(resolved, project_dir):
        raise ValueError("target_file must be inside project_dir")

    return resolved


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False

    return True


def _parse_log_if_available(compile_result: LatexCompileResult) -> LatexLogParseResult | None:
    if not compile_result.log_exists:
        return None

    try:
        return parse_latex_log(compile_result.log_path)
    except Exception:
        return None


def _restore_if_possible(patch_result: PatchResult | ManyPatchResult, target_path: Path) -> PatchResult | None:
    if patch_result.backup_path is None:
        return None

    return restore_backup(target_path, patch_result.backup_path)
