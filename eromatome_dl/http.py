from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
import mimetypes
import os


JETPACK_IMAGE_HOSTS = frozenset({"i0.wp.com", "i1.wp.com", "i2.wp.com"})


class DownloadError(RuntimeError):
    """Raised when a network request or download fails."""


class RedirectBlocked(DownloadError):
    """Raised when a server tries to redirect a request."""


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class HttpResponse:
    url: str
    body: bytes
    content_type: str

    @property
    def text(self) -> str:
        return decode_html(self.body, self.content_type)


def decode_html(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip() or charset
            break
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def iri_to_uri(url: str) -> str:
    split = urlsplit(url)
    hostname = split.hostname
    if not hostname:
        return url

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        ascii_host = hostname

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


def jetpack_origin_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in JETPACK_IMAGE_HOSTS:
        return None

    path = parsed.path.lstrip("/")
    if "/" not in path:
        return None

    host, image_path = path.split("/", 1)
    if not host or not image_path:
        return None

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        ascii_host = host

    return urlunsplit(("https", ascii_host.lower(), f"/{image_path}", "", ""))


class HttpClient:
    """Small no-redirect HTTP client based on the Python standard library."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._opener = build_opener(NoRedirectHandler)
        self._base_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "eromatome-dl/0.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def get(self, url: str, *, referer: str | None = None) -> HttpResponse:
        headers = dict(self._base_headers)
        if referer:
            headers["Referer"] = iri_to_uri(referer)
        request_url = iri_to_uri(url)
        request = Request(request_url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if 300 <= status < 400:
                    raise RedirectBlocked(f"Redirect blocked for {url}")
                return HttpResponse(
                    url=url,
                    body=response.read(),
                    content_type=response.headers.get("Content-Type", ""),
                )
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RedirectBlocked(f"Redirect blocked for {url}") from exc
            raise DownloadError(f"HTTP {exc.code} for {url}") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise DownloadError(f"Network error for {url}: {reason}") from exc
        except OSError as exc:
            raise DownloadError(f"Could not request {url}: {exc}") from exc

    def download(
        self,
        url: str,
        destination: Path,
        *,
        referer: str | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        headers = {
            "User-Agent": self._base_headers["User-Agent"],
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = iri_to_uri(referer)

        request_url = iri_to_uri(url)
        attempt_urls = [request_url]
        origin_url = jetpack_origin_url(request_url)
        if origin_url and origin_url != request_url:
            attempt_urls.append(origin_url)
        part_path = destination.with_name(f"{destination.name}.part")

        for index, attempt_url in enumerate(attempt_urls):
            request = Request(attempt_url, headers=headers, method="GET")
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                    if 300 <= status < 400:
                        raise RedirectBlocked(f"Redirect blocked for {url}")

                    total_header = response.headers.get("Content-Length")
                    total = int(total_header) if total_header and total_header.isdigit() else None
                    downloaded = 0
                    with part_path.open("wb") as file:
                        while True:
                            chunk = response.read(1024 * 128)
                            if not chunk:
                                break
                            file.write(chunk)
                            downloaded += len(chunk)
                            if progress:
                                progress(downloaded, total)
                    os.replace(part_path, destination)
                    return
            except HTTPError as exc:
                part_path.unlink(missing_ok=True)
                if 300 <= exc.code < 400:
                    raise RedirectBlocked(f"Redirect blocked for {url}") from exc
                if exc.code == 404 and index == 0 and len(attempt_urls) > 1:
                    continue
                raise DownloadError(f"HTTP {exc.code} for {url}") from exc
            except URLError as exc:
                part_path.unlink(missing_ok=True)
                reason = getattr(exc, "reason", exc)
                raise DownloadError(f"Network error for {url}: {reason}") from exc
            except OSError as exc:
                part_path.unlink(missing_ok=True)
                raise DownloadError(f"Could not download {url}: {exc}") from exc
            except Exception:
                part_path.unlink(missing_ok=True)
                raise


def filename_from_url(url: str, *, fallback: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).name
    name = path_name or fallback
    stem = Path(name).stem or fallback
    suffix = Path(name).suffix
    if not suffix and content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        suffix = guessed or ""
    return f"{stem}{suffix}"
