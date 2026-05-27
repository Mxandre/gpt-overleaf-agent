"""MCP server wrapper for overleaf-agent tools.

This module intentionally stays thin: each MCP tool delegates to existing
overleaf_agent modules and converts their structured results to JSON-friendly
dictionaries.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, TypeVar
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
import zipfile

from overleaf_agent.context_retriever import retrieve_context as retrieve_project_context
from overleaf_agent.edit_validator import validate_text_edit as validate_project_text_edit
from overleaf_agent.edit_pipeline import apply_text_replacement_and_compile
from overleaf_agent.insert_edit import (
    apply_insert_and_compile,
    validate_insert_position as validate_project_insert_position,
)
from overleaf_agent.latex_cleaner import clean_latex_artifacts as clean_project_latex_artifacts
from overleaf_agent.latex_compile import compile_latex
from overleaf_agent.log_parser import parse_latex_log
from overleaf_agent.project_indexer import detect_main_tex, index_project
from overleaf_agent.range_edit import (
    apply_text_range_and_compile,
    validate_text_range as validate_project_text_range,
)
from overleaf_agent.shell import run_command

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - depends on optional runtime package.
    FastMCP = None  # type: ignore[assignment]


F = TypeVar("F", bound=Callable[..., Any])


def _workspace_root(repo_root: Path) -> Path:
    configured = os.environ.get("WORKSPACE_DIR", "").strip()
    if not configured:
        return (repo_root / "workspace").resolve()

    workspace_path = Path(configured).expanduser()
    if workspace_path.is_absolute():
        return workspace_path.resolve()

    return (repo_root / workspace_path).resolve()


def _load_dotenv_file(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


mcp = FastMCP("overleaf-agent") if FastMCP is not None else None
REPO_ROOT = Path(__file__).resolve().parents[2]
_load_dotenv_file(REPO_ROOT)
WORKSPACE_ROOT = _workspace_root(REPO_ROOT)
os.chdir(REPO_ROOT)

ENVIRONMENT_TOOLS = (
    ("perl", ("perl", "-v")),
    ("latexmk", ("latexmk", "-v")),
    ("pdflatex", ("pdflatex", "--version")),
    ("xelatex", ("xelatex", "--version")),
    ("biber", ("biber", "--version")),
)

VERSION_LINE_HINTS = {
    "perl": ("this is perl",),
    "latexmk": ("latexmk",),
    "pdflatex": ("pdftex", "pdflatex"),
    "xelatex": ("xetex", "xelatex"),
    "biber": ("biber",),
}

PROJECT_SESSIONS: dict[str, dict[str, Any]] = {}
TEMPLATE_ARCHIVE_MAX_FILES = 500


def _tool(func: F) -> F:
    if mcp is None:
        return func

    return mcp.tool()(func)  # type: ignore[return-value]


def _prompt(func: F) -> F:
    if mcp is None:
        return func

    return mcp.prompt()(func)  # type: ignore[return-value]


@_prompt
def overleaf_agent_workflow() -> str:
    """Explain how to use the overleaf-agent MCP tools safely."""

    return  f"""
You are using the overleaf-agent MCP tools to assist with an academic LaTeX project.

Core rule:
- If the user mentions a project but the exact local project name is unclear, call list_workspace_projects before prepare_project.
- When a user mentions a new project path or a new project name, call prepare_project first. Do not invent ~/... or /mnt/user-data/uploads/... paths.
- If the user asks to create a project from an online LaTeX template, first find an official or trusted template URL, show the URL to the user, and ask for explicit confirmation. Only after confirmation, call init_project_from_template_url.
- Reuse the main_tex returned by prepare_project for all later calls on the same project.
- Do not call retrieve_context, validate_text_edit, apply_text_edit, or compile_project before prepare_project for a new project.
- For analysis requests, call retrieve_context after prepare_project.
- For whole-paper review, inspect paper_sections and read sections one by one.
- For compile requests, call compile_project after prepare_project. Successful compilation removes intermediate LaTeX files and keeps the generated PDF.
- If compile_project fails because a LaTeX file such as a .sty, .cls, or .bst is missing, explain the missing file to the user. Ask for explicit confirmation before installing anything. After confirmation, call install_latex_packages with the best known MiKTeX package id, then compile_project again. If installation fails because the package id is wrong, search the web for the correct MiKTeX/CTAN package id, explain the new candidate to the user, ask for confirmation, and retry with that corrected package_id.
- For insertions, call validate_insert_position with line=0 for the beginning of a file or line=N to insert after line N.
- After validate_insert_position, show the user target_file, insertion line, surrounding lines, new_text, and rationale. Only call apply_insert_text after the user explicitly confirms the exact insertion.
- When calling apply_insert_text, pass file_hash returned by validate_insert_position as expected_hash.
- For small exact text edits, call retrieve_context, draft the replacement yourself, then call validate_text_edit.
- For section-level edits, template cleanup, or large replacements, prefer validate_text_range using start_line and end_line from retrieve_context. Do not pass large official template text as old_text.
- After validate_text_edit or validate_text_range, show the user target_file, line range, existing text, new_text, and rationale. Only call apply_text_edit or apply_text_range_edit after the user explicitly confirms the exact replacement.
- When calling apply_text_edit, pass target_text returned by validate_text_edit as target_text.
- When calling apply_text_range_edit, pass target_hash returned by validate_text_range as expected_hash.
- Never call install_latex_packages unless the user has explicitly approved the package installation.
- Never call init_project_from_template_url unless the user has explicitly approved the template URL and target project name.

General safety rules:
- Do not fabricate numerical results, citations, baselines, or experimental claims.
- Preserve LaTeX commands, labels, citations, and mathematical notation unless explicitly asked to change them.
- Prefer small, reviewable edits over large structural rewrites.
"""


@_tool
def list_workspace_projects(max_depth: int = 2) -> dict[str, Any]:
    """List LaTeX-like project directories inside the configured workspace.

    Use this when the user mentions a project name but the exact local path is
    unclear. Returned project names are relative to the workspace and can be
    passed directly to prepare_project.
    """

    def _run() -> dict[str, Any]:
        if max_depth < 1:
            raise ValueError("max_depth must be greater than or equal to 1")

        projects = []
        for directory in _workspace_project_directories(max_depth=max_depth):
            detection = detect_main_tex(directory)
            relative_name = directory.relative_to(WORKSPACE_ROOT).as_posix()
            session = PROJECT_SESSIONS.get(_project_key(directory))
            tex_files = sorted(path.name for path in directory.glob("*.tex") if path.is_file())
            projects.append(
                {
                    "name": relative_name,
                    "project_dir": str(directory),
                    "main_tex": detection.main_tex,
                    "main_tex_candidates": detection.candidates,
                    "main_tex_detection_reason": detection.reason,
                    "root_tex_files": tex_files,
                    "prepared": bool(session and session.get("prepared")),
                }
            )

        return {
            "success": True,
            "workspace_dir": str(WORKSPACE_ROOT),
            "projects": projects,
            "count": len(projects),
        }

    return _run_tool("list_workspace_projects", _run)


@_tool
def prepare_project(
    project_dir: str,
    main_tex: str | None = None,
    environment_timeout: float = 5,
    check_environment: bool = False,
) -> dict[str, Any]:
    """Prepare a LaTeX project by checking env, detecting main TeX, and indexing files."""

    def _run() -> dict[str, Any]:
        resolved_project_dir = _resolve_project_dir(project_dir)
        environment = _check_environment(environment_timeout) if check_environment else _skipped_environment()
        detected_main = detect_main_tex(resolved_project_dir) if main_tex is None else None
        resolved_main_tex = (
            _resolve_project_file_arg(resolved_project_dir, main_tex, "main_tex")
            if main_tex is not None
            else detected_main.main_tex
        )

        if resolved_main_tex is None:
            return {
                "success": False,
                "environment": environment,
                "project_dir": str(resolved_project_dir),
                "main_tex": None,
                "main_tex_detection": detected_main,
                "index": None,
                "error": "Could not infer main_tex. Please specify one of the candidates.",
            }

        project_index = index_project(resolved_project_dir, main_tex=resolved_main_tex)

        session = _save_project_session(
            resolved_project_dir,
            main_tex=resolved_main_tex,
        )

        return {
            "success": True,
            "environment": environment,
            "project_dir": str(resolved_project_dir),
            "main_tex": resolved_main_tex,
            "main_tex_detection": detected_main,
            "index": project_index,
            "session": session,
        }

    return _run_tool("prepare_project", _run)


@_tool
def init_project_from_template_url(
    url: str,
    project_name: str,
    confirmed: bool = False,
    timeout: float = 60,
    max_bytes: int = 50_000_000,
    max_files: int = TEMPLATE_ARCHIVE_MAX_FILES,
    compiler: str = "pdflatex",
) -> dict[str, Any]:
    """Download a confirmed LaTeX template URL into the local workspace.

    Use this only after showing the user the exact URL and target project name,
    and after the user explicitly confirms. The tool supports .zip, .tar.gz,
    .tgz, and single .tex URLs. It never executes downloaded files.
    """

    def _run() -> dict[str, Any]:
        validated_url = _validate_template_url(url)
        validated_project_name = _validate_project_name(project_name)
        target_dir = (WORKSPACE_ROOT / validated_project_name).resolve()
        if not _is_relative_to(target_dir, WORKSPACE_ROOT):
            raise ValueError("project_name must resolve inside the workspace")

        if target_dir.exists():
            return {
                "success": False,
                "status": "target_exists",
                "url": validated_url,
                "project_dir": str(target_dir),
                "message": "Target project directory already exists.",
            }

        template_kind_hint = _template_kind_hint_from_url(validated_url)
        if not confirmed:
            return {
                "success": False,
                "status": "confirmation_required",
                "url": validated_url,
                "project_name": validated_project_name,
                "project_dir": str(target_dir),
                "template_kind_hint": template_kind_hint,
                "message": (
                    "init_project_from_template_url requires confirmed=true "
                    "after explicit user approval of the URL and project name"
                ),
            }

        with tempfile.TemporaryDirectory(prefix=".template-", dir=WORKSPACE_ROOT) as tmp:
            tmp_dir = Path(tmp)
            download_path = tmp_dir / f"template{_template_download_suffix(template_kind_hint)}"
            download = _download_template_url(
                validated_url,
                download_path,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            template_kind = download["template_kind"]

            if template_kind == "tex":
                target_dir.mkdir(parents=False)
                target_file = target_dir / _template_tex_filename(validated_url)
                shutil.copyfile(download_path, target_file)
            else:
                extract_dir = tmp_dir / "extracted"
                extract_dir.mkdir()
                if template_kind == "zip":
                    _safe_extract_zip(download_path, extract_dir, max_files=max_files)
                else:
                    _safe_extract_tar(download_path, extract_dir, max_files=max_files)

                source_root = _template_source_root(extract_dir)
                shutil.copytree(source_root, target_dir)

        detected_main = detect_main_tex(target_dir)
        created_files = _relative_project_files(target_dir)
        if detected_main.main_tex is None:
            return {
                "success": False,
                "status": "main_tex_not_found",
                "url": validated_url,
                "project_dir": str(target_dir),
                "template_kind": template_kind,
                "template_kind_hint": template_kind_hint,
                "download": download,
                "created_files": created_files,
                "main_tex_detection": detected_main,
                "message": "Template was downloaded, but no main TeX file could be detected.",
            }

        project_index = index_project(target_dir, main_tex=detected_main.main_tex)
        compile_result = compile_latex(
            target_dir,
            detected_main.main_tex,
            compiler=compiler,
            timeout=timeout,
        )
        log_parse_result = None
        if not compile_result.success and compile_result.log_exists:
            log_parse_result = parse_latex_log(compile_result.log_path)

        clean_result = None
        if compile_result.success:
            clean_result = clean_project_latex_artifacts(
                project_dir=target_dir,
                main_tex=detected_main.main_tex,
            )

        session = _save_project_session(target_dir, main_tex=detected_main.main_tex)
        return {
            "success": compile_result.success,
            "status": "ready" if compile_result.success else "compile_failed",
            "url": validated_url,
            "project_dir": str(target_dir),
            "template_kind": template_kind,
            "template_kind_hint": template_kind_hint,
            "download": download,
            "created_files": created_files,
            "main_tex": detected_main.main_tex,
            "main_tex_detection": detected_main,
            "index": project_index,
            "compile_result": compile_result,
            "log_parse_result": log_parse_result,
            "clean_result": clean_result,
            "session": session,
        }

    return _run_tool("init_project_from_template_url", _run)


@_tool
def check_tool(tool_name: str, timeout: float = 3) -> dict[str, Any]:
    """Diagnose one required local command, such as latexmk, pdflatex, or biber."""

    def _run() -> dict[str, Any]:
        command = _environment_command(tool_name)
        started_at = time.perf_counter()
        result = run_command(command, cwd=REPO_ROOT, timeout=timeout)
        elapsed_time = round(time.perf_counter() - started_at, 3)
        name = tool_name.lower().strip()
        return {
            "name": name,
            "ok": result.ok,
            "command": list(result.command),
            "resolved_path": shutil.which(command[0]),
            "returncode": result.returncode,
            "elapsed_time": elapsed_time,
            "timed_out": result.timed_out,
            "not_found": result.not_found,
            "version": _first_output_line(name, result.stdout, result.stderr) if result.ok else None,
            "error": None if result.ok else _failure_message(name, result),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "server_cwd": str(Path.cwd()),
            "command_cwd": result.cwd,
        }

    return _run_tool("check_tool", _run)


@_tool
def retrieve_context(
    project_dir: str,
    query: str,
    main_tex: str | None = None,
    max_files: int = 5,
) -> dict[str, Any]:
    """Retrieve LaTeX section context for a query.

    This tool parses the paper and returns paper_sections every time.
    It only returns chunks when the query matches an actual section or
    subsection title. If chunks is empty but paper_sections is non-empty,
    choose an exact title from paper_sections and call this tool again with
    that title as query.
    """

    def _run() -> Any:
        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_main_tex = _resolve_cached_main_tex(resolved_project_dir, main_tex)
        project_index = index_project(resolved_project_dir, main_tex=resolved_main_tex)
        return retrieve_project_context(project_index, query, max_files=max_files)

    return _run_tool("retrieve_context", _run)

@_tool
def validate_insert_position(
    project_dir: str,
    target_file: str,
    line: int,
) -> dict[str, Any]:
    """Validate an insertion point without modifying files.

    Use line=0 to insert at the beginning of the file. Use line=N to insert
    after line N. The tool returns file_hash; pass it as expected_hash to
    apply_insert_text after explicit user confirmation.
    """

    def _run() -> Any:
        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_target_file = _resolve_project_file_arg(
            resolved_project_dir,
            target_file,
            "target_file",
        )
        return validate_project_insert_position(
            project_dir=resolved_project_dir,
            target_file=resolved_target_file,
            line=line,
        )

    return _run_tool("validate_insert_position", _run)


@_tool
def validate_text_edit(
    project_dir: str,
    target_file: str,
    old_text: str,
) -> dict[str, Any]:
    """Validate a proposed exact text edit without modifying files.

    Use this after reading section context and drafting a replacement. Provide
    target_file and old_text from the retrieved context. The tool checks whether
    the text can be uniquely located and returns target_text, the exact text
    from the file that must be passed to apply_text_edit.
    """

    def _run() -> Any:
        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_target_file = _resolve_project_file_arg(
            resolved_project_dir,
            target_file,
            "target_file",
        )
        return validate_project_text_edit(
            project_dir=resolved_project_dir,
            target_file=resolved_target_file,
            old_text=old_text,
        )

    return _run_tool("validate_text_edit", _run)

@_tool
def validate_text_range(
    project_dir: str,
    target_file: str,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    """Validate a 1-based inclusive line range without modifying files.

    Use this for section-level edits, template cleanup, or large replacements.
    The tool reads the selected lines locally and returns target_text plus
    target_hash. Show target_text and the proposed new_text to the user before
    applying any edit.
    """

    def _run() -> Any:
        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_target_file = _resolve_project_file_arg(
            resolved_project_dir,
            target_file,
            "target_file",
        )
        return validate_project_text_range(
            project_dir=resolved_project_dir,
            target_file=resolved_target_file,
            start_line=start_line,
            end_line=end_line,
        )

    return _run_tool("validate_text_range", _run)

@_tool
def apply_insert_text(
    project_dir: str,
    target_file: str,
    line: int,
    expected_hash: str,
    new_text: str,
    main_tex: str | None = None,
    compiler: str = "pdflatex",
    timeout: float = 120,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Apply a validated text insertion and compile the project.

    Before using this tool, ask the user to confirm the exact insertion. Pass
    file_hash returned by validate_insert_position as expected_hash. The tool
    re-reads the file, checks the hash, inserts new_text after line, compiles,
    and restores the original text if compilation fails.
    """

    def _run() -> Any:
        if not confirmed:
            return {
                "success": False,
                "status": "confirmation_required",
                "message": (
                    "apply_insert_text requires confirmed=true after explicit "
                    "user approval"
                ),
            }

        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_main_tex = _resolve_cached_main_tex(resolved_project_dir, main_tex)
        resolved_target_file = _resolve_project_file_arg(
            resolved_project_dir,
            target_file,
            "target_file",
        )
        return apply_insert_and_compile(
            project_dir=resolved_project_dir,
            target_file=resolved_target_file,
            line=line,
            expected_hash=expected_hash,
            new_text=new_text,
            main_tex=resolved_main_tex,
            compiler=compiler,
            timeout=timeout,
        )

    return _run_tool("apply_insert_text", _run)

@_tool
def apply_text_edit(
    project_dir: str,
    target_file: str,
    target_text: str,
    new_text: str,
    main_tex: str | None = None,
    compiler: str = "pdflatex",
    timeout: float = 120,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Apply a validated text replacement and compile the project.

    Before using this tool, ask the user to confirm the exact replacement.
    Use target_text returned by validate_text_edit, not the original old_text.
    This tool refuses to modify files unless confirmed is true.
    """

    def _run() -> Any:
        if not confirmed:
            return {
                "success": False,
                "status": "confirmation_required",
                "message": (
                    "apply_text_edit requires confirmed=true after explicit user approval"
                ),
            }

        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_main_tex = _resolve_cached_main_tex(resolved_project_dir, main_tex)
        resolved_target_file = _resolve_project_file_arg(
            resolved_project_dir,
            target_file,
            "target_file",
        )
        return apply_text_replacement_and_compile(
            project_dir=resolved_project_dir,
            target_file=resolved_target_file,
            old_text=target_text,
            new_text=new_text,
            main_tex=resolved_main_tex,
            compiler=compiler,
            timeout=timeout,
            create_backup_file=False,
        )

    return _run_tool("apply_text_edit", _run)


@_tool
def apply_text_range_edit(
    project_dir: str,
    target_file: str,
    start_line: int,
    end_line: int,
    expected_hash: str,
    new_text: str,
    main_tex: str | None = None,
    compiler: str = "pdflatex",
    timeout: float = 120,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Apply a validated line-range edit and compile the project.

    Before using this tool, ask the user to confirm the exact replacement.
    Pass expected_hash returned by validate_text_range. The tool re-reads the
    line range, checks the hash, writes the replacement, compiles, and restores
    the original text if compilation fails.
    """

    def _run() -> Any:
        if not confirmed:
            return {
                "success": False,
                "status": "confirmation_required",
                "message": (
                    "apply_text_range_edit requires confirmed=true after "
                    "explicit user approval"
                ),
            }

        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_main_tex = _resolve_cached_main_tex(resolved_project_dir, main_tex)
        resolved_target_file = _resolve_project_file_arg(
            resolved_project_dir,
            target_file,
            "target_file",
        )
        return apply_text_range_and_compile(
            project_dir=resolved_project_dir,
            target_file=resolved_target_file,
            start_line=start_line,
            end_line=end_line,
            expected_hash=expected_hash,
            new_text=new_text,
            main_tex=resolved_main_tex,
            compiler=compiler,
            timeout=timeout,
        )

    return _run_tool("apply_text_range_edit", _run)


@_tool
def compile_project(
    project_dir: str,
    main_tex: str | None = None,
    compiler: str = "pdflatex",
    timeout: float = 120,
) -> dict[str, Any]:
    """Compile a LaTeX project and parse the log when compilation fails."""

    def _run() -> dict[str, Any]:
        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_main_tex = _resolve_cached_main_tex(resolved_project_dir, main_tex)
        compile_result = compile_latex(
            resolved_project_dir,
            resolved_main_tex,
            compiler=compiler,
            timeout=timeout,
        )
        log_parse_result = None
        if not compile_result.success and compile_result.log_exists:
            log_parse_result = parse_latex_log(compile_result.log_path)

        clean_result = None
        if compile_result.success:
            clean_result = clean_project_latex_artifacts(
                project_dir=resolved_project_dir,
                main_tex=resolved_main_tex,
            )

        return {
            "success": compile_result.success,
            "main_tex": resolved_main_tex,
            "compile_result": compile_result,
            "log_parse_result": log_parse_result,
            "clean_result": clean_result,
        }

    return _run_tool("compile_project", _run)

@_tool
def install_latex_packages(
    package_id: str,
    confirmed: bool = False,
    timeout: float = 300,
) -> dict[str, Any]:
    """Install a missing LaTeX package with MiKTeX.

    Use this only after compile_project reports a missing LaTeX file and the
    user explicitly confirms the package installation. Pass the MiKTeX package
    id you intend to install. This tool does not guess or rewrite package ids.
    If installation fails, search the web for the correct MiKTeX/CTAN package
    id, explain the new candidate to the user, ask for confirmation, and retry.
    """

    def _run() -> dict[str, Any]:
        validated_package_id = _validate_latex_package_id(package_id)
        if not confirmed:
            return {
                "success": False,
                "status": "confirmation_required",
                "package_id": validated_package_id,
                "message": (
                    "install_latex_packages requires confirmed=true after "
                    "explicit user approval"
                ),
            }

        command = ("miktex", "packages", "install", validated_package_id)
        started_at = time.perf_counter()
        result = run_command(command, cwd=REPO_ROOT, timeout=timeout)
        elapsed_time = round(time.perf_counter() - started_at, 3)
        return {
            "success": result.ok,
            "status": "installed" if result.ok else "install_failed",
            "package_id": validated_package_id,
            "command": list(result.command),
            "resolved_path": shutil.which(command[0]),
            "returncode": result.returncode,
            "elapsed_time": elapsed_time,
            "timed_out": result.timed_out,
            "not_found": result.not_found,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "server_cwd": str(Path.cwd()),
            "command_cwd": result.cwd,
            "message": (
                f"Installed LaTeX package: {validated_package_id}"
                if result.ok
                else _latex_package_install_failure_message(result)
            ),
        }

    return _run_tool("install_latex_packages", _run)


@_tool
def clean_latex_artifacts(
    project_dir: str,
    main_tex: str | None = None,
) -> dict[str, Any]:
    """Remove LaTeX intermediate build files for main_tex while keeping the PDF."""

    def _run() -> Any:
        resolved_project_dir = _resolve_project_dir(project_dir)
        resolved_main_tex = _resolve_cached_main_tex(resolved_project_dir, main_tex)
        return clean_project_latex_artifacts(
            project_dir=resolved_project_dir,
            main_tex=resolved_main_tex,
        )

    return _run_tool("clean_latex_artifacts", _run)



def _run_tool(action: str, callback: Callable[[], Any]) -> dict[str, Any]:
    try:
        result = callback()
    except Exception as exc:
        return {
            "success": False,
            "action": action,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "success": _infer_success(result),
        "action": action,
        "result": _jsonify(result),
    }


def _infer_success(result: Any) -> bool:
    if isinstance(result, dict) and "success" in result:
        return bool(result["success"])

    if hasattr(result, "success"):
        return bool(result.success)

    return True


def _jsonify(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonify(asdict(value))

    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item) for item in value]

    if isinstance(value, Path):
        return str(value)

    return value


def _resolve_project_dir(project_dir: str | Path) -> Path:
    raw_path = Path(project_dir).expanduser()
    candidates = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend(
            [
                WORKSPACE_ROOT / raw_path,
                REPO_ROOT / raw_path,
            ]
        )

    for candidate in candidates:
        resolved = candidate.resolve()
        if (
            _is_relative_to(resolved, WORKSPACE_ROOT)
            and resolved.exists()
            and resolved.is_dir()
        ):
            return resolved

    tried = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(
        "project_dir must exist inside the workspace and be a directory: "
        f"{project_dir}. Workspace: {WORKSPACE_ROOT}. Tried: {tried}"
    )


def _resolve_project_file_arg(
    project_dir: Path,
    file_path: str | Path,
    label: str,
) -> str:
    raw_path = Path(file_path).expanduser()
    resolved = raw_path.resolve() if raw_path.is_absolute() else (project_dir / raw_path).resolve()
    if not _is_relative_to(resolved, project_dir):
        raise ValueError(f"{label} must be inside project_dir")

    return str(resolved.relative_to(project_dir))


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False

    return True


def _environment_command(tool_name: str) -> tuple[str, ...]:
    normalized = tool_name.lower().strip()
    for name, command in ENVIRONMENT_TOOLS:
        if normalized == name:
            return command

    supported = ", ".join(name for name, _ in ENVIRONMENT_TOOLS)
    raise ValueError(f"unsupported tool_name: {tool_name}. Expected one of: {supported}")


def _project_key(project_dir: str | Path) -> str:
    return str(Path(project_dir).expanduser().resolve())


def _save_project_session(
    project_dir: str | Path,
    *,
    main_tex: str,
) -> dict[str, Any]:
    key = _project_key(project_dir)
    session = {
        "main_tex": main_tex,
        "prepared": True,
    }
    PROJECT_SESSIONS[key] = session
    return {"project_dir": key, **session}


def _resolve_cached_main_tex(project_dir: str | Path, main_tex: str | None) -> str:
    if main_tex:
        return _resolve_project_file_arg(Path(project_dir).expanduser().resolve(), main_tex, "main_tex")

    key = _project_key(project_dir)
    session = PROJECT_SESSIONS.get(key)
    cached_main_tex = session.get("main_tex") if session else None
    if isinstance(cached_main_tex, str) and cached_main_tex:
        return cached_main_tex

    detected = detect_main_tex(project_dir)
    if detected.main_tex is None:
        candidates = ", ".join(detected.candidates) or "(none)"
        raise FileNotFoundError(
            f"Could not infer main_tex for {key}. Candidates: {candidates}. "
            "Call prepare_project with an explicit main_tex."
        )

    return detected.main_tex


def _workspace_project_directories(*, max_depth: int) -> list[Path]:
    if not WORKSPACE_ROOT.exists():
        return []

    projects: set[Path] = set()
    for tex_file in WORKSPACE_ROOT.rglob("*.tex"):
        if not tex_file.is_file():
            continue

        relative_parts = tex_file.parent.relative_to(WORKSPACE_ROOT).parts
        if any(part in {".git", "__pycache__", ".venv", "venv", "node_modules"} for part in relative_parts):
            continue

        if len(relative_parts) == 0:
            projects.add(WORKSPACE_ROOT)
            continue

        top_level = WORKSPACE_ROOT / relative_parts[0]
        if _contains_main_like_tex(top_level):
            projects.add(top_level.resolve())
            continue

        depth = min(max_depth, len(relative_parts))
        projects.add((WORKSPACE_ROOT / Path(*relative_parts[:depth])).resolve())

    return sorted(projects, key=lambda path: path.relative_to(WORKSPACE_ROOT).as_posix().lower())


def _contains_main_like_tex(directory: Path) -> bool:
    if not directory.exists() or not directory.is_dir():
        return False

    for tex_file in directory.glob("*.tex"):
        if tex_file.is_file() and "\\documentclass" in _read_text_tolerant(tex_file):
            return True

    return False


def _read_text_tolerant(path: Path) -> str:
    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="replace")


def _validate_template_url(url: str) -> str:
    validated = url.strip()
    parsed = urlparse(validated)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("template URL must use http or https")

    if not parsed.netloc:
        raise ValueError("template URL must include a host")

    return validated


def _validate_project_name(project_name: str) -> str:
    validated = project_name.strip()
    if not validated:
        raise ValueError("project_name must not be empty")

    if validated in {".", ".."} or "/" in validated or "\\" in validated:
        raise ValueError("project_name must be a single directory name")

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", validated):
        raise ValueError(
            "project_name may only contain letters, numbers, underscore, dot, or hyphen"
        )

    return validated


def _template_kind_hint_from_url(url: str) -> str | None:
    path = unquote(urlparse(url).path).lower()
    if path.endswith(".zip"):
        return "zip"

    if path.endswith(".tar.gz") or path.endswith(".tgz"):
        return "tar"

    if path.endswith(".tex"):
        return "tex"

    return None


def _template_download_suffix(template_kind: str | None) -> str:
    if template_kind == "zip":
        return ".zip"

    if template_kind == "tar":
        return ".tar.gz"

    if template_kind == "tex":
        return ".tex"

    return ".download"


def _template_tex_filename(url: str) -> str:
    filename = Path(unquote(urlparse(url).path)).name
    if not filename or not filename.lower().endswith(".tex"):
        return "main.tex"

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
        return "main.tex"

    return filename



def _download_template_url(
    url: str,
    destination: Path,
    *,
    timeout: float,
    max_bytes: int,
) -> dict[str, Any]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    request = Request(url, headers={"User-Agent": "overleaf-agent/0.1"})
    total_bytes = 0
    started_at = time.perf_counter()
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type")
        content_disposition = response.headers.get("Content-Disposition")
        with destination.open("wb") as file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break

                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError(f"template download exceeds max_bytes={max_bytes}")

                file.write(chunk)

    filename_hint = _filename_from_content_disposition(content_disposition)
    template_kind_hint = (
        _template_kind_hint_from_filename(filename_hint)
        or _template_kind_hint_from_url(url)
    )
    template_kind = _detect_template_kind(
        destination,
        content_type=content_type,
        template_kind_hint=template_kind_hint,
    )

    return {
        "bytes": total_bytes,
        "content_type": content_type,
        "content_disposition": content_disposition,
        "filename_hint": filename_hint,
        "template_kind_hint": template_kind_hint,
        "template_kind": template_kind,
        "elapsed_time": round(time.perf_counter() - started_at, 3),
        "max_bytes": max_bytes,
    }


def _filename_from_content_disposition(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None

    match = re.search(r"filename\*?=(?:UTF-8''|\"?)(?P<filename>[^\";]+)", content_disposition, re.IGNORECASE)
    if match is None:
        return None

    filename = Path(unquote(match.group("filename").strip())).name
    return filename or None


def _template_kind_hint_from_filename(filename: str | None) -> str | None:
    if not filename:
        return None

    lower = filename.lower()
    if lower.endswith(".zip"):
        return "zip"

    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "tar"

    if lower.endswith(".tex"):
        return "tex"

    return None


def _detect_template_kind(
    path: Path,
    *,
    content_type: str | None,
    template_kind_hint: str | None,
) -> str:
    content_type_lower = (content_type or "").split(";", 1)[0].strip().lower()

    if zipfile.is_zipfile(path):
        return "zip"

    if tarfile.is_tarfile(path):
        return "tar"

    if content_type_lower in {
        "application/zip",
        "application/x-zip-compressed",
    }:
        return "zip"

    if content_type_lower in {
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
    }:
        return "tar"

    if _looks_like_tex_file(path):
        return "tex"

    if _looks_like_html_file(path):
        raise ValueError(
            "downloaded content looks like HTML, not a LaTeX template archive"
        )

    if template_kind_hint is not None:
        raise ValueError(
            f"downloaded content did not match URL/header template hint: {template_kind_hint}"
        )

    raise ValueError(
        "downloaded content is not a supported LaTeX template; expected zip, tar.gz, tgz, or tex"
    )


def _looks_like_tex_file(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return False

    return "\\documentclass" in sample or "\\begin{document}" in sample


def _looks_like_html_file(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:2048].lower()
    except OSError:
        return False

    stripped = sample.lstrip()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")


def _safe_extract_zip(zip_path: Path, target_dir: Path, *, max_files: int) -> None:
    target_root = target_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > max_files:
            raise ValueError(f"template archive contains more than {max_files} files")

        for member in members:
            destination = (target_root / member.filename).resolve()
            if not _is_relative_to(destination, target_root):
                raise ValueError(f"unsafe archive path: {member.filename}")

        archive.extractall(target_root)


def _safe_extract_tar(tar_path: Path, target_dir: Path, *, max_files: int) -> None:
    target_root = target_dir.resolve()
    with tarfile.open(tar_path, mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > max_files:
            raise ValueError(f"template archive contains more than {max_files} files")

        for member in members:
            if member.issym() or member.islnk():
                raise ValueError(f"archive links are not allowed: {member.name}")

            destination = (target_root / member.name).resolve()
            if not _is_relative_to(destination, target_root):
                raise ValueError(f"unsafe archive path: {member.name}")

        archive.extractall(target_root)


def _template_source_root(extract_dir: Path) -> Path:
    children = [
        child
        for child in extract_dir.iterdir()
        if child.name != "__MACOSX"
    ]
    if len(children) == 1 and children[0].is_dir():
        return children[0]

    return extract_dir


def _relative_project_files(project_dir: Path) -> list[str]:
    files: list[str] = []
    for path in project_dir.rglob("*"):
        if path.is_file():
            files.append(str(path.relative_to(project_dir)).replace("\\", "/"))

    return sorted(files)


def _check_environment(timeout: float = 5) -> dict[str, Any]:
    tools = []
    for name, command in ENVIRONMENT_TOOLS:
        started_at = time.perf_counter()
        result = run_command(command, cwd=REPO_ROOT, timeout=timeout)
        elapsed_time = round(time.perf_counter() - started_at, 3)
        tools.append(
            {
                "name": name,
                "ok": result.ok,
                "command": list(result.command),
                "resolved_path": shutil.which(command[0]),
                "returncode": result.returncode,
                "elapsed_time": elapsed_time,
                "timed_out": result.timed_out,
                "not_found": result.not_found,
                "version": _first_output_line(name, result.stdout, result.stderr) if result.ok else None,
                "error": None if result.ok else _failure_message(name, result),
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command_cwd": result.cwd,
            }
        )

    available = sum(1 for tool in tools if tool["ok"])
    total = len(tools)
    return {
        "ready": available == total,
        "available": available,
        "total": total,
        "server_cwd": str(Path.cwd()),
        "default_command_cwd": str(REPO_ROOT),
        "path_entries": os.environ.get("PATH", "").split(os.pathsep),
        "tools": tools,
    }


def _skipped_environment() -> dict[str, Any]:
    return {
        "ready": False,
        "available": 0,
        "total": len(ENVIRONMENT_TOOLS),
        "skipped": True,
        "server_cwd": str(Path.cwd()),
        "default_command_cwd": str(REPO_ROOT),
        "path_entries": os.environ.get("PATH", "").split(os.pathsep),
        "tools": [],
    }


def _first_output_line(name: str, stdout: str, stderr: str) -> str:
    output = stdout.strip() or stderr.strip()
    hints = VERSION_LINE_HINTS.get(name, ())
    for line in output.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if stripped and any(hint in lower for hint in hints):
            return stripped

    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped

    return "(no output)"


def _failure_message(name: str, result: Any) -> str:
    if result.not_found:
        return f"Command not found: {name}"

    if result.timed_out:
        return result.stderr.strip() or "Command timed out."

    return result.stderr.strip() or result.stdout.strip() or "(no error output)"


def _validate_latex_package_id(package_id: str) -> str:
    validated = package_id.strip()
    if not validated:
        raise ValueError("package_id must not be empty")

    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", validated):
        raise ValueError(
            "package_id may only contain letters, numbers, underscore, dot, plus, or hyphen"
        )

    return validated


def _latex_package_install_failure_message(result: Any) -> str:
    if result.not_found:
        return "MiKTeX command not found: miktex"

    if result.timed_out:
        return result.stderr.strip() or "MiKTeX package installation timed out."

    return (
        result.stderr.strip()
        or result.stdout.strip()
        or "MiKTeX package installation failed."
    )


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server over stdio or Streamable HTTP."""

    args = _parse_args(argv)

    if mcp is None:
        raise RuntimeError(
            "The MCP SDK is not installed. Install it with: pip install mcp"
        )

    transport = _normalize_transport(args.transport, args.http)

    if transport == "stdio":
        mcp.run()
        return

    _configure_http_transport(args.host, args.port, args.path)
    print(
        f"Starting overleaf-agent MCP over HTTP at http://{args.host}:{args.port}{args.path}",
        file=sys.stderr,
    )
    mcp.run(transport="streamable-http")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the overleaf-agent MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="Transport to use. Defaults to stdio.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Shortcut for --transport http.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="HTTP bind host.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8000")),
        help="HTTP bind port.",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("MCP_PATH", "/mcp"),
        help="HTTP MCP endpoint path.",
    )
    return parser.parse_args(argv)


def _normalize_transport(transport: str, http: bool) -> str:
    if http or transport == "http":
        return "streamable-http"

    return transport


def _configure_http_transport(host: str, port: int, path: str) -> None:
    if not path.startswith("/"):
        path = f"/{path}"

    settings = getattr(mcp, "settings", None)
    if settings is None:
        return

    if hasattr(settings, "host"):
        settings.host = host
    if hasattr(settings, "port"):
        settings.port = port
    if hasattr(settings, "streamable_http_path"):
        settings.streamable_http_path = path


if __name__ == "__main__":
    main()
