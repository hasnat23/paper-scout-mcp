"""Semantic Scholar Graph API: citation counts, TL;DRs, citations and references.

Works without a key (shared, rate-limited pool). Set ``S2_API_KEY`` for a
dedicated rate limit.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

API = "https://api.semanticscholar.org/graph/v1"


@dataclass
class Linked:
    title: str
    year: int | None
    citations: int
    arxiv_id: str | None
    venue: str = ""


class ScholarClient:
    def __init__(self, http: httpx.Client | None = None, api_key: str | None = None, retries: int = 3) -> None:
        key = api_key or os.environ.get("S2_API_KEY")
        headers = {"User-Agent": "paper-scout-mcp/0.1", **({"x-api-key": key} if key else {})}
        self.http = http or httpx.Client(timeout=30, headers=headers)
        self.retries = retries

    def _get(self, path: str, **params) -> dict | None:
        for attempt in range(self.retries):
            response = self.http.get(f"{API}{path}", params=params)
            if response.status_code == 429 and attempt < self.retries - 1:
                time.sleep(2**attempt * 2)
                continue
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        return None

    def details(self, arxiv_id: str) -> dict | None:
        """citationCount, influentialCitationCount, venue and TL;DR for an arXiv paper."""
        return self._get(f"/paper/arXiv:{arxiv_id}", fields="citationCount,influentialCitationCount,venue,tldr")

    def linked(self, arxiv_id: str, direction: str = "citations", limit: int = 10) -> list[Linked]:
        """Papers citing this one (``citations``) or cited by it (``references``), most cited first."""
        if direction not in {"citations", "references"}:
            raise ValueError("direction must be 'citations' or 'references'")
        key = "citingPaper" if direction == "citations" else "citedPaper"
        data = self._get(
            f"/paper/arXiv:{arxiv_id}/{direction}",
            fields="title,year,citationCount,externalIds,venue",
            limit=min(max(limit * 5, 50), 1000),  # fetch extra, then rank by citations
        )
        papers = []
        for item in (data or {}).get("data", []):
            p = item.get(key) or {}
            if not p.get("title"):
                continue
            papers.append(
                Linked(
                    title=p["title"],
                    year=p.get("year"),
                    citations=p.get("citationCount") or 0,
                    arxiv_id=(p.get("externalIds") or {}).get("ArXiv"),
                    venue=p.get("venue") or "",
                )
            )
        papers.sort(key=lambda p: -p.citations)
        return papers[:limit]
