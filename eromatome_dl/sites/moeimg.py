from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse
import re

from eromatome_dl.models import Article, ImageItem, dedupe_images, title_fallback_from_url
from eromatome_dl.sites.base import SiteAdapter, SiteParseError


IMAGE_EXTENSIONS = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|avif)(?:[?#].*)?$", re.IGNORECASE)


def _class_set(attrs: dict[str, str]) -> set[str]:
    return {part for part in attrs.get("class", "").split() if part}


def _is_image_url(url: str) -> bool:
    return bool(IMAGE_EXTENSIONS.search(urlparse(url).path))


def _number_text(value: str) -> str:
    return "".join(value.split())


def _leading_filename_number(url: str) -> str:
    filename = PurePosixPath(unquote(urlparse(url).path)).name
    match = re.match(r"^(\d+)(?:\D|$)", filename)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class ImageCandidate:
    url: str
    alt: str


class MoeimgArticleParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.base_netloc = urlparse(base_url).netloc.lower()
        self.title_parts: list[str] = []
        self._in_title = False
        self._stop_images = False
        self._box_depth = 0
        self._current_anchor_href: str | None = None
        self._current_num_parts: list[str] = []
        self._current_num_depth = 0
        self._candidates: list[ImageCandidate] = []
        self.images: list[ImageItem] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key.lower(): value or "" for key, value in attrs_list}
        classes = _class_set(attrs)

        if tag == "h1" and "title" in classes:
            self._in_title = True

        if tag == "div" and "entry-footer" in classes:
            self._finalize_box()
            self._stop_images = True
            return

        if self._stop_images:
            return

        if tag == "div" and "box" in classes:
            self._reset_current_box()
            self._box_depth = 1
            return

        if self._box_depth:
            if tag == "div":
                self._box_depth += 1
                if "num" in classes:
                    self._current_num_depth = self._box_depth
            elif tag == "a":
                href = attrs.get("href", "")
                absolute = urljoin(self.base_url, href)
                self._current_anchor_href = absolute if self._is_article_host_image(absolute) else None
            elif tag == "img":
                src = attrs.get("src", "")
                absolute = urljoin(self.base_url, src)
                if src and self._is_article_host_image(absolute):
                    self._candidates.append(
                        ImageCandidate(
                            url=self._current_anchor_href or absolute,
                            alt=attrs.get("alt", ""),
                        )
                    )

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_title:
            self._in_title = False
        if tag == "a" and self._box_depth:
            self._current_anchor_href = None
        if tag == "div" and self._box_depth:
            if self._current_num_depth == self._box_depth:
                self._current_num_depth = 0
            self._box_depth -= 1
            if self._box_depth == 0:
                self._finalize_box()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._current_num_depth:
            self._current_num_parts.append(data)

    def _finalize_box(self) -> None:
        box_number = _number_text("".join(self._current_num_parts))
        if box_number.isdigit():
            for candidate in self._candidates:
                if self._candidate_matches_box(candidate, box_number):
                    self.images.append(ImageItem(len(self.images) + 1, candidate.url, candidate.alt))
                    break
        self._reset_current_box()

    def _candidate_matches_box(self, candidate: ImageCandidate, box_number: str) -> bool:
        return _number_text(candidate.alt) == box_number or _leading_filename_number(candidate.url) == box_number

    def _reset_current_box(self) -> None:
        self._box_depth = 0
        self._current_anchor_href = None
        self._current_num_parts = []
        self._current_num_depth = 0
        self._candidates = []

    def _is_article_host_image(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.lower() == self.base_netloc and _is_image_url(url)


class MoeimgAdapter(SiteAdapter):
    def __init__(self) -> None:
        super().__init__(name="moeimg")

    def supports(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "moeimg.net"

    def parse(self, url: str, html: str) -> Article:
        parser = MoeimgArticleParser(url)
        parser.feed(html)
        parser.close()

        title = parser.title or title_fallback_from_url(url)
        images = dedupe_images(parser.images)
        if not images:
            raise SiteParseError("No downloadable moeimg article images were found before entry-footer.")

        return Article(url=url, title=title, images=images)
