"""Parse LaTeX paper structure from an indexed project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from overleaf_agent.project_indexer import ProjectIndex


SECTION_COMMAND_RE = re.compile(
    r"(?m)^\\(?P<level>section|subsection|subsubsection)\*?\{(?P<title>[^}]*)\}"
)
SECTION_LEVEL_RANK = {
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
}


@dataclass(frozen=True)
class PaperSection:
    """A section-like block inside a LaTeX source file."""

    file_path: str
    level: str
    title: str
    start_line: int
    end_line: int
    content: str | None = None


@dataclass(frozen=True)
class PaperMap:
    """Parsed structural map of a LaTeX paper."""

    project_dir: str
    main_tex: str
    tex_files: list[str]
    sections: list[PaperSection]


def parse_paper(
    project_index: ProjectIndex,
    *,
    include_content: bool = True,
) -> PaperMap:
    """Parse section structure from a project index.

    The parser is intentionally lightweight. It reads project ``.tex`` files and
    extracts ``\\section``, ``\\subsection``, and ``\\subsubsection`` blocks with
    line ranges. It does not interpret LaTeX macros or modify files.
    """

    sections: list[PaperSection] = []
    for file_path in _candidate_tex_files(project_index):
        content = _read_project_file(project_index, file_path)
        sections.extend(_parse_file_sections(file_path, content, include_content=include_content))

    return PaperMap(
        project_dir=project_index.project_dir,
        main_tex=project_index.main_tex,
        tex_files=project_index.tex_files,
        sections=sections,
    )


def _candidate_tex_files(project_index: ProjectIndex) -> list[str]:
    files = [project_index.main_tex]
    files.extend(project_index.included_tex_files)
    files.extend(project_index.tex_files)
    return list(dict.fromkeys(files))


def _parse_file_sections(
    file_path: str,
    content: str,
    *,
    include_content: bool,
) -> list[PaperSection]:
    matches = list(SECTION_COMMAND_RE.finditer(content))
    sections: list[PaperSection] = []
    for index, match in enumerate(matches):
        end_offset = _section_end(content, matches, index)
        section_content = content[match.start() : end_offset].strip() if include_content else None
        sections.append(
            PaperSection(
                file_path=file_path,
                level=match.group("level"),
                title=match.group("title").strip(),
                start_line=_line_number(content, match.start()),
                end_line=_end_line_number(content, end_offset),
                content=section_content,
            )
        )

    return sections


def _section_end(content: str, matches: list[re.Match[str]], index: int) -> int:
    current_rank = SECTION_LEVEL_RANK[matches[index].group("level")]
    for next_match in matches[index + 1 :]:
        next_rank = SECTION_LEVEL_RANK[next_match.group("level")]
        if next_rank <= current_rank:
            return next_match.start()

    return len(content)


def _read_project_file(project_index: ProjectIndex, file_path: str) -> str:
    path = Path(project_index.project_dir) / file_path
    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="replace")


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _end_line_number(content: str, end_offset: int) -> int:
    if end_offset <= 0:
        return 1

    return content.count("\n", 0, end_offset - 1) + 1
