from __future__ import annotations

from eromatome_dl.encoding import configure_utf8

configure_utf8()

from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, Thread
from time import monotonic
from urllib.parse import urlparse
import re
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from eromatome_dl.downloader import (
    UnsupportedSiteError,
    article_output_dir,
    download_article,
    scan_article,
)
from eromatome_dl.http import DownloadError, HttpClient, RedirectBlocked
from eromatome_dl.models import Article, sanitize_path_component
from eromatome_dl.sites import adapter_for_url
from eromatome_dl.sites.base import SiteParseError


Event = tuple[str, object]
URL_PATTERN = re.compile(
    r"https?://[^\s<>'\"]+|"
    r"(?:"
    r"二次萌えエロ画像\.com|xn--r8jwklh769h2mc880dk1o431a\.com|"
    r"デブ専\.net|xn--edk4a626w\.net"
    r")/[^\s<>'\"]+",
    re.IGNORECASE,
)
TRAILING_URL_PUNCTUATION = ".,;:)]}"
ACTIVE_STATUSES = {"Scanning", "Downloading"}
SPEED_HOLD_SECONDS = 10.0


class QueueStopped(RuntimeError):
    """Raised inside the worker when the user stops the queue."""


@dataclass
class QueueItem:
    item_id: str
    url: str
    source: str
    status: str = "Queued"
    title: str = "-"
    image_count: int | None = None
    progress: float = 0.0
    speed: str = "-"
    speed_updated_at: float = 0.0
    article: Article | None = None
    output_dir: Path | None = None
    filter_dmm_fanza: bool | None = None
    error: str = ""

    def values(self) -> tuple[str, str, str, str, str, str, str]:
        image_count = str(self.image_count) if self.image_count is not None else "-"
        return (
            self.status,
            self.title,
            self.source,
            image_count,
            f"{self.progress:.0f}%",
            self.speed,
            self.url,
        )


@dataclass(frozen=True)
class WorkItem:
    item_id: str
    url: str
    source: str
    article: Article | None


def default_download_folder() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        return downloads
    return Path.cwd()


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        if not url.lower().startswith(("http://", "https://")):
            url = f"https://{url}"
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def source_name_for_url(url: str) -> str:
    adapter = adapter_for_url(url)
    if adapter:
        return adapter.name
    parsed = urlparse(url)
    return parsed.netloc or "-"


def output_dir_for_article(
    article: Article,
    download_folder: Path,
    source: str,
    create_site_subfolder: bool,
) -> Path:
    base_dir = download_folder
    if create_site_subfolder:
        base_dir = base_dir / sanitize_path_component(source, fallback="site")
    return article_output_dir(article, base_dir, unique=False)


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    value = bytes_per_second
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B/s" else f"{value:.0f} {unit}"
        value /= 1024
    return "-"


class DownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Eromatome DL")
        self.minsize(1080, 680)
        self._configure_text_rendering()

        self.items: dict[str, QueueItem] = {}
        self.item_order: list[str] = []
        self.events: Queue[Event] = Queue()
        self.queue_condition = Condition()
        self.worker: Thread | None = None
        self.active_mode: str | None = None
        self.paused = False
        self.stop_requested = False

        self.skip_existing = tk.BooleanVar(value=True)
        self.create_site_subfolder = tk.BooleanVar(value=False)
        self.filter_dmm_fanza = tk.BooleanVar(value=False)
        self.download_folder_var = tk.StringVar(value=str(default_download_folder()))
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_menu()
        self._build_widgets()
        self._update_buttons()
        self.after(100, self._drain_events)

    def _configure_text_rendering(self) -> None:
        family = self._preferred_ui_font()
        if not family:
            return

        for font_name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(font_name).configure(family=family)
            except tk.TclError:
                pass

        style = ttk.Style(self)
        default_size = tkfont.nametofont("TkDefaultFont").cget("size")
        style.configure("Treeview", font=(family, default_size), rowheight=max(22, abs(int(default_size)) + 12))
        style.configure("Treeview.Heading", font=(family, default_size, "bold"))

    def _preferred_ui_font(self) -> str:
        available = {family.lower(): family for family in tkfont.families(self)}
        for family in (
            "Yu Gothic UI",
            "Meiryo UI",
            "Meiryo",
            "MS PGothic",
            "MS Gothic",
            "Noto Sans CJK JP",
            "Segoe UI",
        ):
            if family.lower() in available:
                return available[family.lower()]
        return ""

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        options = tk.Menu(menu_bar, tearoff=False)
        options.add_checkbutton(
            label="Create site-dependent subfolders",
            variable=self.create_site_subfolder,
        )
        options.add_checkbutton(
            label="Filter DMM/FANZA image links",
            variable=self.filter_dmm_fanza,
            command=self._update_buttons,
        )
        options.add_checkbutton(
            label="Skip existing files",
            variable=self.skip_existing,
        )
        menu_bar.add_cascade(label="Options", menu=options)
        self.config(menu=menu_bar)

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=5)

        top = ttk.Frame(self, padding=(12, 12, 12, 8))
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)

        ttk.Label(top, text="Article Links").grid(row=0, column=0, sticky="nw", padx=(0, 8))
        input_frame = ttk.Frame(top)
        input_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        input_frame.columnconfigure(0, weight=1)
        input_frame.rowconfigure(0, weight=1)

        self.url_text = tk.Text(input_frame, height=8, wrap="word", undo=True)
        self.url_text.grid(row=0, column=0, sticky="nsew")
        self.url_text.focus_set()
        url_scroll = ttk.Scrollbar(input_frame, orient="vertical", command=self.url_text.yview)
        url_scroll.grid(row=0, column=1, sticky="ns")
        self.url_text.configure(yscrollcommand=url_scroll.set)

        actions = ttk.Frame(top)
        actions.grid(row=0, column=2, sticky="new")
        actions.columnconfigure(0, weight=1)
        self.add_button = ttk.Button(actions, text="Add Links", command=self.add_links)
        self.add_button.grid(row=0, column=0, sticky="ew")
        self.scan_button = ttk.Button(actions, text="Scan Queue", command=self.scan_queue)
        self.scan_button.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.download_button = ttk.Button(actions, text="Download Queue", command=self.download_queue)
        self.download_button.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.pause_button = ttk.Button(actions, text="Pause Queue", command=self.toggle_pause_queue)
        self.pause_button.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.stop_button = ttk.Button(actions, text="Stop Queue", command=self.stop_queue)
        self.stop_button.grid(row=4, column=0, sticky="ew", pady=(6, 0))

        ttk.Label(top, text="Download Folder").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Entry(top, textvariable=self.download_folder_var).grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(0, 8),
            pady=(10, 0),
        )
        ttk.Button(top, text="Browse", command=self.browse_download_folder).grid(
            row=1,
            column=2,
            sticky="ew",
            pady=(10, 0),
        )

        content = ttk.Frame(self, padding=(12, 0, 12, 8))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        columns = ("status", "title", "source", "images", "progress", "speed", "url")
        self.queue_table = ttk.Treeview(content, columns=columns, show="headings", selectmode="extended")
        self.queue_table.heading("status", text="Status")
        self.queue_table.heading("title", text="Title")
        self.queue_table.heading("source", text="Source")
        self.queue_table.heading("images", text="Images")
        self.queue_table.heading("progress", text="Progress")
        self.queue_table.heading("speed", text="Speed")
        self.queue_table.heading("url", text="URL")
        self.queue_table.column("status", width=108, anchor="w", stretch=False)
        self.queue_table.column("title", width=300, anchor="w")
        self.queue_table.column("source", width=120, anchor="w", stretch=False)
        self.queue_table.column("images", width=72, anchor="center", stretch=False)
        self.queue_table.column("progress", width=86, anchor="e", stretch=False)
        self.queue_table.column("speed", width=110, anchor="e", stretch=False)
        self.queue_table.column("url", width=300, anchor="w")
        self.queue_table.grid(row=0, column=0, sticky="nsew")

        table_scroll_y = ttk.Scrollbar(content, orient="vertical", command=self.queue_table.yview)
        table_scroll_y.grid(row=0, column=1, sticky="ns")
        table_scroll_x = ttk.Scrollbar(content, orient="horizontal", command=self.queue_table.xview)
        table_scroll_x.grid(row=1, column=0, sticky="ew")
        self.queue_table.configure(yscrollcommand=table_scroll_y.set, xscrollcommand=table_scroll_x.set)
        self.queue_table.bind("<Button-3>", self._show_queue_menu)
        self.queue_table.bind("<Control-Button-1>", self._show_queue_menu)
        self._build_queue_menu()

        queue_actions = ttk.Frame(content)
        queue_actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.remove_button = ttk.Button(queue_actions, text="Remove Selected", command=self.remove_selected)
        self.remove_button.grid(row=0, column=0)
        self.clear_button = ttk.Button(queue_actions, text="Clear Finished", command=self.clear_finished)
        self.clear_button.grid(row=0, column=1, padx=(8, 0))

        log_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        log_frame.grid(row=2, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)
        ttk.Label(log_frame, text="Activity").grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(log_frame, height=6, wrap="word", state="disabled")
        self.log_text.grid(row=1, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        bottom = ttk.Frame(self, padding=(12, 0, 12, 12))
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bottom, variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(bottom, textvariable=self.status_var, width=36).grid(row=0, column=1, sticky="e")

    def _build_queue_menu(self) -> None:
        self.queue_menu = tk.Menu(self, tearoff=False)
        self.queue_menu.add_command(label="Highest Priority", command=self.move_selected_to_top)
        self.queue_menu.add_command(label="Increase Priority", command=self.move_selected_up)
        self.queue_menu.add_command(label="Decrease Priority", command=self.move_selected_down)
        self.queue_menu.add_command(label="Lowest Priority", command=self.move_selected_to_bottom)
        self.queue_menu.add_separator()
        self.queue_menu.add_command(label="Remove", command=self.remove_selected)

    def _show_queue_menu(self, event) -> None:
        item_id = self.queue_table.identify_row(event.y)
        if item_id and item_id not in self.queue_table.selection():
            self.queue_table.selection_set(item_id)
        if item_id:
            self.queue_menu.tk_popup(event.x_root, event.y_root)

    def browse_download_folder(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self.download_folder_var.get() or str(default_download_folder())
        )
        if selected:
            self.download_folder_var.set(selected)

    def add_links(self) -> None:
        urls = extract_urls(self.url_text.get("1.0", "end"))
        if not urls:
            messagebox.showerror("No Links", "Paste one or more article links.")
            return

        existing_urls = {item.url for item in self.items.values()}
        added = 0
        skipped = 0
        for url in urls:
            if url in existing_urls:
                skipped += 1
                continue
            item_id = uuid4().hex
            item = QueueItem(item_id=item_id, url=url, source=source_name_for_url(url))
            with self.queue_condition:
                self.items[item_id] = item
                self.item_order.append(item_id)
                self.queue_condition.notify_all()
            self.queue_table.insert("", "end", iid=item_id, values=item.values())
            existing_urls.add(url)
            added += 1

        self.url_text.delete("1.0", "end")
        self._log(f"Added {added} link(s) to the queue.")
        if skipped:
            self._log(f"Skipped {skipped} duplicate link(s).")
        self._update_overall_progress_from_items()
        self._update_buttons()

    def scan_queue(self) -> None:
        if not self._has_scan_work():
            messagebox.showinfo("Nothing to scan", "The queue has no links that need scanning.")
            return
        self._start_worker("scan", "Scanning", self._scan_worker, self.filter_dmm_fanza.get())

    def download_queue(self) -> None:
        if not self._has_download_work():
            messagebox.showinfo("Nothing to download", "The queue has no pending links.")
            return
        download_folder = Path(self.download_folder_var.get()).expanduser()
        create_site_subfolder = self.create_site_subfolder.get()
        skip_existing = self.skip_existing.get()
        filter_dmm_fanza = self.filter_dmm_fanza.get()
        self._start_worker(
            "download",
            "Downloading",
            self._download_worker,
            download_folder,
            create_site_subfolder,
            skip_existing,
            filter_dmm_fanza,
        )

    def remove_selected(self) -> None:
        removed = 0
        skipped_active = 0
        with self.queue_condition:
            for item_id in self.queue_table.selection():
                item = self.items.get(item_id)
                if item is None:
                    continue
                if item.status in ACTIVE_STATUSES:
                    skipped_active += 1
                    continue
                self.items.pop(item_id, None)
                if item_id in self.item_order:
                    self.item_order.remove(item_id)
                self.queue_table.delete(item_id)
                removed += 1
            self.queue_condition.notify_all()
        if removed:
            self._log(f"Removed {removed} queued item(s).")
        if skipped_active:
            self._log(f"Skipped {skipped_active} active item(s); stop the queue before removing them.")
        self._update_overall_progress_from_items()
        self._update_buttons()

    def clear_finished(self) -> None:
        removed = 0
        with self.queue_condition:
            for item_id in list(self.item_order):
                item = self.items[item_id]
                if item.status == "Done":
                    self.items.pop(item_id, None)
                    self.item_order.remove(item_id)
                    self.queue_table.delete(item_id)
                    removed += 1
            self.queue_condition.notify_all()
        if removed:
            self._log(f"Cleared {removed} finished item(s).")
        self._update_overall_progress_from_items()
        self._update_buttons()

    def _work_item(self, item_id: str) -> WorkItem:
        item = self.items[item_id]
        return WorkItem(item_id=item.item_id, url=item.url, source=item.source, article=item.article)

    def _has_scan_work(self) -> bool:
        filter_dmm_fanza = self.filter_dmm_fanza.get()
        with self.queue_condition:
            return any(
                item.status != "Done"
                and item.status not in ACTIVE_STATUSES
                and (item.article is None or item.filter_dmm_fanza != filter_dmm_fanza)
                for item in self.items.values()
            )

    def _has_download_work(self) -> bool:
        with self.queue_condition:
            return any(
                item.status != "Done" and item.status not in ACTIVE_STATUSES
                for item in self.items.values()
            )

    def _next_scan_work(self, attempted: set[str], filter_dmm_fanza: bool) -> WorkItem | None:
        with self.queue_condition:
            if self.stop_requested:
                raise QueueStopped
            for item_id in self.item_order:
                if item_id in attempted:
                    continue
                item = self.items.get(item_id)
                if item is None or item.status == "Done" or item.status in ACTIVE_STATUSES:
                    continue
                if item.article is not None and item.filter_dmm_fanza == filter_dmm_fanza:
                    continue
                item.status = "Scanning"
                item.progress = 0
                item.speed = "-"
                item.speed_updated_at = 0
                item.error = ""
                return self._work_item(item_id)
        return None

    def _next_download_work(self, attempted: set[str], filter_dmm_fanza: bool) -> WorkItem | None:
        with self.queue_condition:
            if self.stop_requested:
                raise QueueStopped
            for item_id in self.item_order:
                if item_id in attempted:
                    continue
                item = self.items.get(item_id)
                if item is None or item.status == "Done" or item.status in ACTIVE_STATUSES:
                    continue
                needs_scan = item.article is None or item.filter_dmm_fanza != filter_dmm_fanza
                item.status = "Scanning" if needs_scan else "Downloading"
                item.progress = 0
                item.speed = "-"
                item.speed_updated_at = 0
                item.error = ""
                work = self._work_item(item_id)
                if needs_scan:
                    return WorkItem(
                        item_id=work.item_id,
                        url=work.url,
                        source=work.source,
                        article=None,
                    )
                return work
        return None

    def _wait_if_paused(self) -> None:
        with self.queue_condition:
            while self.paused and not self.stop_requested:
                self.queue_condition.wait(timeout=0.25)
            if self.stop_requested:
                raise QueueStopped

    def _raise_if_stopped(self) -> None:
        with self.queue_condition:
            if self.stop_requested:
                raise QueueStopped

    def _update_worker_item(self, item_id: str, **changes: object) -> bool:
        with self.queue_condition:
            item = self.items.get(item_id)
            if item is None:
                return False
            for key, value in changes.items():
                setattr(item, key, value)
                if key == "speed":
                    item.speed_updated_at = monotonic() if value != "-" else 0
            self.queue_condition.notify_all()
        return True

    def _mark_active_items_stopped(self) -> None:
        stopped_ids: list[str] = []
        with self.queue_condition:
            for item in self.items.values():
                if item.status in ACTIVE_STATUSES:
                    item.status = "Stopped"
                    item.speed = "-"
                    item.speed_updated_at = 0
                    stopped_ids.append(item.item_id)
            self.paused = False
            self.queue_condition.notify_all()
        for item_id in stopped_ids:
            self.events.put(("item_update", {"id": item_id, "status": "Stopped", "speed": "-"}))

    def _queue_snapshot(self) -> list[QueueItem]:
        with self.queue_condition:
            return [self.items[item_id] for item_id in self.item_order if item_id in self.items]

    def toggle_pause_queue(self) -> None:
        if not self._is_busy():
            return
        with self.queue_condition:
            self.paused = not self.paused
            paused = self.paused
            self.queue_condition.notify_all()
        if paused:
            self.status_var.set("Paused")
            self._log("Queue paused.")
        else:
            self.status_var.set("Downloading" if self.active_mode == "download" else "Scanning")
            self._log("Queue resumed.")
        self._update_buttons()

    def stop_queue(self) -> None:
        if not self._is_busy():
            return
        with self.queue_condition:
            self.stop_requested = True
            self.paused = False
            self.queue_condition.notify_all()
        self.status_var.set("Stopping")
        self._log("Stopping queue.")
        self._update_buttons()

    def move_selected_to_top(self) -> None:
        self._move_selected("top")

    def move_selected_up(self) -> None:
        self._move_selected("up")

    def move_selected_down(self) -> None:
        self._move_selected("down")

    def move_selected_to_bottom(self) -> None:
        self._move_selected("bottom")

    def _move_selected(self, direction: str) -> None:
        selected = [item_id for item_id in self.queue_table.selection() if item_id in self.items]
        if not selected:
            return
        with self.queue_condition:
            order = [item_id for item_id in self.item_order if item_id in self.items]
            selected_set = set(selected)
            selected_in_order = [item_id for item_id in order if item_id in selected_set]
            if direction == "top":
                self.item_order = selected_in_order + [item_id for item_id in order if item_id not in selected_set]
            elif direction == "bottom":
                self.item_order = [item_id for item_id in order if item_id not in selected_set] + selected_in_order
            elif direction == "up":
                self.item_order = self._shift_order(order, selected_set, -1)
            elif direction == "down":
                self.item_order = self._shift_order(order, selected_set, 1)
            self.queue_condition.notify_all()
        self._reorder_table()
        self._update_buttons()

    def _shift_order(self, order: list[str], selected: set[str], step: int) -> list[str]:
        new_order = list(order)
        indexes = range(1, len(new_order)) if step < 0 else range(len(new_order) - 2, -1, -1)
        for index in indexes:
            item_id = new_order[index]
            swap_index = index + step
            if item_id in selected and new_order[swap_index] not in selected:
                new_order[index], new_order[swap_index] = new_order[swap_index], new_order[index]
        return new_order

    def _reorder_table(self) -> None:
        for index, item_id in enumerate(self.item_order):
            if self.queue_table.exists(item_id):
                self.queue_table.move(item_id, "", index)

    def _start_worker(self, mode: str, label: str, target, *args) -> None:
        if self._is_busy():
            messagebox.showwarning("Busy", "A queue operation is already running.")
            return
        with self.queue_condition:
            self.active_mode = mode
            self.paused = False
            self.stop_requested = False
        self.pause_button.configure(text="Pause Queue")
        self.status_var.set(label)
        self._update_overall_progress_from_items()
        self._set_busy(True)
        self.worker = Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def _scan_worker(self, filter_dmm_fanza: bool) -> None:
        http = HttpClient()
        message = "Scan complete"
        attempted: set[str] = set()
        try:
            while True:
                self._wait_if_paused()
                work = self._next_scan_work(attempted, filter_dmm_fanza)
                if work is None:
                    break

                try:
                    self.events.put(
                        ("item_update", {"id": work.item_id, "status": "Scanning", "progress": 0, "speed": "-"})
                    )
                    self.events.put(("log", f"Scanning: {work.url}"))
                    self._raise_if_stopped()
                    article = scan_article(work.url, http, filter_dmm_fanza=filter_dmm_fanza)
                    self._raise_if_stopped()
                    source = source_name_for_url(work.url)
                    self._update_worker_item(
                        work.item_id,
                        article=article,
                        source=source,
                        title=article.title,
                        image_count=len(article.images),
                        filter_dmm_fanza=filter_dmm_fanza,
                        status="Ready",
                        progress=100,
                        speed="-",
                    )
                    self.events.put(
                        (
                            "item_scanned",
                            {
                                "id": work.item_id,
                                "article": article,
                                "source": source,
                                "filter_dmm_fanza": filter_dmm_fanza,
                                "progress": 100,
                            },
                        )
                    )
                    self.events.put(("log", f"Found {len(article.images)} image(s): {article.title}"))
                except QueueStopped:
                    raise
                except (UnsupportedSiteError, SiteParseError, RedirectBlocked, DownloadError, ValueError, OSError) as exc:
                    self._update_worker_item(work.item_id, status="Error", error=str(exc), speed="-")
                    self.events.put(("item_error", {"id": work.item_id, "error": str(exc)}))
                finally:
                    attempted.add(work.item_id)
        except QueueStopped:
            message = "Queue stopped"
            self._mark_active_items_stopped()
        finally:
            self.events.put(("worker_done", message))

    def _download_worker(
        self,
        download_folder: Path,
        create_site_subfolder: bool,
        skip_existing: bool,
        filter_dmm_fanza: bool,
    ) -> None:
        http = HttpClient()
        message = "Download complete"
        attempted: set[str] = set()

        try:
            while True:
                self._wait_if_paused()
                work = self._next_download_work(attempted, filter_dmm_fanza)
                if work is None:
                    break

                try:
                    article = work.article
                    source = work.source
                    if article is None:
                        self.events.put(
                            ("item_update", {"id": work.item_id, "status": "Scanning", "progress": 0, "speed": "-"})
                        )
                        self.events.put(("log", f"Scanning: {work.url}"))
                        self._raise_if_stopped()
                        article = scan_article(work.url, http, filter_dmm_fanza=filter_dmm_fanza)
                        self._raise_if_stopped()
                        source = source_name_for_url(work.url)
                        self._update_worker_item(
                            work.item_id,
                            article=article,
                            source=source,
                            title=article.title,
                            image_count=len(article.images),
                            filter_dmm_fanza=filter_dmm_fanza,
                            status="Ready",
                            progress=0,
                            speed="-",
                        )
                        self.events.put(
                            (
                                "item_scanned",
                                {
                                    "id": work.item_id,
                                    "article": article,
                                    "source": source,
                                    "filter_dmm_fanza": filter_dmm_fanza,
                                    "progress": 0,
                                },
                            )
                        )

                    self._raise_if_stopped()
                    output_dir = output_dir_for_article(article, download_folder, source, create_site_subfolder)
                    image_total = len(article.images)
                    if image_total == 0:
                        raise ValueError("No images found")
                    completed_images = 0
                    completed_bytes = 0
                    started_at = monotonic()

                    self._update_worker_item(
                        work.item_id,
                        status="Downloading",
                        progress=0,
                        speed="-",
                        output_dir=output_dir,
                    )
                    self.events.put(
                        (
                            "item_update",
                            {
                                "id": work.item_id,
                                "status": "Downloading",
                                "progress": 0,
                                "speed": "-",
                                "output_dir": output_dir,
                            },
                        )
                    )
                    self.events.put(("log", f"Downloading to: {output_dir}"))

                    def before_image(_image) -> None:
                        self._wait_if_paused()
                        self._raise_if_stopped()

                    def progress(image, downloaded: int, total: int | None) -> None:
                        self._wait_if_paused()
                        self._raise_if_stopped()
                        elapsed = max(monotonic() - started_at, 0.001)
                        bytes_per_second = (completed_bytes + downloaded) / elapsed
                        if total:
                            image_fraction = downloaded / total
                            percent = ((image.ordinal - 1 + image_fraction) / image_total) * 100
                        else:
                            percent = (completed_images / image_total) * 100
                        self.events.put(
                            (
                                "item_progress",
                                {
                                    "id": work.item_id,
                                    "progress": percent,
                                    "speed": format_speed(bytes_per_second),
                                },
                            )
                        )

                    def on_result(result) -> None:
                        nonlocal completed_images, completed_bytes
                        completed_images += 1
                        if result.skipped:
                            self.events.put(("log", f"Skipped existing: {result.path.name}"))
                        else:
                            try:
                                completed_bytes += result.path.stat().st_size
                            except OSError:
                                pass
                            self.events.put(("log", f"Saved: {result.path.name}"))
                        percent = (completed_images / image_total) * 100
                        self.events.put(("item_progress", {"id": work.item_id, "progress": percent}))

                    results = download_article(
                        article,
                        output_dir,
                        client=http,
                        skip_existing=skip_existing,
                        before_image=before_image,
                        item_progress=progress,
                        result_callback=on_result,
                    )
                    self._update_worker_item(work.item_id, status="Done", progress=100, speed="-", output_dir=output_dir)
                    self.events.put(
                        (
                            "item_done",
                            {
                                "id": work.item_id,
                                "count": len(results),
                                "output_dir": output_dir,
                            },
                        )
                    )
                except QueueStopped:
                    raise
                except (UnsupportedSiteError, SiteParseError, RedirectBlocked, DownloadError, ValueError, OSError) as exc:
                    self._update_worker_item(work.item_id, status="Error", error=str(exc), speed="-")
                    self.events.put(("item_error", {"id": work.item_id, "error": str(exc)}))
                finally:
                    attempted.add(work.item_id)
        except QueueStopped:
            message = "Queue stopped"
            self._mark_active_items_stopped()
        finally:
            self.events.put(("worker_done", message))

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._log(str(payload))
                elif event == "item_update":
                    self._apply_item_update(payload)
                elif event == "item_scanned":
                    self._apply_item_scanned(payload)
                elif event == "item_progress":
                    self._apply_item_update(payload)
                elif event == "item_done":
                    self._apply_item_done(payload)
                elif event == "item_error":
                    self._apply_item_error(payload)
                elif event == "worker_done":
                    self.status_var.set(str(payload))
                    self.worker = None
                    with self.queue_condition:
                        self.active_mode = None
                        self.paused = False
                        self.stop_requested = False
                        self.queue_condition.notify_all()
                    self._set_busy(False)
                    self._update_buttons()
        except Empty:
            pass
        self._expire_stale_speeds()
        self._update_overall_progress_from_items()
        self.after(100, self._drain_events)

    def _apply_item_update(self, payload: object) -> None:
        data = dict(payload)
        with self.queue_condition:
            item = self.items.get(str(data["id"]))
            if not item:
                return
            for key in ("status", "progress", "speed", "output_dir"):
                if key in data:
                    setattr(item, key, data[key])
                    if key == "speed":
                        item.speed_updated_at = monotonic() if data[key] != "-" else 0
        self._refresh_item(item)

    def _apply_item_scanned(self, payload: object) -> None:
        data = dict(payload)
        with self.queue_condition:
            item = self.items.get(str(data["id"]))
            if not item:
                return
            article = data["article"]
            source = str(data["source"])
            item.article = article
            item.source = source
            item.title = article.title
            item.image_count = len(article.images)
            if "filter_dmm_fanza" in data:
                item.filter_dmm_fanza = bool(data["filter_dmm_fanza"])
            item.status = "Ready"
            item.progress = float(data.get("progress", 0))
            item.speed = "-"
            item.speed_updated_at = 0
        self._refresh_item(item)

    def _apply_item_done(self, payload: object) -> None:
        data = dict(payload)
        with self.queue_condition:
            item = self.items.get(str(data["id"]))
            if not item:
                return
            item.status = "Done"
            item.progress = 100
            item.speed = "-"
            item.speed_updated_at = 0
            item.output_dir = data.get("output_dir")
        self._refresh_item(item)
        self._log(f"Finished {data['count']} image(s): {item.title}")

    def _apply_item_error(self, payload: object) -> None:
        data = dict(payload)
        with self.queue_condition:
            item = self.items.get(str(data["id"]))
            if not item:
                return
            item.status = "Error"
            item.error = str(data["error"])
            item.speed = "-"
            item.speed_updated_at = 0
        self._refresh_item(item)
        self._log(f"Error for {item.url}: {item.error}")

    def _refresh_item(self, item: QueueItem) -> None:
        if self.queue_table.exists(item.item_id):
            self.queue_table.item(item.item_id, values=item.values())

    def _expire_stale_speeds(self) -> None:
        now = monotonic()
        expired: list[QueueItem] = []
        with self.queue_condition:
            for item in self.items.values():
                if item.speed == "-" or not item.speed_updated_at:
                    continue
                if now - item.speed_updated_at >= SPEED_HOLD_SECONDS:
                    item.speed = "-"
                    item.speed_updated_at = 0
                    expired.append(item)
        for item in expired:
            self._refresh_item(item)

    def _update_overall_progress_from_items(self) -> None:
        items = self._queue_snapshot()
        if not items:
            self.progress_var.set(0)
            return

        mode = self.active_mode

        def effective_progress(item: QueueItem) -> float:
            if mode == "scan":
                if item.article is not None or item.status == "Error":
                    return 100
                return item.progress if item.status in {"Scanning", "Stopped"} else 0
            if mode == "download":
                if item.status in {"Done", "Error"}:
                    return 100
                if item.status in {"Downloading", "Stopped"}:
                    return item.progress
                return 0
            if item.status in {"Done", "Error"}:
                return 100
            return item.progress

        progress = sum(max(0, min(100, effective_progress(item))) for item in items) / len(items)
        self.progress_var.set(progress)

    def _set_busy(self, busy: bool) -> None:
        self.add_button.configure(state="normal")
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.download_button.configure(state="disabled" if busy else "normal")
        self.pause_button.configure(state="normal" if busy else "disabled")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.pause_button.configure(text="Pause Queue")

    def _update_buttons(self) -> None:
        busy = self._is_busy()
        self.add_button.configure(state="normal")
        self.scan_button.configure(state="disabled" if busy or not self._has_scan_work() else "normal")
        self.download_button.configure(state="disabled" if busy or not self._has_download_work() else "normal")
        self.pause_button.configure(state="normal" if busy else "disabled")
        self.pause_button.configure(text="Resume Queue" if busy and self.paused else "Pause Queue")
        self.stop_button.configure(state="normal" if busy and not self.stop_requested else "disabled")
        self.remove_button.configure(state="normal" if self.items else "disabled")
        has_finished = any(item.status == "Done" for item in self.items.values())
        self.clear_button.configure(state="normal" if has_finished else "disabled")

    def _is_busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    app = DownloaderApp()
    app.mainloop()
