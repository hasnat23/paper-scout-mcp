from pathlib import Path

import httpx
import pytest

from paper_scout import ArxivClient, Library, ScholarClient, Services, create_server
from paper_scout.arxiv import build_query, parse_feed, parse_html, parse_id
from paper_scout.library import citation_key

FIXTURES = Path(__file__).parent / "fixtures"
FEED = (FIXTURES / "arxiv_feed.xml").read_text()
HTML = (FIXTURES / "paper.html").read_text()

S2_DETAILS = {"citationCount": 420, "influentialCitationCount": 37, "venue": "arXiv", "tldr": {"text": "CRAG fixes bad retrieval."}}
S2_CITATIONS = {
    "data": [
        {"citingPaper": {"title": "Small follow-up", "year": 2025, "citationCount": 3, "externalIds": {}}},
        {"citingPaper": {"title": "Big follow-up", "year": 2024, "citationCount": 900, "externalIds": {"ArXiv": "2405.00001"}}},
        {"citingPaper": {"title": None}},
    ]
}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "export.arxiv.org" in url:
        return httpx.Response(200, text=FEED)
    if "arxiv.org/html/2401.15884" in url:
        return httpx.Response(200, text=HTML)
    if "arxiv.org/html/" in url:
        return httpx.Response(404)
    if url.endswith("/citations") or "/citations?" in url:
        return httpx.Response(200, json=S2_CITATIONS)
    if "semanticscholar" in url:
        return httpx.Response(200, json=S2_DETAILS)
    return httpx.Response(404)


@pytest.fixture
def services(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Services(ArxivClient(http, min_interval=0), ScholarClient(http), Library(tmp_path / "lib.db"))


class TestArxiv:
    def test_parse_id(self):
        for raw in ["2401.15884", "2401.15884v3", "arXiv:2401.15884", "https://arxiv.org/abs/2401.15884v1", "https://arxiv.org/pdf/2401.15884"]:
            assert parse_id(raw) == "2401.15884"
        assert parse_id("hep-th/9901001v2") == "hep-th/9901001"
        with pytest.raises(ValueError):
            parse_id("not an id")

    def test_build_query(self):
        assert build_query('corrective "retrieval augmented"') == 'all:corrective AND all:"retrieval augmented"'
        assert build_query("ti:CRAG AND au:Yan") == "ti:CRAG AND au:Yan"
        assert build_query("rag", category="cs.CL") == "(all:rag) AND cat:cs.CL"

    def test_parse_feed(self):
        papers = {p.id: p for p in parse_feed(FEED)}
        assert set(papers) == {"2401.15884", "2310.11511"}
        crag = papers["2401.15884"]
        assert crag.title == "Corrective Retrieval Augmented Generation"
        assert crag.year == 2024 and crag.authors[0] and "cs.CL" in crag.categories

    def test_parse_html(self):
        abstract, sections = parse_html(HTML)
        assert abstract == "LLMs hallucinate. We propose CRAG."
        assert [s.title for s in sections] == ["1 Introduction", "2 Method", "Appendix A Prompts"]
        assert "x^2" not in sections[0].text
        assert "Decompose then recompose." in sections[1].text  # subsections stay with their parent


class TestLibrary:
    def test_save_tag_note_export(self, tmp_path):
        lib = Library(tmp_path / "lib.db")
        paper = parse_feed(FEED)[0]
        lib.save(paper, tags=["RAG", "survey"], note="read later")
        lib.save(paper, tags=["rag"])  # idempotent
        [entry] = lib.entries(tag="rag")
        assert entry.tags == ["rag", "survey"] and entry.notes == ["read later"]
        assert lib.entries(query="nothing-like-this") == []
        bib = lib.bibtex(tag="rag")
        assert bib.startswith("@misc{") and f"eprint = {{{paper.id}}}" in bib
        assert lib.remove(paper.id) and not lib.has(paper.id)

    def test_note_requires_saved_paper(self, tmp_path):
        with pytest.raises(KeyError):
            Library(tmp_path / "lib.db").add_note("2401.15884", "x")

    def test_citation_key(self):
        assert citation_key(["Shi-Qi Yan", "Jia-Chen Gu"], 2024, "Corrective Retrieval Augmented Generation") == "yan2024corrective"
        assert citation_key([], 2020, "The A of B") == "anon2020b"


class TestScholar:
    def test_linked_sorted_and_filtered(self, services):
        linked = services.scholar.linked("2401.15884", limit=5)
        assert [p.title for p in linked] == ["Big follow-up", "Small follow-up"]
        assert linked[0].arxiv_id == "2405.00001"

    def test_bad_direction(self, services):
        with pytest.raises(ValueError):
            services.scholar.linked("2401.15884", direction="sideways")


def text_of(result) -> str:
    content = result[0] if isinstance(result, tuple) else result
    content = getattr(content, "content", content)
    return "\n".join(getattr(c, "text", str(c)) for c in content)


class TestServer:
    async def test_tools_registered(self, services):
        names = {t.name for t in await create_server(services).list_tools()}
        assert names == {
            "search_papers", "get_paper", "read_paper", "find_related", "save_paper",
            "add_note", "list_library", "remove_paper", "export_bibtex",
        }  # fmt: skip

    async def test_tool_annotations(self, services):
        tools = {t.name: t.annotations for t in await create_server(services).list_tools()}
        for name, a in tools.items():
            hints = (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint)
            assert None not in hints, f"{name} is missing a hint"
        read_only = {"search_papers", "get_paper", "read_paper", "find_related", "list_library", "export_bibtex"}
        assert {n for n, a in tools.items() if a.read_only_hint} == read_only
        assert {n for n, a in tools.items() if a.destructive_hint} == {"remove_paper"}
        assert {n for n, a in tools.items() if a.open_world_hint} == {"search_papers", "get_paper", "read_paper", "find_related", "save_paper"}

    async def test_research_flow(self, services):
        server = create_server(services)
        out = text_of(await server.call_tool("search_papers", {"query": "corrective rag"}))
        assert "arXiv:2401.15884" in out

        out = text_of(await server.call_tool("get_paper", {"arxiv_id": "2401.15884"}))
        assert "Citations: 420 (37 influential)" in out and "TL;DR: CRAG fixes bad retrieval." in out

        toc = text_of(await server.call_tool("read_paper", {"arxiv_id": "2401.15884"}))
        assert "2. 2 Method" in toc
        assert "retrieval evaluator" in text_of(await server.call_tool("read_paper", {"arxiv_id": "2401.15884", "section": "method"}))
        assert "No section matching" in text_of(await server.call_tool("read_paper", {"arxiv_id": "2401.15884", "section": "zzz"}))
        assert "no HTML version" in text_of(await server.call_tool("read_paper", {"arxiv_id": "1706.03762"}))

        assert "Big follow-up" in text_of(await server.call_tool("find_related", {"arxiv_id": "2401.15884"}))

        await server.call_tool("save_paper", {"arxiv_id": "2401.15884", "tags": ["rag"]})
        await server.call_tool("add_note", {"arxiv_id": "arXiv:2401.15884", "note": "evaluator is a T5"})
        listing = text_of(await server.call_tool("list_library", {"tag": "rag"}))
        assert "[rag]" in listing and "note: evaluator is a T5" in listing
        assert "@misc{" in text_of(await server.call_tool("export_bibtex", {"tag": "rag"}))
        assert text_of(await server.call_tool("remove_paper", {"arxiv_id": "2401.15884"})) == "Removed."
        assert text_of(await server.call_tool("list_library", {})) == "Library is empty."
