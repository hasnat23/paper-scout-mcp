"""A local reading library (SQLite): saved papers, tags, notes, BibTeX export."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .arxiv import Paper

DEFAULT_PATH = Path(os.environ.get("PAPER_SCOUT_HOME", Path.home() / ".paper-scout")) / "library.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY, title TEXT, authors TEXT, year INTEGER, abstract TEXT,
    categories TEXT, added_at REAL
);
CREATE TABLE IF NOT EXISTS tags (paper_id TEXT, tag TEXT, PRIMARY KEY (paper_id, tag));
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, text TEXT, created_at REAL);
"""

_STOPWORDS = {"a", "an", "the", "on", "of", "for", "in", "to", "and", "with", "towards", "via"}


@dataclass
class Entry:
    id: str
    title: str
    authors: list[str]
    year: int
    abstract: str
    categories: list[str]
    tags: list[str]
    notes: list[str]


class Library:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(_SCHEMA)

    def save(self, paper: Paper, tags: list[str] | None = None, note: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO papers VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT added_at FROM papers WHERE id = ?), ?))",
                (paper.id, paper.title, json.dumps(paper.authors), paper.year, paper.abstract, json.dumps(paper.categories), paper.id, time.time()),
            )
            for tag in tags or []:
                self.db.execute("INSERT OR IGNORE INTO tags VALUES (?, ?)", (paper.id, tag.strip().lower()))
        if note:
            self.add_note(paper.id, note)

    def add_note(self, paper_id: str, text: str) -> None:
        if not self.has(paper_id):
            raise KeyError(f"{paper_id} is not in the library; save it first")
        with self.db:
            self.db.execute("INSERT INTO notes (paper_id, text, created_at) VALUES (?, ?, ?)", (paper_id, text, time.time()))

    def remove(self, paper_id: str) -> bool:
        with self.db:
            removed = self.db.execute("DELETE FROM papers WHERE id = ?", (paper_id,)).rowcount
            self.db.execute("DELETE FROM tags WHERE paper_id = ?", (paper_id,))
            self.db.execute("DELETE FROM notes WHERE paper_id = ?", (paper_id,))
        return bool(removed)

    def has(self, paper_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone() is not None

    def entries(self, tag: str | None = None, query: str | None = None) -> list[Entry]:
        sql = "SELECT id, title, authors, year, abstract, categories FROM papers"
        args: list = []
        clauses = []
        if tag:
            clauses.append("id IN (SELECT paper_id FROM tags WHERE tag = ?)")
            args.append(tag.lower())
        if query:
            clauses.append("(title LIKE ? OR abstract LIKE ?)")
            args += [f"%{query}%"] * 2
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY added_at DESC"
        out = []
        for pid, title, authors, year, abstract, categories in self.db.execute(sql, args).fetchall():
            tags = [r[0] for r in self.db.execute("SELECT tag FROM tags WHERE paper_id = ? ORDER BY tag", (pid,))]
            notes = [r[0] for r in self.db.execute("SELECT text FROM notes WHERE paper_id = ? ORDER BY id", (pid,))]
            out.append(Entry(pid, title, json.loads(authors), year, abstract, json.loads(categories), tags, notes))
        return out

    def tags(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT tag, COUNT(*) FROM tags GROUP BY tag ORDER BY COUNT(*) DESC").fetchall())

    def bibtex(self, ids: list[str] | None = None, tag: str | None = None) -> str:
        entries = self.entries(tag=tag)
        if ids:
            entries = [e for e in entries if e.id in set(ids)]
        return "\n\n".join(to_bibtex(e) for e in entries)


def citation_key(authors: list[str], year: int, title: str) -> str:
    last = re.sub(r"[^a-z]", "", (authors[0].split()[-1] if authors else "anon").lower())
    word = next((w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _STOPWORDS), "paper")
    return f"{last}{year}{word}"


def to_bibtex(e: Entry) -> str:
    fields = {
        "title": "{" + e.title + "}",
        "author": " and ".join(e.authors),
        "year": str(e.year),
        "eprint": e.id,
        "archivePrefix": "arXiv",
        "primaryClass": e.categories[0] if e.categories else "",
        "url": f"https://arxiv.org/abs/{e.id}",
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    return f"@misc{{{citation_key(e.authors, e.year, e.title)},\n{body}\n}}"
