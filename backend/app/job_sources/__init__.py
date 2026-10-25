"""Public job-source connectors behind one interface (see base.py)."""
from .base import (
    BoardFetch,
    HttpClient,
    NormalizedPosting,
    SourceError,
)
from .registry import SOURCES, SourceInfo, get_connector, source_info

__all__ = [
    "BoardFetch",
    "HttpClient",
    "NormalizedPosting",
    "SOURCES",
    "SourceError",
    "SourceInfo",
    "get_connector",
    "source_info",
]
