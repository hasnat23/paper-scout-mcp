"""arXiv API client (Atom feed) and full-text reader (arXiv's HTML rendering)."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

API = "https://export.arxiv.org/api/query"
HTML = "https://arxiv.org/html/{id}"
_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
_FIELD = re.compile(r"\b(ti|au|abs|co|jr|cat|rn|all):", re.IGNORECASE)


@dataclass
class Paper:
    id: str
    version: str
    title: str
    authors: list[str]
    abstract: str
    published: str
    updated: str
    categories: list[str] = field(default_factory=list)
    comment: str = ""

    @property
    def year(self) -> int:
        return int(self.published[:4])

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.id}"


@dataclass
class Section:
    title: str
    text: str


def parse_id(value: str) -> str:
    """Normalise '2401.15884v2', 'arXiv:2401.15884' or an arxiv.org URL to '2401.15884'."""
    match = _ID.search(value)
    if not match:
        raise ValueError(f"not an arXiv identifier: {value!r}")
    return match.group(1)


def build_query(query: str, category: str | None = None) -> str:
    """Turn free text into an arXiv query: every term must match (arXiv ORs bare terms).

    Queries that already use field prefixes (``ti:``, ``au:``, ``cat:`` ...) are passed through.
    """
    if _FIELD.search(query):
        q = query
    else:
        terms = re.findall(r'"[^"]+"|\S+', query)
        q = " AND ".join(f"all:{t}" for t in terms if t.upper() not in {"AND", "OR"})
    if category:
        q = f"({q}) AND cat:{category}"
    return q


def parse_feed(xml: str) -> list[Paper]:
    root = ET.fromstring(xml)
    papers = []
    for entry in root.findall("a:entry", _NS):
        raw_id = entry.findtext("a:id", default="", namespaces=_NS)
        if "/abs/" not in raw_id:
            continue  # arXiv returns an error entry for malformed queries
        match = _ID.search(raw_id.split("/abs/", 1)[1])
        papers.append(
            Paper(
                id=match.group(1),
                version=match.group(2) or "v1",
                title=_clean(entry.findtext("a:title", default="", namespaces=_NS)),
                authors=[_clean(a.findtext("a:name", default="", namespaces=_NS)) for a in entry.findall("a:author", _NS)],
                abstract=_clean(entry.findtext("a:summary", default="", namespaces=_NS)),
                published=entry.findtext("a:published", default="", namespaces=_NS)[:10],
                updated=entry.findtext("a:updated", default="", namespaces=_NS)[:10],
                categories=[c.get("term", "") for c in entry.findall("a:category", _NS)],
                comment=_clean(entry.findtext("arxiv:comment", default="", namespaces=_NS)),
            )
        )
    return papers


def parse_html(html: str) -> tuple[str, list[Section]]:
    """Split an arXiv HTML (LaTeXML) paper into (abstract, top-level sections)."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select("math annotation, .ltx_note_outer, .ltx_tag_note, script, style"):
        junk.decompose()
    abstract_node = soup.select_one("div.ltx_abstract")
    abstract = ""
    if abstract_node:
        heading = abstract_node.find(["h6", "h2"])
        if heading:
            heading.decompose()
        abstract = _clean(abstract_node.get_text(" "))
    sections = []
    for node in soup.select("section.ltx_section, section.ltx_appendix"):
        heading = node.find(["h2", "h3"])
        title = _clean(heading.get_text(" ")) if heading else node.get("id", "")
        if heading:
            heading.decompose()
        sections.append(Section(title, _clean(node.get_text(" "))))
    return abstract, sections


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class ArxivClient:
    """Small arXiv client. Respects arXiv's request to wait ~3 s between API calls."""

    def __init__(self, http: httpx.Client | None = None, min_interval: float = 3.0) -> None:
        self.http = http or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "paper-scout-mcp/0.1"})
        self.min_interval = min_interval
        self._last = 0.0
        self._html_cache: dict[str, tuple[str, list[Section]] | None] = {}

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def search(self, query: str, max_results: int = 10, sort: str = "relevance", category: str | None = None) -> list[Paper]:
        sort_by = {"relevance": "relevance", "newest": "submittedDate", "updated": "lastUpdatedDate"}[sort]
        self._throttle()
        response = self.http.get(
            API,
            params={
                "search_query": build_query(query, category),
                "max_results": max_results,
                "sortBy": sort_by,
                "sortOrder": "descending",
            },
        )
        response.raise_for_status()
        return parse_feed(response.text)

    def get(self, *ids: str) -> list[Paper]:
        self._throttle()
        response = self.http.get(API, params={"id_list": ",".join(parse_id(i) for i in ids), "max_results": len(ids)})
        response.raise_for_status()
        return parse_feed(response.text)

    def full_text(self, arxiv_id: str) -> tuple[str, list[Section]] | None:
        """Sections of the paper's HTML version, or None when arXiv has no HTML rendering."""
        key = parse_id(arxiv_id)
        if key not in self._html_cache:
            response = self.http.get(HTML.format(id=key))
            self._html_cache[key] = parse_html(response.text) if response.status_code == 200 else None
        return self._html_cache[key]
