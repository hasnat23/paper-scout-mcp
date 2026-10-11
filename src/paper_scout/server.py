"""paper-scout MCP server: arXiv search, full-text reading, citation graphs and a local library."""

from __future__ import annotations

import textwrap

from mcp.server.mcpserver import MCPServer

from .arxiv import ArxivClient, Paper, parse_id
from .library import Library
from .scholar import ScholarClient

INSTRUCTIONS = """Research assistant tools for academic papers on arXiv.
Typical flow: search_papers -> get_paper (abstract, citations, TL;DR) -> read_paper (table of
contents, then one section at a time) -> find_related (citations/references) -> save_paper with
tags and notes -> export_bibtex. Always cite papers by arXiv id."""


class Services:
    """Lazily created clients, swappable in tests."""

    def __init__(self, arxiv: ArxivClient | None = None, scholar: ScholarClient | None = None, library: Library | None = None):
        self._arxiv, self._scholar, self._library = arxiv, scholar, library

    @property
    def arxiv(self) -> ArxivClient:
        self._arxiv = self._arxiv or ArxivClient()
        return self._arxiv

    @property
    def scholar(self) -> ScholarClient:
        self._scholar = self._scholar or ScholarClient()
        return self._scholar

    @property
    def library(self) -> Library:
        self._library = self._library or Library()
        return self._library


def _authors(authors: list[str], n: int = 3) -> str:
    return ", ".join(authors[:n]) + (" et al." if len(authors) > n else "")


def _card(p: Paper, abstract_chars: int = 400) -> str:
    abstract = textwrap.shorten(p.abstract, abstract_chars, placeholder=" ...")
    return f"**{p.title}** ({p.year})\narXiv:{p.id} · {_authors(p.authors)} · {', '.join(p.categories[:3])}\n{abstract}"


def create_server(services: Services | None = None) -> MCPServer:
    s = services or Services()

    def lookup(arxiv_id: str) -> Paper | None:
        wanted = parse_id(arxiv_id)
        return next((p for p in s.arxiv.get(wanted) if p.id == wanted), None)
    server = MCPServer("paper-scout", instructions=INSTRUCTIONS, version="0.1.0", log_level="WARNING")

    @server.tool()
    def search_papers(query: str, max_results: int = 8, sort: str = "relevance", category: str | None = None) -> str:
        """Search arXiv. `query` is free text (all terms must match) or arXiv syntax (ti:, au:, abs:, cat:).
        `sort`: relevance | newest | updated. `category` e.g. cs.CL, cs.LG, cs.IR."""
        papers = s.arxiv.search(query, max_results=min(max_results, 30), sort=sort, category=category)
        if not papers:
            return f"No arXiv results for {query!r}. Try fewer or broader terms."
        return "\n\n".join(f"{i}. {_card(p)}" for i, p in enumerate(papers, 1))

    @server.tool()
    def get_paper(arxiv_id: str) -> str:
        """Full metadata and abstract for one paper, plus citation count and TL;DR from Semantic Scholar."""
        p = lookup(arxiv_id)
        if p is None:
            return f"arXiv:{arxiv_id} not found."
        lines = [
            f"# {p.title}",
            f"arXiv:{p.id}{p.version} · published {p.published} · updated {p.updated}",
            f"Authors: {', '.join(p.authors)}",
            f"Categories: {', '.join(p.categories)}",
        ]
        if p.comment:
            lines.append(f"Comment: {p.comment}")
        try:
            info = s.scholar.details(p.id)
        except Exception:  # noqa: BLE001 - Semantic Scholar is best-effort
            info = None
        if info:
            lines.append(f"Citations: {info.get('citationCount', 0)} ({info.get('influentialCitationCount', 0)} influential)")
            if info.get("venue"):
                lines.append(f"Venue: {info['venue']}")
            if (info.get("tldr") or {}).get("text"):
                lines.append(f"TL;DR: {info['tldr']['text']}")
        lines += [f"Links: {p.url} · {p.pdf_url}", "", "## Abstract", p.abstract]
        return "\n".join(lines)

    @server.tool()
    def read_paper(arxiv_id: str, section: str | None = None, max_chars: int = 12000) -> str:
        """Read a paper's full text from arXiv's HTML version. Without `section`, returns the table of
        contents; pass a section number (e.g. "3") or a title fragment (e.g. "method") to read it."""
        doc = s.arxiv.full_text(arxiv_id)
        if doc is None:
            return f"arXiv has no HTML version of {arxiv_id} (older or non-LaTeX papers). Use get_paper for the abstract or open the PDF."
        abstract, sections = doc
        if section is None:
            toc = "\n".join(f"{i}. {sec.title}  ({len(sec.text):,} chars)" for i, sec in enumerate(sections, 1))
            return f"## Abstract\n{abstract}\n\n## Sections\n{toc}\n\nCall read_paper again with section=<number or title>."
        match = None
        if section.strip().isdigit() and 0 < int(section) <= len(sections):
            match = sections[int(section) - 1]
        else:
            match = next((sec for sec in sections if section.lower() in sec.title.lower()), None)
        if match is None:
            return f"No section matching {section!r}. Sections: " + "; ".join(sec.title for sec in sections)
        text = match.text if len(match.text) <= max_chars else match.text[:max_chars] + f"\n\n[truncated at {max_chars:,} chars]"
        return f"## {match.title}\n{text}"

    @server.tool()
    def find_related(arxiv_id: str, direction: str = "citations", limit: int = 10) -> str:
        """Most-cited papers that cite this one (`citations`) or that it cites (`references`)."""
        papers = s.scholar.linked(parse_id(arxiv_id), direction=direction, limit=min(limit, 50))
        if not papers:
            return f"No {direction} found for arXiv:{arxiv_id} (Semantic Scholar may be rate-limiting; set S2_API_KEY)."
        rows = [
            f"{i}. {p.title} ({p.year or '?'}) · {p.citations} citations" + (f" · arXiv:{p.arxiv_id}" if p.arxiv_id else "")
            for i, p in enumerate(papers, 1)
        ]
        return f"{direction.title()} of arXiv:{arxiv_id}, most cited first:\n" + "\n".join(rows)

    @server.tool()
    def save_paper(arxiv_id: str, tags: list[str] | None = None, note: str | None = None) -> str:
        """Save a paper to the local library, optionally with tags and a note."""
        p = lookup(arxiv_id)
        if p is None:
            return f"arXiv:{arxiv_id} not found."
        s.library.save(p, tags=tags, note=note)
        return f"Saved arXiv:{p.id} \"{p.title}\"" + (f" with tags {tags}" if tags else "")

    @server.tool()
    def add_note(arxiv_id: str, note: str) -> str:
        """Attach a note to a saved paper."""
        try:
            s.library.add_note(parse_id(arxiv_id), note)
        except KeyError as exc:
            return str(exc.args[0])
        return f"Note added to arXiv:{parse_id(arxiv_id)}."

    @server.tool()
    def list_library(tag: str | None = None, query: str | None = None) -> str:
        """List saved papers, optionally filtered by tag or a text query. Shows tags and notes."""
        entries = s.library.entries(tag=tag, query=query)
        if not entries:
            tags = s.library.tags()
            return "Library is empty." if not tags else f"No matches. Tags in library: {', '.join(tags)}"
        out = []
        for e in entries:
            block = f"- **{e.title}** ({e.year}) arXiv:{e.id}" + (f"  [{', '.join(e.tags)}]" if e.tags else "")
            block += "".join(f"\n    note: {n}" for n in e.notes)
            out.append(block)
        return f"{len(entries)} paper(s):\n" + "\n".join(out)

    @server.tool()
    def remove_paper(arxiv_id: str) -> str:
        """Remove a paper (and its tags and notes) from the library."""
        return "Removed." if s.library.remove(parse_id(arxiv_id)) else "Not in library."

    @server.tool()
    def export_bibtex(tag: str | None = None, arxiv_ids: list[str] | None = None) -> str:
        """BibTeX for saved papers: all of them, one tag, or specific ids."""
        bib = s.library.bibtex(ids=[parse_id(i) for i in arxiv_ids] if arxiv_ids else None, tag=tag)
        return bib or "No saved papers match."

    @server.prompt()
    def literature_review(topic: str, papers: int = 8) -> str:
        """Run a short literature review on a topic using the paper-scout tools."""
        return (
            f"Do a literature review on: {topic}\n"
            f"1. search_papers for the topic (try 2-3 phrasings); pick the {papers} most relevant papers.\n"
            "2. For the top 3, get_paper and read_paper (method + results sections).\n"
            "3. Use find_related on the most influential one to catch important follow-ups.\n"
            f"4. save_paper each one with the tag '{topic.lower()[:30]}' and a one-line note.\n"
            "5. Write a synthesis: main approaches, how they compare, open problems. Cite as arXiv:<id>.\n"
            "6. Finish with export_bibtex for the tag."
        )

    return server


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
