#!/usr/bin/env python3
"""Desktop GUI for TitleTracer, built on Tkinter (ships with Python --
no extra dependency). Wraps the same scan/apply engine the CLI uses:
Preview runs a full scan and shows the plan in a table without touching
any files; Apply performs the renames from that cached plan (no
re-scanning). See README.md for the CLI if you'd rather script this."""

import json
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from titletracer.cli import build_plan_movie, build_plan_tv, resolve_episodes
from titletracer.config import DEFAULT_EXTENSIONS, DEFAULT_PATTERN, JELLYFIN_PATTERN, RunConfig
from titletracer.engine import ScanCancelled, apply_plan

logger = logging.getLogger("titletracer")


class QueueLogHandler(logging.Handler):
    """Pushes formatted log records into a thread-safe queue for the GUI
    thread to drain -- logging calls happen on the background scan
    thread, but only the main thread may touch Tkinter widgets."""

    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.queue = q

    def emit(self, record: logging.LogRecord) -> None:
        self.queue.put(self.format(record))


class TitleTracerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("TitleTracer")
        root.geometry("900x700")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.plan = []
        self.scan_thread = None
        self.cancel_event = threading.Event()

        self.mode = tk.StringVar(value="tv")
        self.directory = tk.StringVar()
        self.show_name = tk.StringVar()
        self.source = tk.StringVar(value="tvmaze")
        self.episodes_json = tk.StringVar()
        self.tvmaze_id = tk.StringVar()
        self.tmdb_api_key = tk.StringVar()
        self.movies_json = tk.StringVar()
        self.season = tk.StringVar()
        self.threshold = tk.DoubleVar(value=80.0)
        self.interval = tk.DoubleVar(value=5.0)
        self.max_scan = tk.DoubleVar(value=300.0)
        self.full_scan = tk.BooleanVar(value=False)
        self.crop = tk.StringVar(value="center")
        self.ocr_lang = tk.StringVar(value="eng")
        self.jellyfin = tk.BooleanVar(value=True)
        self.organize = tk.BooleanVar(value=True)
        self.fill_gaps = tk.BooleanVar(value=False)
        self.absolute_numbering = tk.BooleanVar(value=False)
        self.vlm_verify = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="Pick a directory and click Preview.")

        self._build_widgets()
        self._on_mode_change()
        self.root.after(100, self._drain_log_queue)

    # -- widget construction -------------------------------------------------

    def _build_widgets(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        mode_frame = ttk.LabelFrame(top, text="Mode", padding=8)
        mode_frame.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(
            mode_frame, text="TV Show", value="tv", variable=self.mode, command=self._on_mode_change,
        ).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(
            mode_frame, text="Movie", value="movie", variable=self.mode, command=self._on_mode_change,
        ).pack(side="left")

        dir_frame = ttk.Frame(top)
        dir_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(dir_frame, text="Directory:").pack(side="left")
        ttk.Entry(dir_frame, textvariable=self.directory).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(dir_frame, text="Browse...", command=self._browse_directory).pack(side="left")

        # -- TV-specific fields --
        self.tv_frame = ttk.LabelFrame(top, text="TV Show options", padding=8)
        row1 = ttk.Frame(self.tv_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Show name:", width=14).pack(side="left")
        ttk.Entry(row1, textvariable=self.show_name).pack(side="left", fill="x", expand=True)

        row2 = ttk.Frame(self.tv_frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Source:", width=14).pack(side="left")
        ttk.Combobox(
            row2, textvariable=self.source, values=["tvmaze", "tmdb", "local"], width=10, state="readonly",
        ).pack(side="left")
        ttk.Label(row2, text="  Season (optional):").pack(side="left", padx=(12, 4))
        ttk.Entry(row2, textvariable=self.season, width=6).pack(side="left")
        ttk.Label(row2, text="  TVMaze ID (optional):").pack(side="left", padx=(12, 4))
        ttk.Entry(row2, textvariable=self.tvmaze_id, width=8).pack(side="left")

        row3 = ttk.Frame(self.tv_frame)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Episodes JSON:", width=14).pack(side="left")
        ttk.Entry(row3, textvariable=self.episodes_json).pack(side="left", fill="x", expand=True)
        ttk.Button(row3, text="Browse...", command=self._browse_episodes_json).pack(side="left", padx=(4, 4))
        ttk.Button(row3, text="Export Episodes JSON...", command=self._on_export_episodes).pack(side="left")

        row4 = ttk.Frame(self.tv_frame)
        row4.pack(fill="x", pady=2)
        ttk.Checkbutton(row4, text="Fill gaps (position-inference for title-card-less episodes)",
                         variable=self.fill_gaps).pack(side="left")

        row5 = ttk.Frame(self.tv_frame)
        row5.pack(fill="x", pady=2)
        ttk.Checkbutton(
            row5, text="Absolute numbering (collapse to one season -- for shows whose "
                       "source-database 'seasons' are actually broadcast years)",
            variable=self.absolute_numbering,
        ).pack(side="left")

        # -- Movie-specific fields --
        self.movie_frame = ttk.LabelFrame(top, text="Movie options", padding=8)
        mrow1 = ttk.Frame(self.movie_frame)
        mrow1.pack(fill="x", pady=2)
        ttk.Label(mrow1, text="TMDb API key:", width=14).pack(side="left")
        ttk.Entry(mrow1, textvariable=self.tmdb_api_key, show="*").pack(side="left", fill="x", expand=True)

        mrow2 = ttk.Frame(self.movie_frame)
        mrow2.pack(fill="x", pady=2)
        ttk.Label(mrow2, text="Overrides JSON:", width=14).pack(side="left")
        ttk.Entry(mrow2, textvariable=self.movies_json).pack(side="left", fill="x", expand=True)
        ttk.Button(mrow2, text="Browse...", command=self._browse_movies_json).pack(side="left")

        # -- Shared options --
        shared = ttk.LabelFrame(top, text="Options", padding=8)
        shared.pack(fill="x", pady=(8, 8))
        srow1 = ttk.Frame(shared)
        srow1.pack(fill="x", pady=2)
        ttk.Label(srow1, text="Threshold:").pack(side="left")
        ttk.Spinbox(srow1, from_=0, to=100, textvariable=self.threshold, width=6).pack(side="left", padx=(4, 16))
        ttk.Label(srow1, text="Interval (s):").pack(side="left")
        ttk.Spinbox(srow1, from_=1, to=60, textvariable=self.interval, width=6).pack(side="left", padx=(4, 16))
        self.max_scan_spin = ttk.Spinbox(srow1, from_=10, to=36000, textvariable=self.max_scan, width=6)
        ttk.Label(srow1, text="Max scan (s):").pack(side="left")
        self.max_scan_spin.pack(side="left", padx=(4, 16))
        ttk.Checkbutton(
            srow1, text="Full scan (ignore cap)", variable=self.full_scan, command=self._on_full_scan_change,
        ).pack(side="left", padx=(0, 16))
        ttk.Label(srow1, text="Crop:").pack(side="left")
        ttk.Combobox(
            srow1, textvariable=self.crop, width=12, state="readonly",
            values=["full", "center", "lower-third", "upper-third"],
        ).pack(side="left", padx=4)

        srow2 = ttk.Frame(shared)
        srow2.pack(fill="x", pady=2)
        ttk.Checkbutton(srow2, text="Jellyfin naming", variable=self.jellyfin).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(srow2, text="Organize into subfolders", variable=self.organize).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(srow2, text="VLM fallback (Ollama, TV only)", variable=self.vlm_verify).pack(side="left")

        srow3 = ttk.Frame(shared)
        srow3.pack(fill="x", pady=2)
        ttk.Label(srow3, text="OCR language(s):").pack(side="left")
        ttk.Entry(srow3, textvariable=self.ocr_lang, width=12).pack(side="left", padx=(4, 6))
        ttk.Label(
            srow3, text="TV only -- e.g. 'eng', 'jpn', or 'eng+jpn' (needs matching traineddata installed)",
            foreground="gray",
        ).pack(side="left")

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill="x", pady=(4, 0))
        self.preview_btn = ttk.Button(btn_frame, text="Preview", command=self._on_preview)
        self.preview_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.apply_btn = ttk.Button(btn_frame, text="Apply Renames", command=self._on_apply, state="disabled")
        self.apply_btn.pack(side="left")
        ttk.Label(btn_frame, textvariable=self.status_text).pack(side="left", padx=12)

        # -- Results table --
        table_frame = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        table_frame.pack(fill="both", expand=True)
        columns = ("file", "status", "label", "score", "target")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        for col, width in zip(columns, (200, 110, 220, 60, 260)):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=width, anchor="w")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # -- Log pane --
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=(10, 4))
        log_frame.pack(fill="both", expand=False, padx=10, pady=10)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _on_mode_change(self):
        self.tv_frame.pack_forget()
        self.movie_frame.pack_forget()
        if self.mode.get() == "tv":
            self.tv_frame.pack(fill="x", pady=(0, 8))
        else:
            self.movie_frame.pack(fill="x", pady=(0, 8))

    def _on_full_scan_change(self):
        self.max_scan_spin["state"] = "disabled" if self.full_scan.get() else "normal"

    # -- file/dir pickers -----------------------------------------------------

    def _browse_directory(self):
        path = filedialog.askdirectory()
        if path:
            self.directory.set(path)

    def _browse_episodes_json(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path:
            self.episodes_json.set(path)

    def _browse_movies_json(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path:
            self.movies_json.set(path)

    # -- config assembly -------------------------------------------------------

    def _build_config(self) -> RunConfig:
        pattern = JELLYFIN_PATTERN if self.jellyfin.get() else DEFAULT_PATTERN
        season = int(self.season.get()) if self.season.get().strip() else None
        tvmaze_id = int(self.tvmaze_id.get()) if self.tvmaze_id.get().strip() else None
        return RunConfig(
            directory=Path(self.directory.get()),
            show_name=self.show_name.get() or None,
            mode=self.mode.get(),
            interactive=False,  # never block on input() from the GUI thread
            source=self.source.get(),
            local_json=Path(self.episodes_json.get()) if self.episodes_json.get() else None,
            movies_json=Path(self.movies_json.get()) if self.movies_json.get() else None,
            tmdb_api_key=self.tmdb_api_key.get() or None,
            tvmaze_id=tvmaze_id,
            season=season,
            interval_sec=self.interval.get(),
            max_scan_sec=0.0 if self.full_scan.get() else self.max_scan.get(),
            crop_mode=self.crop.get(),
            ocr_lang=self.ocr_lang.get() or "eng",
            threshold=self.threshold.get(),
            extensions=list(DEFAULT_EXTENSIONS),
            pattern=pattern if self.mode.get() == "tv" else DEFAULT_PATTERN,
            organize_seasons=self.organize.get(),
            fill_gaps=self.fill_gaps.get(),
            absolute_numbering=self.absolute_numbering.get(),
            vlm_verify=self.vlm_verify.get(),
        )

    # -- preview / apply --------------------------------------------------------

    def _on_preview(self):
        if not self.directory.get():
            messagebox.showerror("TitleTracer", "Pick a directory first.")
            return
        if self.mode.get() == "tv" and not self.show_name.get():
            messagebox.showerror("TitleTracer", "Show name is required for TV mode.")
            return

        self._set_log_handler()
        self.tree.delete(*self.tree.get_children())
        self.plan = []
        self.cancel_event.clear()
        self.apply_btn["state"] = "disabled"
        self.preview_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"
        self.status_text.set("Scanning...")

        cfg = self._build_config()
        self.scan_thread = threading.Thread(target=self._scan_worker, args=(cfg,), daemon=True)
        self.scan_thread.start()

    def _on_cancel(self):
        self.cancel_event.set()
        self.cancel_btn["state"] = "disabled"
        self.status_text.set("Cancelling...")

    def _scan_worker(self, cfg: RunConfig):
        def on_progress(idx, total, video):
            if self.cancel_event.is_set():
                raise ScanCancelled()
            self.log_queue.put(f"__PROGRESS__ {idx}/{total} {video.name}")

        try:
            if cfg.mode == "tv":
                plan = build_plan_tv(cfg, on_progress=on_progress)
            else:
                plan = build_plan_movie(cfg, on_progress=on_progress)
            self.log_queue.put("__PLAN_DONE__")
            self.plan = plan
        except ScanCancelled:
            self.log_queue.put("__PLAN_CANCELLED__")
        except RuntimeError as exc:
            self.log_queue.put(f"__PLAN_ERROR__ {exc}")

    def _on_export_episodes(self):
        if self.mode.get() != "tv":
            messagebox.showerror("TitleTracer", "Export Episodes JSON only applies to TV mode.")
            return
        if not self.show_name.get():
            messagebox.showerror("TitleTracer", "Show name is required.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return

        self._set_log_handler()
        cfg = self._build_config()
        threading.Thread(target=self._export_worker, args=(cfg, Path(path)), daemon=True).start()

    def _export_worker(self, cfg: RunConfig, path: Path):
        try:
            episodes = resolve_episodes(cfg)
            payload = {"episodes": [
                {"season": e.season, "episode": e.number, "title": e.title} for e in episodes
            ]}
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.log_queue.put(f"__EXPORT_DONE__ {len(episodes)}|{path}")
        except RuntimeError as exc:
            self.log_queue.put(f"__EXPORT_ERROR__ {exc}")

    def _on_apply(self):
        if not self.plan:
            return
        matched = [it for it in self.plan if it.status in ("matched", "matched_inferred")]
        if not matched:
            messagebox.showinfo("TitleTracer", "Nothing to apply -- no matched files in the plan.")
            return
        if not messagebox.askyesno(
            "TitleTracer", f"Rename {len(matched)} file(s) now? This cannot be undone automatically.",
        ):
            return

        self._set_log_handler()
        cfg = self._build_config()
        applied = apply_plan(self.plan, cfg.directory, dry_run=False)
        self.status_text.set(f"Applied {applied} rename(s).")
        self._refresh_table()
        messagebox.showinfo("TitleTracer", f"Renamed {applied} file(s).")

    # -- logging / table plumbing ------------------------------------------------

    def _set_log_handler(self):
        for h in list(logger.handlers):
            if isinstance(h, QueueLogHandler):
                logger.removeHandler(h)
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    def _drain_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line == "__PLAN_DONE__":
                    self._on_scan_done()
                elif line == "__PLAN_CANCELLED__":
                    self._on_scan_cancelled()
                elif line.startswith("__PLAN_ERROR__"):
                    self._on_scan_error(line[len("__PLAN_ERROR__ "):])
                elif line.startswith("__EXPORT_DONE__"):
                    self._on_export_done(line[len("__EXPORT_DONE__ "):])
                elif line.startswith("__EXPORT_ERROR__"):
                    messagebox.showerror("TitleTracer", line[len("__EXPORT_ERROR__ "):])
                elif line.startswith("__PROGRESS__"):
                    self.status_text.set(line[len("__PROGRESS__ "):])
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _append_log(self, line: str):
        self.log_text["state"] = "normal"
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text["state"] = "disabled"

    def _on_scan_done(self):
        self.preview_btn["state"] = "normal"
        self.cancel_btn["state"] = "disabled"
        self._refresh_table()
        matched = sum(1 for it in self.plan if it.status in ("matched", "matched_inferred"))
        self.status_text.set(f"Preview complete: {matched}/{len(self.plan)} matched.")
        self.apply_btn["state"] = "normal" if matched else "disabled"

    def _on_scan_cancelled(self):
        self.preview_btn["state"] = "normal"
        self.cancel_btn["state"] = "disabled"
        self.status_text.set("Cancelled.")

    def _on_scan_error(self, message: str):
        self.preview_btn["state"] = "normal"
        self.cancel_btn["state"] = "disabled"
        self.status_text.set("Error -- see log.")
        messagebox.showerror("TitleTracer", message)

    def _on_export_done(self, payload: str):
        count, path = payload.split("|", 1)
        if messagebox.askyesno(
            "TitleTracer", f"Wrote {count} episode(s) to {path}.\n\nLoad it now (Source: local)?",
        ):
            self.source.set("local")
            self.episodes_json.set(path)

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for item in self.plan:
            self.tree.insert("", "end", values=(
                item.video.name, item.status, item.label,
                f"{item.score:.0f}" if item.score else "", item.target_display or item.note,
            ))


def main():
    root = tk.Tk()
    TitleTracerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
