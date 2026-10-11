"""paper-scout: an MCP server for searching, reading and organising arXiv papers."""

from .arxiv import ArxivClient, Paper, Section
from .library import Library
from .scholar import ScholarClient
from .server import Services, create_server

__version__ = "0.1.0"
__all__ = ["ArxivClient", "Library", "Paper", "ScholarClient", "Section", "Services", "create_server"]
