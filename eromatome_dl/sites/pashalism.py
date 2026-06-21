from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import re

from eromatome_dl.models import Article, ImageItem, dedupe_images, title_fallback_from_url
from eromatome_dl.sites.base import SiteAdapter, SiteParseError


IMAGE_EXTENSIONS = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|avif)(?:[?#].*)?$", re.IGNORECASE)
TITLE_CLASSES = {"single-post-title", "entry-title"}
SUPPORTED_HOSTS = {"pashalism.com", "www.pashalism.com"}


def _class_set(attrs: dict[str, str]) -> set[str]:
    return {part for part in attrs.get("class", "").split() if part}


def _is_image_url(url: str) -> bool:
    return bool(IMAGE_EXTENSIONS.search(urlparse(url).path))


class PashalismArticleParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self._in_title = False
        self._content_depth = 0
        self._content_done = False
        self._current_anchor_href: str | None = None
        self.images: list[ImageItem] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key.lower(): value or "" for key, value in attrs_list}
        classes = _class_set(attrs)

        if tag == "h1" and TITLE_CLASSES.issubset(classes):
            self._in_title = True

        if tag == "div" and not self._content_done:
            if self._content_depth:
                self._content_depth += 1
            elif "content" in classes:
                self._content_depth = 1
            return

        if not self._content_depth:
            return

        if tag == "a":
            href = attrs.get("href", "")
            absolute = urljoin(self.base_url, href)
            self._current_anchor_href = absolute if self._is_supported_image(absolute) else None
        elif tag == "img" and self._current_anchor_href:
            self.images.append(
                ImageItem(
                    ordinal=len(self.images) + 1,
                    url=self._current_anchor_href,
                    label=attrs.get("alt", ""),
                )
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_title:
            self._in_title = False

        if tag == "a" and self._content_depth:
            self._current_anchor_href = None

        if tag == "div" and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_done = True
                self._current_anchor_href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

    def _is_supported_image(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.lower() in SUPPORTED_HOSTS and _is_image_url(url)


class PashalismAdapter(SiteAdapter):
    def __init__(self) -> None:
        super().__init__(name="pashalism")

    def supports(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in SUPPORTED_HOSTS

    def parse(self, url: str, html: str) -> Article:
        parser = PashalismArticleParser(url)
        parser.feed(html)
        parser.close()

        title = parser.title or title_fallback_from_url(url)
        images = dedupe_images(parser.images)
        if not images:
            raise SiteParseError("No downloadable pashalism article images were found inside div.content.")

        return Article(url=url, title=title, images=images)
