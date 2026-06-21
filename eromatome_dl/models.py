from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse
import re


@dataclass(frozen=True)
class ImageItem:
    """One downloadable image discovered in an article."""

    ordinal: int
    url: str
    label: str = ""


@dataclass(frozen=True)
class Article:
    """Parsed article metadata and downloadable image URLs."""

    url: str
    title: str
    images: tuple[ImageItem, ...]


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_path_component(value: str, *, fallback: str = "download", limit: int = 120) -> str:
    """Return a Windows-friendly single path component."""

    normalized = WHITESPACE.sub(" ", value).strip()
    cleaned = INVALID_FILENAME_CHARS.sub("_", normalized)
    cleaned = cleaned.rstrip(" .")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" .")

    if not cleaned:
        cleaned = fallback

    if cleaned.upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"{cleaned}_"

    return cleaned


def title_fallback_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path:
        stem = Path(unquote(path)).stem
        if stem:
            return sanitize_path_component(stem, fallback="article")
    if parsed.netloc:
        return sanitize_path_component(parsed.netloc, fallback="article")
    return "article"


def unique_folder(parent: Path, folder_name: str) -> Path:
    """Return a non-existing folder path under parent."""

    base = parent / sanitize_path_component(folder_name, fallback="article")
    if not base.exists():
        return base

    for number in range(2, 1000):
        candidate = parent / f"{base.name} ({number})"
        if not candidate.exists():
            return candidate

    raise FileExistsError(f"Could not find an unused folder name for {base}")


def dedupe_images(items: Iterable[ImageItem]) -> tuple[ImageItem, ...]:
    seen: set[str] = set()
    deduped: list[ImageItem] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        deduped.append(ImageItem(len(deduped) + 1, item.url, item.label))
    return tuple(deduped)
