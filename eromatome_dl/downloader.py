from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from eromatome_dl.http import HttpClient
from eromatome_dl.models import Article, ImageItem, sanitize_path_component, unique_folder
from eromatome_dl.sites import adapter_for_url
from eromatome_dl.sites.base import SiteParseError


class UnsupportedSiteError(ValueError):
    """Raised when no adapter supports the entered URL."""


@dataclass(frozen=True)
class DownloadResult:
    image: ImageItem
    path: Path
    skipped: bool = False


def scan_article(url: str, client: HttpClient | None = None) -> Article:
    adapter = adapter_for_url(url)
    if adapter is None:
        raise UnsupportedSiteError(f"No site adapter supports this URL: {url}")

    http = client or HttpClient()
    try:
        return adapter.scan(url, http)
    except SiteParseError:
        raise
    except ValueError as exc:
        raise SiteParseError(f"Could not parse article HTML: {exc}") from exc


def article_output_dir(article: Article, parent: Path, *, unique: bool = True) -> Path:
    folder_name = sanitize_path_component(article.title, fallback="article")
    return unique_folder(parent, folder_name) if unique else parent / folder_name


def image_filename(image: ImageItem) -> str:
    parsed = urlparse(image.url)
    raw_name = Path(unquote(parsed.path)).name
    fallback = f"image_{image.ordinal:03d}.jpg"
    name = sanitize_path_component(raw_name, fallback=fallback)
    stem = Path(name).stem or f"image_{image.ordinal:03d}"
    suffix = Path(name).suffix
    return f"{image.ordinal:03d}_{stem}{suffix}"


def download_article(
    article: Article,
    output_dir: Path,
    *,
    client: HttpClient | None = None,
    skip_existing: bool = True,
    before_image: Callable[[ImageItem], None] | None = None,
    item_progress: Callable[[ImageItem, int, int | None], None] | None = None,
    result_callback: Callable[[DownloadResult], None] | None = None,
) -> list[DownloadResult]:
    http = client or HttpClient()
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[DownloadResult] = []

    for image in article.images:
        if before_image:
            before_image(image)

        destination = output_dir / image_filename(image)
        if skip_existing and destination.exists():
            result = DownloadResult(image=image, path=destination, skipped=True)
            results.append(result)
            if result_callback:
                result_callback(result)
            continue

        def progress(downloaded: int, total: int | None, current: ImageItem = image) -> None:
            if item_progress:
                item_progress(current, downloaded, total)

        http.download(image.url, destination, referer=article.url, progress=progress)
        result = DownloadResult(image=image, path=destination)
        results.append(result)
        if result_callback:
            result_callback(result)

    return results
