from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eromatome_dl.models import Article

if TYPE_CHECKING:
    from eromatome_dl.http import HttpClient


class SiteParseError(ValueError):
    """Raised when a supported site's HTML cannot be parsed."""


@dataclass(frozen=True)
class SiteAdapter(ABC):
    name: str

    @abstractmethod
    def supports(self, url: str) -> bool:
        """Return True when this adapter should parse the URL."""

    @abstractmethod
    def parse(self, url: str, html: str) -> Article:
        """Parse article metadata and downloadable image URLs."""

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        """Fetch and parse an article.

        Adapters can override this when a site needs extra article page fetches.
        """

        from eromatome_dl.http import HttpClient

        http = client or HttpClient()
        response = http.get(url)
        return self.parse(url, response.text)
