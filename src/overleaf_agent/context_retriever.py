"""Retrieve relevant LaTeX context for a user query."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from overleaf_agent.paper_parser import PaperMap, PaperSection, parse_paper
from overleaf_agent.project_indexer import ProjectIndex


STOPWORDS = {
    "about",
    "and",
    "for",
    "from",
    "into",
    "please",
    "that",
    "the",
    "this",
    "with",
    "一下",
    "什么",
    "帮我",
    "看看",
}


@dataclass(frozen=True)
class RetrievedChunk:
    """A selected file or section and its text content."""

    file_path: str
    content: str
    reason: str
    score: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class RetrievedSection:
    """Section metadata returned with retrieved context."""

    file_path: str
    level: str
    title: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class RetrievedContext:
    """Context selected for a user query."""

    query: str
    selected_files: list[str]
    chunks: list[RetrievedChunk]
    paper_sections: list[RetrievedSection] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_reason: str | None = None


def retrieve_context(
    project_index: ProjectIndex,
    query: str,
    *,
    max_files: int = 5,
) -> RetrievedContext:
    """Select section content from a project index.

    If the query matches real section or subsection titles, this returns those
    section contents. If no title matches, it returns no chunks but still returns
    the paper section map so the caller can choose an exact title and call again.
    For very early projects with no parsed sections, it falls back to main and
    directly included TeX files.
    """

    if max_files <= 0:
        raise ValueError("max_files must be greater than 0")

    paper_map = parse_paper(project_index, include_content=True)
    section_matches = _score_sections(paper_map, _query_terms(query))
    if section_matches:
        chunks = _chunks_from_section_matches(section_matches[:max_files])
    elif not paper_map.sections:
        chunks = _default_file_chunks(project_index, max_files)
    else:
        chunks = []

    return RetrievedContext(
        query=query,
        selected_files=_unique_files(chunk.file_path for chunk in chunks),
        chunks=chunks,
        paper_sections=_section_metadata(paper_map),
    )


def _score_sections(
    paper_map: PaperMap,
    query_terms: set[str],
) -> list[tuple[int, PaperSection]]:
    scored: list[tuple[int, PaperSection]] = []
    for section in paper_map.sections:
        title_terms = _title_terms(section.title)
        score = len(query_terms & title_terms)
        if score > 0:
            scored.append((20 + score, section))

    scored.sort(
        key=lambda item: (
            -item[0],
            _section_level_rank(item[1].level),
            item[1].file_path,
            item[1].start_line,
        )
    )
    return scored


def _chunks_from_section_matches(
    section_matches: list[tuple[int, PaperSection]],
) -> list[RetrievedChunk]:
    chunks: list[RetrievedChunk] = []
    for score, section in section_matches:
        chunks.append(
            RetrievedChunk(
                file_path=section.file_path,
                content=section.content or "",
                reason=(
                    f"matched paper {section.level} title: {section.title} "
                    f"(lines {section.start_line}-{section.end_line})"
                ),
                score=score,
                start_line=section.start_line,
                end_line=section.end_line,
            )
        )

    return chunks


def _default_file_chunks(project_index: ProjectIndex, max_files: int) -> list[RetrievedChunk]:
    files = [project_index.main_tex]
    files.extend(project_index.included_tex_files)
    unique_files = list(dict.fromkeys(files))

    chunks: list[RetrievedChunk] = []
    for file_path in unique_files[:max_files]:
        content = _read_project_file(project_index, file_path)
        chunks.append(
            RetrievedChunk(
                file_path=file_path,
                content=content,
                reason="fallback: project has no parsed sections",
                score=1,
                start_line=1,
                end_line=_file_end_line(content),
            )
        )

    return chunks


def _section_level_rank(level: str) -> int:
    ranks = {
        "section": 1,
        "subsection": 2,
        "subsubsection": 3,
    }
    return ranks.get(level, 100)


def _query_terms(query: str) -> set[str]:
    normalized = query.lower()
    raw_terms = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized)
    return {term for term in raw_terms if len(term) > 1 and term not in STOPWORDS}


def _title_terms(title: str) -> set[str]:
    terms = _query_terms(title)
    acronym = "".join(word[0] for word in re.findall(r"[A-Za-z]+", title)).lower()
    if len(acronym) > 1:
        terms.add(acronym)

    return terms


def _read_project_file(project_index: ProjectIndex, file_path: str) -> str:
    path = Path(project_index.project_dir) / file_path
    for encoding in ("utf-8", "cp936", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="replace")


def _file_end_line(content: str) -> int:
    if not content:
        return 1

    return content.count("\n", 0, len(content) - 1) + 1


def _section_metadata(paper_map: PaperMap) -> list[RetrievedSection]:
    return [
        RetrievedSection(
            file_path=section.file_path,
            level=section.level,
            title=section.title,
            start_line=section.start_line,
            end_line=section.end_line,
        )
        for section in paper_map.sections
    ]


def _unique_files(file_paths) -> list[str]:
    return list(dict.fromkeys(file_paths))
