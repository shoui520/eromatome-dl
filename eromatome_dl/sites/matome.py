from __future__ import annotations

from html.parser import HTMLParser
from posixpath import normpath
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlsplit, urlunsplit
import re

from eromatome_dl.http import DownloadError, HttpClient, JETPACK_IMAGE_HOSTS, RedirectBlocked, jetpack_origin_url
from eromatome_dl.models import Article, ImageItem, dedupe_images, title_fallback_from_url
from eromatome_dl.sites.base import SiteAdapter, SiteParseError


IMAGE_EXTENSIONS = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|avif)(?:[?#].*)?$", re.IGNORECASE)
IE_CONDITIONAL_COMMENT_START = re.compile(r"<!--\[if\s[^>]*\]>", re.IGNORECASE)
IE_CONDITIONAL_COMMENT_END = re.compile(r"<!\[endif\]-->", re.IGNORECASE)
NIJIMOE_EROGAZOU_PUNYCODE_HOST = "xn--r8jwklh769h2mc880dk1o431a.com"
NIJIMOE_EROGAZOU_UNICODE_HOST = "二次萌えエロ画像.com"
DEBUSEN_PUNYCODE_HOST = "xn--edk4a626w.net"
DEBUSEN_UNICODE_HOST = "デブ専.net"


def _attrs(attrs_list: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key.lower(): value or "" for key, value in attrs_list}


def _class_set(attrs: dict[str, str]) -> set[str]:
    return {part for part in attrs.get("class", "").split() if part}


def _has_classes(classes: set[str], *required: str) -> bool:
    return set(required).issubset(classes)


def _is_image_url(url: str) -> bool:
    return bool(IMAGE_EXTENSIONS.search(urlparse(url).path))


def _srcset_candidates(value: str) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for raw_candidate in value.split(","):
        parts = raw_candidate.strip().split()
        if not parts:
            continue
        weight = 0
        if len(parts) > 1 and parts[1].endswith("w") and parts[1][:-1].isdigit():
            weight = int(parts[1][:-1])
        candidates.append((weight, parts[0]))
    return candidates


def _hostname_root(hostname: str) -> str:
    parts = hostname.lower().split(".")
    if len(parts) <= 2:
        return hostname.lower()
    if len(parts[-2]) <= 3 and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _strip_ie_conditional_markers(html: str) -> str:
    html = IE_CONDITIONAL_COMMENT_START.sub("", html)
    return IE_CONDITIONAL_COMMENT_END.sub("", html)


def _iri_to_uri(url: str) -> str:
    split = urlsplit(url)
    hostname = split.hostname
    if not hostname:
        return url

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return url

    netloc = ascii_host
    if split.port:
        netloc = f"{netloc}:{split.port}"
    if split.username:
        userinfo = quote(split.username, safe="%")
        if split.password is not None:
            userinfo = f"{userinfo}:{quote(split.password, safe='%')}"
        netloc = f"{userinfo}@{netloc}"

    return urlunsplit(
        (
            split.scheme,
            netloc,
            quote(split.path, safe="/%"),
            quote(split.query, safe="=&;%:+,/?"),
            quote(split.fragment, safe="%"),
        )
    )


class BaseArticleParser(HTMLParser):
    title_class_sets: tuple[frozenset[str], ...] = ()

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self._in_title = False
        self.images: list[ImageItem] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = _attrs(attrs_list)
        classes = _class_set(attrs)

        if tag == "h1" and self._is_title(classes):
            self._in_title = True

        self._handle_starttag(tag, attrs, classes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_title:
            self._in_title = False

        self._handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

        self._handle_data(data)

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        pass

    def _handle_endtag(self, tag: str) -> None:
        pass

    def _handle_data(self, data: str) -> None:
        pass

    def _is_title(self, classes: set[str]) -> bool:
        return any(required.issubset(classes) for required in self.title_class_sets)

    def _supported_image_url(self, value: str, allowed_hosts: set[str] | frozenset[str]) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() not in allowed_hosts:
            return None
        if not _is_image_url(absolute):
            return None
        return absolute

    def _add_image(self, url: str, label: str = "") -> None:
        self.images.append(ImageItem(len(self.images) + 1, url, label))


class ParserBackedAdapter(SiteAdapter):
    hosts: frozenset[str] = frozenset()
    parser_cls: type[BaseArticleParser]
    image_context = "article content"

    def supports(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in self.hosts

    def parse(self, url: str, html: str) -> Article:
        parser = self.parser_cls(url)
        parser.feed(html)
        parser.close()

        title = parser.title or title_fallback_from_url(url)
        images = dedupe_images(parser.images)
        if not images:
            raise SiteParseError(f"No downloadable {self.name} article images were found in {self.image_context}.")

        return Article(url=url, title=title, images=images)


class KimootokoArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"c-postTitle__ttl"}),)
    image_hosts = frozenset({"kimootoko.net", "www.kimootoko.net"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "arasuji" in classes:
            self._stop_images = True
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img" and self._current_anchor_href:
            self._add_image(self._current_anchor_href, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current_anchor_href = None


class KimootokoAdapter(ParserBackedAdapter):
    hosts = KimootokoArticleParser.image_hosts
    parser_cls = KimootokoArticleParser
    image_context = "the article before div.arasuji"

    def __init__(self) -> None:
        super().__init__(name="kimootoko")


class IchinukeArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"c-postTitle__ttl"}),)
    image_hosts = frozenset({"ichinuke.com", "www.ichinuke.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._figure_depth = 0

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "figure":
            self._figure_depth += 1
            return

        if not self._figure_depth:
            return

        if tag == "img":
            image_url = (
                self._supported_image_url(attrs.get("data-luminous", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "figure" and self._figure_depth:
            self._figure_depth -= 1


class IchinukeAdapter(ParserBackedAdapter):
    hosts = IchinukeArticleParser.image_hosts
    parser_cls = IchinukeArticleParser
    image_context = "figure image blocks"

    def __init__(self) -> None:
        super().__init__(name="ichinuke")


class EroconArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"article-title"}),)
    image_hosts = frozenset({"livedoor.blogimg.jp"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._imgbox_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "article-sub-category" in classes:
            self._stop_images = True
            self._imgbox_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._imgbox_depth:
                self._imgbox_depth += 1
            elif "imgbox" in classes:
                self._imgbox_depth = 1
            return

        if not self._imgbox_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = self._current_anchor_href or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._imgbox_depth:
            self._current_anchor_href = None
        elif tag == "div" and self._imgbox_depth:
            self._imgbox_depth -= 1


class EroconAdapter(ParserBackedAdapter):
    hosts = frozenset({"erocon.gger.jp"})
    parser_cls = EroconArticleParser
    image_context = "imgbox blocks before article-sub-category"

    def __init__(self) -> None:
        super().__init__(name="erocon")


class NijifanArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"nijifan.net", "www.nijifan.net"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._container_depth = 0
        self._current_modal_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div":
            if self._container_depth:
                self._container_depth += 1
            elif "bialty-container" in classes:
                self._container_depth = 1
            return

        if not self._container_depth:
            return

        if tag == "a" and "td-modal-image" in classes:
            self._current_modal_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img" and self._current_modal_href:
            self._add_image(self._current_modal_href, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._container_depth:
            self._current_modal_href = None
        elif tag == "div" and self._container_depth:
            self._container_depth -= 1
            if self._container_depth == 0:
                self._current_modal_href = None


class NijifanAdapter(ParserBackedAdapter):
    hosts = NijifanArticleParser.image_hosts
    parser_cls = NijifanArticleParser
    image_context = "div.bialty-container"

    def __init__(self) -> None:
        super().__init__(name="nijifan")


class EntryContentAnchorImageParser(BaseArticleParser):
    content_class = "entry-content"
    stop_start_classes: frozenset[str] = frozenset()
    image_hosts: frozenset[str] = frozenset()

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._content_done = False
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if self._content_depth and tag == "div" and self.stop_start_classes and self.stop_start_classes.issubset(classes):
            self._content_done = True
            self._content_depth = 0
            self._current_anchor_href = None
            return

        if tag == "div" and not self._content_done:
            if self._content_depth:
                self._content_depth += 1
            elif self.content_class in classes:
                self._content_depth = 1
            return

        if not self._content_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img" and self._current_anchor_href:
            self._add_image(self._current_anchor_href, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._content_depth:
            self._current_anchor_href = None
        elif tag == "div" and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_done = True
                self._current_anchor_href = None


class BeppinGirlArticleParser(EntryContentAnchorImageParser):
    title_class_sets = (frozenset({"entry-title"}),)
    stop_start_classes = frozenset({"freebox", "has-title"})
    image_hosts = frozenset({"beppin-girl.com", "www.beppin-girl.com"})


class BeppinGirlAdapter(ParserBackedAdapter):
    hosts = BeppinGirlArticleParser.image_hosts
    parser_cls = BeppinGirlArticleParser
    image_context = "div.entry-content before div.freebox.has-title"

    def __init__(self) -> None:
        super().__init__(name="beppin-girl")


class BakufuArticleParser(EntryContentAnchorImageParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"img.bakufu.jp"})


class BakufuAdapter(ParserBackedAdapter):
    hosts = frozenset({"bakufu.jp", "www.bakufu.jp"})
    parser_cls = BakufuArticleParser
    image_context = "entry-content img.bakufu.jp image links"

    def __init__(self) -> None:
        super().__init__(name="bakufu")


class SengiribestArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"1000giribest.com", "www.1000giribest.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._content_done = False

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and not self._content_done:
            if self._content_depth:
                self._content_depth += 1
            elif "entry-content" in classes:
                self._content_depth = 1
            return

        if self._content_depth and tag == "img":
            image_url = self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_done = True


class SengiribestAdapter(ParserBackedAdapter):
    hosts = SengiribestArticleParser.image_hosts
    parser_cls = SengiribestArticleParser
    image_context = "div.entry-content"

    def __init__(self) -> None:
        super().__init__(name="1000giribest")


class ItaDoArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"boxTitle02", "titleColor_red", "h1Text"}),)
    image_hosts = frozenset({"ita-do.com", "www.ita-do.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._stop_images = False

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if self._content_depth and "singleAtode" in classes:
            self._stop_images = True
            self._content_depth = 0
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._content_depth:
                self._content_depth += 1
            elif _has_classes(classes, "box", "singleBox"):
                self._content_depth = 1
            return

        if self._content_depth and tag == "img":
            image_url = (
                self._supported_image_url(attrs.get("data-lazy-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._content_depth:
            self._content_depth -= 1


class ItaDoAdapter(ParserBackedAdapter):
    hosts = ItaDoArticleParser.image_hosts
    parser_cls = ItaDoArticleParser
    image_context = "div.box.singleBox before div.singleAtode"

    def __init__(self) -> None:
        super().__init__(name="ita-do")

    def parse(self, url: str, html: str) -> Article:
        return super().parse(url, _strip_ie_conditional_markers(html))


class LoveliveforeverArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"loveliveforever.com", "www.loveliveforever.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._content_done = False

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and not self._content_done:
            if self._content_depth:
                self._content_depth += 1
            elif attrs.get("id") == "the-content" and "entry-content" in classes:
                self._content_depth = 1
            return

        if self._content_depth and tag == "img":
            image_url = (
                self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_done = True


class LoveliveforeverAdapter(ParserBackedAdapter):
    hosts = LoveliveforeverArticleParser.image_hosts
    parser_cls = LoveliveforeverArticleParser
    image_context = "div#the-content.entry-content"

    def __init__(self) -> None:
        super().__init__(name="loveliveforever")


class MegamichArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"Single-Heading"}),)
    image_hosts = frozenset({"megamich.com", "www.megamich.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._hero_depth = 0
        self._entry_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "pagerarea" in classes:
            self._stop_images = True
            self._hero_depth = 0
            self._entry_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "figure":
            if self._hero_depth:
                self._hero_depth += 1
            elif "Single-Eyecatch" in classes:
                self._hero_depth = 1
            return

        if tag == "li":
            if self._entry_depth:
                self._entry_depth += 1
            elif re.match(r"^num\d+$", attrs.get("id", "")):
                self._entry_depth = 1
            return

        if self._hero_depth and tag == "img":
            image_url = self._supported_megamich_image_url(attrs.get("src", ""))
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))
            return

        if not self._entry_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_megamich_image_url(attrs.get("href", ""))
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_megamich_image_url(attrs.get("data-src", ""))
                or self._supported_megamich_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "figure" and self._hero_depth:
            self._hero_depth -= 1
        elif tag == "a" and self._entry_depth:
            self._current_anchor_href = None
        elif tag == "li" and self._entry_depth:
            self._entry_depth -= 1
            if self._entry_depth == 0:
                self._current_anchor_href = None

    def _supported_megamich_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() not in self.image_hosts:
            return None

        if parsed.path.startswith("/wp-content/uploads/img/") and _is_image_url(absolute):
            return absolute

        if parsed.path == "/wp-content/plugins/c-tool/thumb.php" and parse_qs(parsed.query).get("image"):
            return absolute

        return None


def _megamich_page_url(url: str, page_number: int) -> str:
    split = urlsplit(url)
    base_path = re.sub(r"_\d+(\.html)$", r"\1", split.path, flags=re.IGNORECASE)
    if not base_path.lower().endswith(".html"):
        return urlunsplit((split.scheme, split.netloc, split.path, "", ""))

    path = re.sub(r"(\.html)$", f"_{page_number}.html", base_path, flags=re.IGNORECASE)
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


class MegamichAdapter(ParserBackedAdapter):
    hosts = MegamichArticleParser.image_hosts
    parser_cls = MegamichArticleParser
    image_context = "figure.Single-Eyecatch and li#num images before div.pagerarea"
    max_pages = 200

    def __init__(self) -> None:
        super().__init__(name="megamich")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        http = client or HttpClient()
        first_response = http.get(url)
        first_article = self.parse(url, first_response.text)
        images = list(first_article.images)

        for page_number in range(2, self.max_pages + 1):
            page_url = _megamich_page_url(url, page_number)
            try:
                response = http.get(page_url, referer=url)
                page_article = self.parse(page_url, response.text)
            except RedirectBlocked:
                break
            except DownloadError as exc:
                if "HTTP 404" in str(exc):
                    break
                raise
            except SiteParseError:
                break

            merged = list(dedupe_images([*images, *page_article.images]))
            if len(merged) == len(images):
                break
            images = merged

        return Article(url=url, title=first_article.title, images=dedupe_images(images))


class EropuruArticleParser(BaseArticleParser):
    title_class_sets = (frozenset(),)
    article_hosts = frozenset({"eropuru.com", "www.eropuru.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._image_item_depth = 0

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "element_pagenavi" in classes and self.images:
            self._stop_images = True
            self._image_item_depth = 0
            return

        if self._stop_images:
            return

        if tag == "li":
            if self._image_item_depth:
                self._image_item_depth += 1
            elif attrs.get("data-img-id"):
                self._image_item_depth = 1
            return

        if self._image_item_depth and tag == "img":
            image_url = (
                self._supported_eropuru_image_url(attrs.get("data-src", ""))
                or self._supported_eropuru_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._image_item_depth:
            self._image_item_depth -= 1

    def _supported_eropuru_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        if not re.match(r"^img\d*\.eropuru\.com$", parsed.netloc.lower()):
            return None
        if not _is_image_url(absolute):
            return None
        return absolute


def _eropuru_page_url(url: str, page_number: int) -> str:
    split = urlsplit(url)
    query_parts = [part for part in split.query.split("&") if part and not part.startswith("p=")]
    query_parts.append(f"p={page_number}")
    return urlunsplit((split.scheme, split.netloc, split.path, "&".join(query_parts), split.fragment))


class EropuruAdapter(ParserBackedAdapter):
    hosts = EropuruArticleParser.article_hosts
    parser_cls = EropuruArticleParser
    image_context = "li[data-img-id] images before the bottom div.element_pagenavi"
    max_pages = 200

    def __init__(self) -> None:
        super().__init__(name="eropuru")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        http = client or HttpClient()
        first_response = http.get(url)
        first_article = self.parse(url, first_response.text)
        images = list(first_article.images)

        for page_number in range(2, self.max_pages + 1):
            page_url = _eropuru_page_url(url, page_number)
            try:
                response = http.get(page_url, referer=url)
                page_article = self.parse(page_url, response.text)
            except RedirectBlocked:
                break
            except DownloadError as exc:
                if "HTTP 400" in str(exc) or "HTTP 404" in str(exc):
                    break
                raise
            except SiteParseError:
                break

            merged = list(dedupe_images([*images, *page_article.images]))
            if len(merged) == len(images):
                break
            images = merged

        return Article(url=url, title=first_article.title, images=dedupe_images(images))


class NijimoeEroGazouArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"c-postTitle__ttl"}),)
    image_hosts = frozenset({NIJIMOE_EROGAZOU_PUNYCODE_HOST})

    def __init__(self, base_url: str) -> None:
        super().__init__(_iri_to_uri(base_url))
        self._article_started = False
        self._stop_images = False

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "swell-block-button" in classes:
            self._stop_images = True
            return

        if self._stop_images or not self._article_started:
            return

        if tag == "img":
            image_url = (
                self._supported_nijimoe_image_url(attrs.get("data-src", ""))
                or self._supported_nijimoe_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self.title_parts:
            self._article_started = True

    def _supported_nijimoe_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = _iri_to_uri(urljoin(self.base_url, value))
        parsed = urlparse(absolute)
        if parsed.netloc.lower() not in self.image_hosts:
            return None
        if not _is_image_url(absolute):
            return None
        return absolute


class NijimoeEroGazouAdapter(ParserBackedAdapter):
    hosts = frozenset({NIJIMOE_EROGAZOU_PUNYCODE_HOST, NIJIMOE_EROGAZOU_UNICODE_HOST})
    parser_cls = NijimoeEroGazouArticleParser
    image_context = "article images after h1.c-postTitle__ttl before div.swell-block-button"

    def __init__(self) -> None:
        super().__init__(name="nijimoe-erogazou")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        fetch_url = _iri_to_uri(url)
        http = client or HttpClient()
        response = http.get(fetch_url)
        article = self.parse(fetch_url, response.text)
        return Article(url=url, title=article.title, images=article.images)


class KyarabetsuNijieroArticleParser(BaseArticleParser):
    title_class_sets = (frozenset(),)
    image_hosts = frozenset({"kyarabetsunijiero.net", "www.kyarabetsunijiero.net"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._ignored_figure_depth = 0
        self._image_figure_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "pager" in classes and self.images:
            self._stop_images = True
            self._ignored_figure_depth = 0
            self._image_figure_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "figure":
            if self._ignored_figure_depth:
                self._ignored_figure_depth += 1
            elif "eye-catch" in classes:
                self._ignored_figure_depth = 1
                self._current_anchor_href = None
            elif self._image_figure_depth:
                self._image_figure_depth += 1
            elif "size-large" in classes:
                self._image_figure_depth = 1
            return

        if self._ignored_figure_depth or not self._image_figure_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._image_figure_depth:
            self._current_anchor_href = None
        elif tag == "figure":
            if self._ignored_figure_depth:
                self._ignored_figure_depth -= 1
            elif self._image_figure_depth:
                self._image_figure_depth -= 1
                if self._image_figure_depth == 0:
                    self._current_anchor_href = None


def _kyarabetsu_page_url(url: str, page_number: int) -> str:
    split = urlsplit(url)
    query_parts = [part for part in split.query.split("&") if part and not part.startswith("pg=")]
    query_parts.append(f"pg={page_number}")
    return urlunsplit((split.scheme, split.netloc, split.path, "&".join(query_parts), split.fragment))


class KyarabetsuNijieroAdapter(ParserBackedAdapter):
    hosts = KyarabetsuNijieroArticleParser.image_hosts
    parser_cls = KyarabetsuNijieroArticleParser
    image_context = "figure.size-large images before the bottom div.pager"
    max_pages = 200

    def __init__(self) -> None:
        super().__init__(name="kyarabetsunijiero")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        http = client or HttpClient()
        first_response = http.get(url)
        first_article = self.parse(url, first_response.text)
        images = list(first_article.images)

        for page_number in range(2, self.max_pages + 1):
            page_url = _kyarabetsu_page_url(url, page_number)
            try:
                response = http.get(page_url, referer=url)
                page_article = self.parse(page_url, response.text)
            except RedirectBlocked:
                break
            except DownloadError as exc:
                if "HTTP 404" in str(exc):
                    break
                raise
            except SiteParseError:
                break

            merged = list(dedupe_images([*images, *page_article.images]))
            if len(merged) == len(images):
                break
            images = merged

        return Article(url=url, title=first_article.title, images=dedupe_images(images))


class SexuadArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"c-postTitle__ttl"}),)
    image_hosts = frozenset({"sexuad.jp", "www.sexuad.jp"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._ignored_depth = 0
        self._thumb_depth = 0
        self._image_figure_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "button" and _has_classes(classes, "simplefavorite-button", "has-count"):
            self._stop_images = True
            self._ignored_depth = 0
            self._thumb_depth = 0
            self._image_figure_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._ignored_depth:
                self._ignored_depth += 1
            elif "w-singleTop" in classes:
                self._ignored_depth = 1
                self._current_anchor_href = None
            return

        if self._ignored_depth:
            return

        if tag == "figure":
            if self._thumb_depth:
                self._thumb_depth += 1
            elif "p-articleThumb" in classes:
                self._thumb_depth = 1
            elif self._image_figure_depth:
                self._image_figure_depth += 1
            elif _has_classes(classes, "wp-block-image", "size-large"):
                self._image_figure_depth = 1
            return

        if self._thumb_depth and tag == "img":
            image_url = (
                self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))
            return

        if not self._image_figure_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "a" and self._image_figure_depth:
            self._current_anchor_href = None
        elif tag == "figure":
            if self._thumb_depth:
                self._thumb_depth -= 1
            elif self._image_figure_depth:
                self._image_figure_depth -= 1
                if self._image_figure_depth == 0:
                    self._current_anchor_href = None


class SexuadAdapter(ParserBackedAdapter):
    hosts = SexuadArticleParser.image_hosts
    parser_cls = SexuadArticleParser
    image_context = "figure.p-articleThumb and figure.wp-block-image.size-large before button.simplefavorite-button.has-count"

    def __init__(self) -> None:
        super().__init__(name="sexuad")


class FemmedollArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"c-postTitle__ttl"}),)
    image_hosts = frozenset({"femmedoll.jp", "www.femmedoll.jp"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._thumb_depth = 0
        self._gallery_depth = 0
        self._gallery_done = False
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "figure":
            if self._thumb_depth:
                self._thumb_depth += 1
            elif "p-articleThumb" in classes:
                self._thumb_depth = 1
            return

        if self._thumb_depth and tag == "img":
            image_url = (
                self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))
            return

        if tag == "section" and not self._gallery_done:
            if self._gallery_depth:
                self._gallery_depth += 1
            elif _has_classes(classes, "wp-block-gallery", "has-nested-images"):
                self._gallery_depth = 1
            return

        if not self._gallery_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._best_supported_srcset_url(attrs.get("data-srcset", ""))
                or self._best_supported_srcset_url(attrs.get("srcset", ""))
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._gallery_depth:
            self._current_anchor_href = None
        elif tag == "figure" and self._thumb_depth:
            self._thumb_depth -= 1
        elif tag == "section" and self._gallery_depth:
            self._gallery_depth -= 1
            if self._gallery_depth == 0:
                self._gallery_done = True
                self._current_anchor_href = None

    def _best_supported_srcset_url(self, value: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for weight, candidate in _srcset_candidates(value):
            image_url = self._supported_image_url(candidate, self.image_hosts)
            if image_url:
                matches.append((weight, image_url))
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]


class FemmedollAdapter(ParserBackedAdapter):
    hosts = FemmedollArticleParser.image_hosts
    parser_cls = FemmedollArticleParser
    image_context = "figure.p-articleThumb and section.wp-block-gallery.has-nested-images"

    def __init__(self) -> None:
        super().__init__(name="femmedoll")


class HentaiWitchArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"hentai-witch.com", "www.hentai-witch.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._image_figure_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "footer" and _has_classes(classes, "article-footer", "entry-footer"):
            self._stop_images = True
            self._image_figure_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "figure":
            if self._image_figure_depth:
                self._image_figure_depth += 1
            elif "wp-block-image" in classes and ("size-large" in classes or "size-full" in classes):
                self._image_figure_depth = 1
            return

        if not self._image_figure_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._best_supported_srcset_url(attrs.get("data-srcset", ""))
                or self._best_supported_srcset_url(attrs.get("srcset", ""))
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._image_figure_depth:
            self._current_anchor_href = None
        elif tag == "figure" and self._image_figure_depth:
            self._image_figure_depth -= 1
            if self._image_figure_depth == 0:
                self._current_anchor_href = None

    def _best_supported_srcset_url(self, value: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for weight, candidate in _srcset_candidates(value):
            image_url = self._supported_image_url(candidate, self.image_hosts)
            if image_url:
                matches.append((weight, image_url))
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]


class HentaiWitchAdapter(ParserBackedAdapter):
    hosts = HentaiWitchArticleParser.image_hosts
    parser_cls = HentaiWitchArticleParser
    image_context = "figure.wp-block-image.size-large/full before footer.article-footer.entry-footer"

    def __init__(self) -> None:
        super().__init__(name="hentai-witch")


class AdamanEroArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    article_hosts = frozenset({"adaman-ero.com", "www.adaman-ero.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._main_depth = 0
        self._stop_images = False
        self._ignored_depth = 0
        self._hero_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if self._stop_images:
            return

        if tag == "div":
            if self._main_depth:
                if attrs.get("id") == "jp-relatedposts":
                    self._stop_images = True
                    self._main_depth = 0
                    self._ignored_depth = 0
                    self._hero_depth = 0
                    self._current_anchor_href = None
                    return

                if self._ignored_depth:
                    self._ignored_depth += 1
                elif "p-viewer-wrap" in classes:
                    self._ignored_depth = 1
                    self._current_anchor_href = None

                if self._hero_depth:
                    self._hero_depth += 1
                elif "st-eyecatch-under" in classes:
                    self._hero_depth = 1

                self._main_depth += 1
            elif "mainbox" in classes:
                self._main_depth = 1
            return

        if not self._main_depth or self._ignored_depth:
            return

        if self._hero_depth and tag == "img":
            image_url = (
                self._supported_adaman_image_url(attrs.get("data-src", ""))
                or self._best_supported_srcset_url(attrs.get("data-srcset", ""))
                or self._best_supported_srcset_url(attrs.get("srcset", ""))
                or self._supported_adaman_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))
            return

        if tag == "a":
            self._current_anchor_href = self._supported_adaman_image_url(attrs.get("href", ""))
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._best_supported_srcset_url(attrs.get("data-srcset", ""))
                or self._best_supported_srcset_url(attrs.get("srcset", ""))
                or self._supported_adaman_image_url(attrs.get("data-src", ""))
                or self._supported_adaman_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._main_depth:
            self._current_anchor_href = None
        elif tag == "div":
            if self._ignored_depth:
                self._ignored_depth -= 1
            if self._hero_depth:
                self._hero_depth -= 1
            if self._main_depth:
                self._main_depth -= 1
                if self._main_depth == 0:
                    self._current_anchor_href = None

    def _best_supported_srcset_url(self, value: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for weight, candidate in _srcset_candidates(value):
            image_url = self._supported_adaman_image_url(candidate)
            if image_url:
                matches.append((weight, image_url))
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]

    def _supported_adaman_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        host = parsed.netloc.lower()
        if host in self.article_hosts:
            if not _is_image_url(absolute):
                return None
            return absolute
        if host in JETPACK_IMAGE_HOSTS and _is_image_url(absolute):
            origin_url = jetpack_origin_url(absolute)
            if origin_url and urlparse(origin_url).netloc.lower() in self.article_hosts:
                return origin_url
        return None


class AdamanEroAdapter(ParserBackedAdapter):
    hosts = AdamanEroArticleParser.article_hosts
    parser_cls = AdamanEroArticleParser
    image_context = "div.mainbox before div#jp-relatedposts, excluding div.p-viewer-wrap"

    def __init__(self) -> None:
        super().__init__(name="adaman-ero")


class EchiechiGazouArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"echiechigazou.com", "www.echiechigazou.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._stop_images = False
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if self._stop_images:
            return

        if self._content_depth and tag == "hr" and _has_classes(
            classes,
            "wp-block-separator",
            "has-alpha-channel-opacity",
        ):
            self._stop_images = True
            self._content_depth = 0
            self._current_anchor_href = None
            return

        if tag == "div":
            if self._content_depth:
                self._content_depth += 1
            elif _has_classes(classes, "entry-content", "cf", "iwe-shadow-paper"):
                self._content_depth = 1
            return

        if not self._content_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._best_supported_srcset_url(attrs.get("data-srcset", ""))
                or self._best_supported_srcset_url(attrs.get("srcset", ""))
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._content_depth:
            self._current_anchor_href = None
        elif tag == "div" and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._current_anchor_href = None

    def _best_supported_srcset_url(self, value: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for weight, candidate in _srcset_candidates(value):
            image_url = self._supported_image_url(candidate, self.image_hosts)
            if image_url:
                matches.append((weight, image_url))
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]


class EchiechiGazouAdapter(ParserBackedAdapter):
    hosts = EchiechiGazouArticleParser.image_hosts
    parser_cls = EchiechiGazouArticleParser
    image_context = "div.entry-content.cf.iwe-shadow-paper before hr.wp-block-separator.has-alpha-channel-opacity"

    def __init__(self) -> None:
        super().__init__(name="echiechigazou")


class NijiPinkArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    article_hosts = frozenset({"2ji.pink", "www.2ji.pink"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "button" and _has_classes(classes, "simplefavorite-button", "has-count") and self.images:
            self._stop_images = True
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_2ji_image_url(attrs.get("href", ""))
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_2ji_image_url(attrs.get("data-src", ""))
                or self._supported_2ji_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current_anchor_href = None

    def _supported_2ji_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != "img.2ji.pink":
            return None
        if not _is_image_url(absolute):
            return None
        return absolute


class NijiPinkAdapter(ParserBackedAdapter):
    hosts = NijiPinkArticleParser.article_hosts
    parser_cls = NijiPinkArticleParser
    image_context = "img.2ji.pink image links before button.simplefavorite-button.has-count"

    def __init__(self) -> None:
        super().__init__(name="2ji-pink")


class EromanidcArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"c-postTitle__ttl"}),)
    image_hosts = frozenset({"eromanidc.com", "www.eromanidc.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._stop_images = False
        self._ignored_depth = 0
        self._image_figure_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "w-singleBottom" in classes and self._content_depth:
            self._stop_images = True
            self._content_depth = 0
            self._ignored_depth = 0
            self._image_figure_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._ignored_depth:
                self._ignored_depth += 1
                return
            if self._content_depth and ("ams-feed-wrap" in classes or "w-singleTop" in classes):
                self._ignored_depth = 1
                self._current_anchor_href = None
                return
            if self._content_depth:
                self._content_depth += 1
            elif "post_content" in classes:
                self._content_depth = 1
            return

        if self._ignored_depth or not self._content_depth:
            return

        if tag == "figure":
            if self._image_figure_depth:
                self._image_figure_depth += 1
            elif _has_classes(classes, "wp-block-image", "size-full"):
                self._image_figure_depth = 1
            return

        if not self._image_figure_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "div" and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._current_anchor_href = None
        elif tag == "a" and self._image_figure_depth:
            self._current_anchor_href = None
        elif tag == "figure" and self._image_figure_depth:
            self._image_figure_depth -= 1
            if self._image_figure_depth == 0:
                self._current_anchor_href = None


class EromanidcAdapter(ParserBackedAdapter):
    hosts = EromanidcArticleParser.image_hosts
    parser_cls = EromanidcArticleParser
    image_context = "div.post_content figure.wp-block-image.size-full before div.w-singleBottom"

    def __init__(self) -> None:
        super().__init__(name="eromanidc")


class OppaisanArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"heading"}),)
    article_hosts = frozenset({"oppaisan.com", "www.oppaisan.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._image_box_depth = 0

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "section" and attrs.get("id") == "related_post":
            self._stop_images = True
            self._image_box_depth = 0
            return

        if self._stop_images:
            return

        if tag in {"div", "li"}:
            if self._image_box_depth:
                self._image_box_depth += 1
            elif "img_fav_add_waku" in classes or "img_list" in classes:
                self._image_box_depth = 1
            return

        if tag != "img":
            return

        image_url = (
            self._supported_oppaisan_image_url(attrs.get("data-src", ""))
            or self._supported_oppaisan_image_url(attrs.get("src", ""))
        )
        if not image_url:
            return

        if self._image_box_depth or self._is_hero_image(image_url, attrs):
            self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag in {"div", "li"} and self._image_box_depth:
            self._image_box_depth -= 1

    def _supported_oppaisan_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        host = parsed.netloc.lower()
        if host not in {"img.oppaisan.com", "pics.dmm.co.jp"}:
            return None
        if not _is_image_url(absolute):
            return None
        return absolute

    def _is_hero_image(self, image_url: str, attrs: dict[str, str]) -> bool:
        parsed = urlparse(image_url)
        return parsed.netloc.lower() == "img.oppaisan.com" and parsed.path.startswith("/img/entry_images/")


class OppaisanAdapter(ParserBackedAdapter):
    hosts = OppaisanArticleParser.article_hosts
    parser_cls = OppaisanArticleParser
    image_context = "hero image and div.img_fav_add_waku images before section#related_post"

    def __init__(self) -> None:
        super().__init__(name="oppaisan")


def _canonical_url_without_query(url: str) -> str:
    split = urlsplit(url)
    path = normpath(split.path)
    if split.path.endswith("/") and not path.endswith("/"):
        path = f"{path}/"
    if not path.startswith("/"):
        path = f"/{path}"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


class LesKokoArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"single-post-title", "entry-title"}),)
    image_hosts = frozenset({"les-koko.com", "www.les-koko.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._article_img_depth = 0
        self._ignored_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and "pager" in classes:
            self._stop_images = True
            self._article_img_depth = 0
            self._ignored_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._ignored_depth:
                self._ignored_depth += 1
            elif "item_area" in classes or "rect-ads-row" in classes:
                self._ignored_depth = 1
                return

            if self._article_img_depth:
                self._article_img_depth += 1
            elif "article_img" in classes:
                self._article_img_depth = 1
            return

        if self._ignored_depth or not self._article_img_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_les_image_url(attrs.get("href", ""))
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_les_image_url(attrs.get("data-src", ""))
                or self._supported_les_image_url(attrs.get("src", ""))
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._article_img_depth:
            self._current_anchor_href = None
        elif tag == "div":
            if self._ignored_depth:
                self._ignored_depth -= 1
            elif self._article_img_depth:
                self._article_img_depth -= 1
                if self._article_img_depth == 0:
                    self._current_anchor_href = None

    def _supported_les_image_url(self, value: str) -> str | None:
        image_url = self._supported_image_url(value, self.image_hosts)
        if not image_url:
            return None
        return _canonical_url_without_query(image_url)


def _les_koko_page_url(url: str, page_number: int) -> str:
    split = urlsplit(url)
    query_parts = [part for part in split.query.split("&") if part and not part.startswith("page=")]
    query_parts.append(f"page={page_number}")
    return urlunsplit((split.scheme, split.netloc, split.path, "&".join(query_parts), split.fragment))


def _les_koko_initial_urls(url: str) -> tuple[str, ...]:
    split = urlsplit(url)
    if split.path.endswith("/"):
        return (url,)

    with_slash = urlunsplit((split.scheme, split.netloc, f"{split.path}/", split.query, split.fragment))
    return (url, with_slash)


class LesKokoAdapter(ParserBackedAdapter):
    hosts = LesKokoArticleParser.image_hosts
    parser_cls = LesKokoArticleParser
    image_context = "div.article_img blocks before div.pager"
    max_pages = 200

    def __init__(self) -> None:
        super().__init__(name="les-koko")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        http = client or HttpClient()
        first_article: Article | None = None
        first_url = url
        last_redirect: RedirectBlocked | None = None
        for candidate_url in _les_koko_initial_urls(url):
            try:
                first_response = http.get(candidate_url)
                first_article = self.parse(candidate_url, first_response.text)
                first_url = candidate_url
                break
            except RedirectBlocked as exc:
                last_redirect = exc

        if first_article is None:
            if last_redirect is not None:
                raise last_redirect
            raise SiteParseError("No downloadable les-koko article images were found in the first article page.")

        images = list(first_article.images)

        for page_number in range(2, self.max_pages + 1):
            page_url = _les_koko_page_url(first_url, page_number)
            try:
                response = http.get(page_url, referer=first_url)
                page_article = self.parse(page_url, response.text)
            except RedirectBlocked:
                break
            except DownloadError as exc:
                if "HTTP 404" in str(exc):
                    break
                raise
            except SiteParseError:
                break

            merged = list(dedupe_images([*images, *page_article.images]))
            if len(merged) == len(images):
                break
            images = merged

        return Article(url=url, title=first_article.title, images=dedupe_images(images))


class YaruoArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title", "single-title"}),)
    image_hosts = frozenset({"yaruo.info", "www.yaruo.info"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._main_depth = 0
        self._content_depth = 0
        self._content_done = False
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "main":
            if self._main_depth:
                self._main_depth += 1
            elif attrs.get("id") == "main":
                self._main_depth = 1
            return

        if not self._main_depth or self._content_done:
            return

        if tag in {"div", "section"}:
            if self._content_depth:
                self._content_depth += 1
            elif _has_classes(classes, "entry-content", "cf"):
                self._content_depth = 1
            return

        if not self._content_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = self._current_anchor_href or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
            if not image_url:
                image_url = self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._content_depth:
            self._current_anchor_href = None
        elif tag in {"div", "section"} and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_done = True
                self._current_anchor_href = None
        elif tag == "main" and self._main_depth:
            self._main_depth -= 1


class YaruoAdapter(ParserBackedAdapter):
    hosts = YaruoArticleParser.image_hosts
    parser_cls = YaruoArticleParser
    image_context = "main#main .entry-content.cf"

    def __init__(self) -> None:
        super().__init__(name="yaruo")


class ErogazouGalleryArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"title"}),)
    article_hosts = frozenset({"erogazou.gallery", "www.erogazou.gallery"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._stop_images = False
        self._ignored_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and attrs.get("id") == "single-page-links":
            self._stop_images = True
            self._content_depth = 0
            self._ignored_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if self._content_depth and tag == "div" and "contents-afi" in classes:
            self._ignored_depth = 1
            self._current_anchor_href = None
            return

        if tag == "iframe" and attrs.get("id") == "onlineBanner":
            self._ignored_depth = max(self._ignored_depth, 1)
            self._current_anchor_href = None
            return

        if tag == "div":
            if self._ignored_depth:
                self._ignored_depth += 1
            elif self._content_depth:
                self._content_depth += 1
            elif "content" in classes:
                self._content_depth = 1
            return

        if self._ignored_depth or not self._content_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_gallery_image_url(attrs.get("href", ""))
        elif tag == "img":
            image_url = self._current_anchor_href or self._supported_gallery_image_url(attrs.get("src", ""))
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._content_depth:
            self._current_anchor_href = None
        elif tag == "iframe" and self._ignored_depth:
            self._ignored_depth = 0
        elif tag == "div":
            if self._ignored_depth:
                self._ignored_depth -= 1
            elif self._content_depth:
                self._content_depth -= 1
                if self._content_depth == 0:
                    self._current_anchor_href = None

    def _supported_gallery_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        host = parsed.netloc.lower()
        if host not in self.article_hosts and not re.match(r"^img\d*\.erogazou\.gallery$", host):
            return None
        if not _is_image_url(absolute):
            return None
        return absolute


def _erogazou_gallery_page_url(url: str, page_number: int) -> str:
    split = urlsplit(url)
    path = f"{split.path.rstrip('/')}/{page_number}"
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


def _erogazou_gallery_initial_urls(url: str) -> tuple[str, ...]:
    split = urlsplit(url)
    if split.path.endswith("/"):
        return (url,)

    with_slash = urlunsplit((split.scheme, split.netloc, f"{split.path}/", split.query, split.fragment))
    return (url, with_slash)


class ErogazouGalleryAdapter(ParserBackedAdapter):
    hosts = ErogazouGalleryArticleParser.article_hosts
    parser_cls = ErogazouGalleryArticleParser
    image_context = "div.content before #single-page-links"
    max_pages = 200

    def __init__(self) -> None:
        super().__init__(name="erogazou-gallery")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        http = client or HttpClient()
        first_article: Article | None = None
        first_url = url
        last_redirect: RedirectBlocked | None = None
        for candidate_url in _erogazou_gallery_initial_urls(url):
            try:
                first_response = http.get(candidate_url)
                first_article = self.parse(candidate_url, first_response.text)
                first_url = candidate_url
                break
            except RedirectBlocked as exc:
                last_redirect = exc

        if first_article is None:
            if last_redirect is not None:
                raise last_redirect
            raise SiteParseError("No downloadable erogazou-gallery article images were found in the first article page.")

        images = list(first_article.images)

        for page_number in range(2, self.max_pages + 1):
            page_url = _erogazou_gallery_page_url(first_url, page_number)
            try:
                response = http.get(page_url, referer=first_url)
                page_article = self.parse(page_url, response.text)
            except RedirectBlocked:
                break
            except DownloadError as exc:
                if "HTTP 404" in str(exc):
                    break
                raise
            except SiteParseError:
                break

            merged = list(dedupe_images([*images, *page_article.images]))
            if len(merged) == len(images):
                break
            images = merged

        return Article(url=url, title=first_article.title, images=dedupe_images(images))


class DebusenArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({DEBUSEN_PUNYCODE_HOST, DEBUSEN_UNICODE_HOST})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._eyecatch_depth = 0
        self._content_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "footer" and _has_classes(classes, "article-footer", "entry-footer"):
            self._stop_images = True
            self._eyecatch_depth = 0
            self._content_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._eyecatch_depth:
                self._eyecatch_depth += 1
            elif "eye-catch-wrap" in classes:
                self._eyecatch_depth = 1

            if self._content_depth:
                self._content_depth += 1
            elif _has_classes(classes, "entry-content", "cf"):
                self._content_depth = 1
            return

        if self._eyecatch_depth and tag == "img":
            image_url = self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))
            return

        if not self._content_depth:
            return

        if tag == "a":
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img":
            image_url = (
                self._current_anchor_href
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._content_depth:
            self._current_anchor_href = None
        elif tag == "div":
            if self._eyecatch_depth:
                self._eyecatch_depth -= 1
            if self._content_depth:
                self._content_depth -= 1


class DebusenAdapter(ParserBackedAdapter):
    hosts = frozenset({DEBUSEN_PUNYCODE_HOST, DEBUSEN_UNICODE_HOST})
    parser_cls = DebusenArticleParser
    image_context = "eye-catch and div.entry-content.cf before footer.article-footer.entry-footer"

    def __init__(self) -> None:
        super().__init__(name="debusen")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        fetch_url = _iri_to_uri(url)
        http = client or HttpClient()
        response = http.get(fetch_url)
        article = self.parse(fetch_url, response.text)
        return Article(url=url, title=article.title, images=article.images)


class ErokanArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"erokan.net", "www.erokan.net", "img.erokan.net"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._content_depth = 0
        self._content_done = False
        self._ignored_depth = 0

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if self._content_depth and tag == "div" and _has_classes(classes, "wpulike", "wpulike-heart"):
            if self.images:
                self._content_depth = 0
                self._content_done = True
            else:
                self._ignored_depth = 1
            return

        if tag in {"div", "section"} and not self._content_done:
            if self._ignored_depth:
                self._ignored_depth += 1
            elif self._content_depth:
                self._content_depth += 1
            elif _has_classes(classes, "entry-content", "cf"):
                self._content_depth = 1
            return

        if self._ignored_depth or not self._content_depth:
            return

        if tag == "img":
            image_url = (
                self._supported_image_url(attrs.get("src", ""), self.image_hosts)
                or self._supported_image_url(attrs.get("data-src", ""), self.image_hosts)
            )
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag in {"div", "section"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"div", "section"} and self._content_depth:
            self._content_depth -= 1
            if self._content_depth == 0:
                self._content_done = True


class ErokanAdapter(ParserBackedAdapter):
    hosts = frozenset({"erokan.net", "www.erokan.net"})
    parser_cls = ErokanArticleParser
    image_context = "div.entry-content.cf before div.wpulike.wpulike-heart"

    def __init__(self) -> None:
        super().__init__(name="erokan")


class HnaladyArticleParser(BaseArticleParser):
    article_hosts = frozenset({"hnalady.com", "www.hnalady.com"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._in_anchor_title = False
        self._in_h2_title = False
        self._title_done = False
        self._content_depth = 0

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "h3" and "entry-bottom" in classes:
            self._stop_images = True
            self._content_depth = 0
            return

        if tag == "div":
            if self._content_depth:
                self._content_depth += 1
            elif "content" in classes and attrs.get("id", "").startswith("e"):
                self._content_depth = 1
            return

        if tag == "h2" and self._content_depth and not self.title_parts:
            self._in_h2_title = True
            return

        if tag == "a" and not self._title_done and self._is_article_link(attrs.get("href", "")):
            self._in_anchor_title = True
            return

        if self._stop_images or not (self._title_done or self._content_depth):
            return

        if tag == "img":
            image_url = self._supported_hnalady_image_url(attrs.get("src", ""))
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_anchor_title:
            self._in_anchor_title = False
            self._title_done = bool(self.title)
        elif tag == "h2" and self._in_h2_title:
            self._in_h2_title = False
            self._title_done = bool(self.title)
        elif tag == "div" and self._content_depth:
            self._content_depth -= 1

    def _handle_data(self, data: str) -> None:
        if self._in_anchor_title or self._in_h2_title:
            self.title_parts.append(data)

    def _is_article_link(self, value: str) -> bool:
        if not value:
            return False

        linked = urlparse(urljoin(self.base_url, value))
        base = urlparse(self.base_url)
        return linked.netloc.lower() in self.article_hosts and linked.path == base.path

    def _supported_hnalady_image_url(self, value: str) -> str | None:
        if not value:
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        if not re.match(r"^blog-imgs-\d+\.fc2\.com$", parsed.netloc.lower()):
            return None
        if not parsed.path.startswith("/h/n/a/hnalady/"):
            return None
        if not _is_image_url(absolute):
            return None
        return absolute


class HnaladyAdapter(ParserBackedAdapter):
    hosts = HnaladyArticleParser.article_hosts
    parser_cls = HnaladyArticleParser
    image_context = "FC2 article images before h3.entry-bottom"

    def __init__(self) -> None:
        super().__init__(name="hnalady")

    def supports(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "http" and parsed.netloc.lower() in self.hosts


class EroGazouArticleParser(BaseArticleParser):
    title_class_sets = (frozenset({"entry-title"}),)
    image_hosts = frozenset({"ero-gazou.jp", "www.ero-gazou.jp"})

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self._stop_images = False
        self._eyecatch_depth = 0
        self._content_depth = 0
        self._current_anchor_href: str | None = None

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if tag == "div" and _has_classes(classes, "pager-links", "pager-prev-next"):
            self._stop_images = True
            self._eyecatch_depth = 0
            self._content_depth = 0
            self._current_anchor_href = None
            return

        if self._stop_images:
            return

        if tag == "div":
            if self._eyecatch_depth:
                self._eyecatch_depth += 1
            elif "eye-catch-wrap" in classes:
                self._eyecatch_depth = 1

            if self._content_depth:
                self._content_depth += 1
            elif "entry-content" in classes:
                self._content_depth = 1
            return

        if self._eyecatch_depth and tag == "img":
            image_url = self._supported_image_url(attrs.get("src", ""), self.image_hosts)
            if image_url:
                self._add_image(image_url, attrs.get("alt", ""))
            return

        if self._content_depth:
            if tag == "a":
                self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
            elif tag == "img":
                image_url = self._current_anchor_href or self._supported_image_url(attrs.get("src", ""), self.image_hosts)
                if image_url:
                    self._add_image(image_url, attrs.get("alt", ""))
            return

        if tag == "a" and _has_classes(classes, "fancybox", "image"):
            self._current_anchor_href = self._supported_image_url(attrs.get("href", ""), self.image_hosts)
        elif tag == "img" and self._current_anchor_href:
            self._add_image(self._current_anchor_href, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current_anchor_href = None
        elif tag == "div":
            if self._eyecatch_depth:
                self._eyecatch_depth -= 1
            if self._content_depth:
                self._content_depth -= 1


def _ero_gazou_page_urls(url: str, page_number: int) -> tuple[str, str]:
    split = urlsplit(url)
    base_path = split.path.rstrip("/")
    without_slash = urlunsplit((split.scheme, split.netloc, f"{base_path}/{page_number}", "", ""))
    with_slash = urlunsplit((split.scheme, split.netloc, f"{base_path}/{page_number}/", "", ""))
    return without_slash, with_slash


def _ero_gazou_initial_urls(url: str) -> tuple[str, ...]:
    split = urlsplit(url)
    if split.path.endswith("/"):
        return (url,)

    with_slash = urlunsplit((split.scheme, split.netloc, f"{split.path}/", split.query, split.fragment))
    return (url, with_slash)


class EroGazouAdapter(ParserBackedAdapter):
    hosts = EroGazouArticleParser.image_hosts
    parser_cls = EroGazouArticleParser
    image_context = "the article before pager links"
    max_pages = 200

    def __init__(self) -> None:
        super().__init__(name="ero-gazou")

    def scan(self, url: str, client: HttpClient | None = None) -> Article:
        http = client or HttpClient()
        first_article: Article | None = None
        first_url = url
        last_redirect: RedirectBlocked | None = None
        for candidate_url in _ero_gazou_initial_urls(url):
            try:
                first_response = http.get(candidate_url)
                first_article = self.parse(candidate_url, first_response.text)
                first_url = candidate_url
                break
            except RedirectBlocked as exc:
                last_redirect = exc

        if first_article is None:
            if last_redirect is not None:
                raise last_redirect
            raise SiteParseError("No downloadable ero-gazou article images were found in the first article page.")

        images = list(first_article.images)

        for page_number in range(2, self.max_pages + 1):
            page_article: Article | None = None
            for page_url in _ero_gazou_page_urls(first_url, page_number):
                try:
                    response = http.get(page_url, referer=first_url)
                    page_article = self.parse(page_url, response.text)
                    break
                except RedirectBlocked:
                    continue
                except DownloadError as exc:
                    if "HTTP 404" in str(exc):
                        continue
                    raise
                except SiteParseError:
                    continue

            if page_article is None:
                break

            merged = list(dedupe_images([*images, *page_article.images]))
            if len(merged) == len(images):
                break
            images = merged

        return Article(url=url, title=first_article.title, images=dedupe_images(images))


class GenericMatomeArticleParser(BaseArticleParser):
    title_class_sets = (frozenset(),)
    _container_tags = {"article", "aside", "div", "figure", "footer", "main", "nav", "section", "ul", "ol", "li"}
    _content_tokens = {
        "article-body",
        "article-content",
        "article__body",
        "bialty-container",
        "box",
        "content-inner",
        "entry-content",
        "honbun",
        "iwe-shadow-paper",
        "mainbox",
        "post-body",
        "post-content",
        "post_content",
        "single-content",
        "singlebox",
        "the-content",
    }
    _hero_tokens = {
        "articlethumb",
        "eye-catch",
        "eye-catch-wrap",
        "eyecatch",
        "p-articlethumb",
        "post-thumbnail",
        "st-eyecatch-under",
    }
    _ignored_tokens = {
        "ad",
        "ad-area",
        "adbox",
        "ads",
        "adsbygoogle",
        "advertisement",
        "ams-feed-wrap",
        "article-footer",
        "banner",
        "blogroll",
        "breadcrumb",
        "comment",
        "comments",
        "entry-footer",
        "favorite",
        "footer",
        "header",
        "kanren",
        "menu",
        "nav",
        "pager",
        "pagination",
        "pickup",
        "popular",
        "profile",
        "p-viewer-wrap",
        "ranking",
        "recommend",
        "related",
        "share",
        "sidebar",
        "singlebottom",
        "singletop",
        "sns",
        "widget",
        "wpulike",
    }
    _ad_hosts = {
        "ad.doubleclick.net",
        "ads.google.com",
        "adservice.google.com",
        "ams.exad.jp",
        "googleads.g.doubleclick.net",
        "googlesyndication.com",
        "pagead2.googlesyndication.com",
    }
    _blocked_image_host_suffixes = (
        "dmm.co.jp",
        "dmm.com",
        "fanza.jp",
    )

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        parsed = urlparse(base_url)
        self._base_host = parsed.netloc.lower()
        self._base_root = _hostname_root(self._base_host)
        self._content_depth = 0
        self._hero_depth = 0
        self._ignored_depth = 0
        self._content_done = False
        self._current_anchor_href: str | None = None
        self._hero_images: list[tuple[str, str]] = []
        self._content_images: list[tuple[str, str]] = []
        self._fallback_images: list[tuple[str, str]] = []

    def _is_title(self, classes: set[str]) -> bool:
        return not self.title_parts

    def _handle_starttag(self, tag: str, attrs: dict[str, str], classes: set[str]) -> None:
        if self._content_depth and self._is_separator_terminator(tag, classes):
            self._content_depth = 0
            self._hero_depth = 0
            self._content_done = True
            self._current_anchor_href = None
            return

        if tag in self._container_tags:
            if self._ignored_depth:
                self._ignored_depth += 1
                return

            if self._is_ignored_container(tag, attrs, classes):
                self._ignored_depth = 1
                self._current_anchor_href = None
                if self._content_depth and self._content_images:
                    self._content_done = True
                    self._content_depth = 0
                    self._hero_depth = 0
                return

            if self._content_depth:
                self._content_depth += 1
            elif not self._content_done and self._is_content_container(tag, attrs, classes):
                self._content_depth = 1

            if self._hero_depth:
                self._hero_depth += 1
            elif self._is_hero_container(classes):
                self._hero_depth = 1
            return

        if self._ignored_depth:
            return

        broad = bool(self._content_depth or self._hero_depth)
        if tag == "a":
            self._current_anchor_href = self._supported_generic_image_url(attrs.get("href", ""), broad=broad)
        elif tag == "img":
            image_url = self._image_url_from_attrs(attrs, broad=broad)
            if image_url:
                self._add_candidate(image_url, attrs.get("alt", ""))

    def _handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current_anchor_href = None
        elif tag in self._container_tags:
            if self._ignored_depth:
                self._ignored_depth -= 1
                return
            if self._hero_depth:
                self._hero_depth -= 1
            if self._content_depth:
                self._content_depth -= 1
                if self._content_depth == 0:
                    self._current_anchor_href = None

    def _image_url_from_attrs(self, attrs: dict[str, str], *, broad: bool) -> str | None:
        return (
            self._current_anchor_href
            or self._supported_generic_image_url(attrs.get("data-full-url", ""), broad=broad)
            or self._supported_generic_image_url(attrs.get("data-luminous", ""), broad=broad)
            or self._best_supported_srcset_url(attrs.get("data-srcset", ""), broad=broad)
            or self._best_supported_srcset_url(attrs.get("srcset", ""), broad=broad)
            or self._supported_generic_image_url(attrs.get("data-lazy-src", ""), broad=broad)
            or self._supported_generic_image_url(attrs.get("data-src", ""), broad=broad)
            or self._supported_generic_image_url(attrs.get("data-original", ""), broad=broad)
            or self._supported_generic_image_url(attrs.get("src", ""), broad=broad)
        )

    def _add_candidate(self, url: str, label: str) -> None:
        if self._hero_depth:
            self._hero_images.append((url, label))
        elif self._content_depth:
            self._content_images.append((url, label))
        else:
            self._fallback_images.append((url, label))

    def article_images(self) -> tuple[ImageItem, ...]:
        if self._content_images:
            selected = [*self._hero_images, *self._content_images]
        elif self._hero_images:
            selected = self._hero_images
        else:
            selected = self._fallback_images
        return tuple(ImageItem(index, url, label) for index, (url, label) in enumerate(selected, start=1))

    def _is_content_container(self, tag: str, attrs: dict[str, str], classes: set[str]) -> bool:
        if tag == "article":
            return True

        tokens = self._tokens(attrs, classes)
        if tokens & self._content_tokens:
            return True

        combined = " ".join(tokens).replace("_", "-").lower()
        return any(
            pattern in combined
            for pattern in (
                "article-body",
                "article-content",
                "entry-content",
                "post-body",
                "post-content",
                "single-content",
            )
        )

    def _is_hero_container(self, classes: set[str]) -> bool:
        tokens = {token.replace("_", "-").lower() for token in classes}
        if tokens & self._hero_tokens:
            return True
        return any("eyecatch" in token or "eye-catch" in token or "articlethumb" in token for token in tokens)

    def _is_ignored_container(self, tag: str, attrs: dict[str, str], classes: set[str]) -> bool:
        if tag in {"footer", "nav", "aside"} and (self._content_images or self._content_depth):
            return True

        tokens = self._tokens(attrs, classes)
        if attrs.get("id") == "jp-relatedposts":
            return True
        if tokens & self._ignored_tokens:
            return True
        return any(
            token.startswith("ad-")
            or token.endswith("-ad")
            or "related" in token
            or "recommend" in token
            or "ranking" in token
            for token in tokens
        )

    def _is_separator_terminator(self, tag: str, classes: set[str]) -> bool:
        return (
            tag == "hr"
            and self._content_images
            and _has_classes(classes, "wp-block-separator", "has-alpha-channel-opacity")
        )

    def _tokens(self, attrs: dict[str, str], classes: set[str]) -> set[str]:
        tokens = {token.replace("_", "-").lower() for token in classes}
        element_id = attrs.get("id", "").replace("_", "-").lower()
        if element_id:
            tokens.add(element_id)
        return tokens

    def _best_supported_srcset_url(self, value: str, *, broad: bool) -> str | None:
        matches: list[tuple[int, str]] = []
        for weight, candidate in _srcset_candidates(value):
            image_url = self._supported_generic_image_url(candidate, broad=broad)
            if image_url:
                matches.append((weight, image_url))
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]

    def _supported_generic_image_url(self, value: str, *, broad: bool) -> str | None:
        if not value or value.startswith(("data:", "blob:", "javascript:")):
            return None

        absolute = urljoin(self.base_url, value)
        parsed = urlparse(absolute)
        host = parsed.netloc.lower()
        if parsed.scheme not in {"http", "https"} or not _is_image_url(absolute):
            return None
        if self._is_blocked_image_host(host):
            return None
        if host in JETPACK_IMAGE_HOSTS:
            origin_url = jetpack_origin_url(absolute)
            if not origin_url:
                return None
            proxied_host = urlparse(origin_url).netloc.lower()
            if self._is_blocked_image_host(proxied_host):
                return None
            return origin_url if proxied_host == self._base_host else None
        if broad:
            return absolute
        if host == self._base_host or host.endswith(f".{self._base_host}"):
            return absolute
        if _hostname_root(host) == self._base_root:
            return absolute
        return None

    def _is_blocked_image_host(self, host: str) -> bool:
        if host in self._ad_hosts or any(host.endswith(f".{ad_host}") for ad_host in self._ad_hosts):
            return True
        return any(host == suffix or host.endswith(f".{suffix}") for suffix in self._blocked_image_host_suffixes)


class GenericMatomeAdapter(SiteAdapter):
    def __init__(self) -> None:
        super().__init__(name="generic-matome")

    def supports(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def parse(self, url: str, html: str) -> Article:
        parser = GenericMatomeArticleParser(url)
        parser.feed(html)
        parser.close()

        title = parser.title or title_fallback_from_url(url)
        images = dedupe_images(parser.article_images())
        if not images:
            raise SiteParseError("No downloadable generic matome article images were found.")
        return Article(url=url, title=title, images=images)
