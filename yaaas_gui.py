#!/usr/bin/env python3
"""YAAAS GUI. Keep yaaas.py in the same folder.

Fedora:         sudo dnf install python3-tkinter
Debian/Ubuntu:  sudo apt install python3-tk
Run:            python3 yaaas_gui.py
"""
import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import yaaas as core

LOW_SCORE = 85  # fuzzy matches under this get highlighted


def fmt_len(sec):
    return f"{int(sec) // 60}:{int(sec) % 60:02d}" if sec else ""


class SegmentedBar(tk.Canvas):
    """Progress bar in two labelled parts: downloading and syncing."""
    DL, SY, OFF = "#8ec1f0", "#9fd89f", "#d0d0d0"

    def __init__(self, master):
        super().__init__(master, height=30, highlightthickness=0)
        self.state = (0, 0, 0, 0, "idle")
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, dl_done, dl_total, sync_done, sync_total, stage="idle"):
        self.state = (dl_done, dl_total, sync_done, sync_total, stage)
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), int(self["height"])
        dd, dt, sd, st, stage = self.state
        known = dt > 0 or st > 0
        dl_off = known and dt == 0
        sync_off = known and st == 0
        if not known:
            frac = 0.5
        elif dl_off:
            frac = 0.2
        elif sync_off:
            frac = 0.8
        else:
            frac = min(max(dt / (dt + st), 0.25), 0.75)
        gap = 6
        split = (w - gap) * frac
        parts = (
            ("Downloading (off)" if dl_off else "Downloading", dd, dt, self.OFF if dl_off else self.DL,
             0, split, stage == "download", known and not dl_off),
            ("Syncing (off)" if sync_off else "Syncing", sd, st, self.OFF if sync_off else self.SY,
             split + gap, w - 1, stage == "sync", known and not sync_off),
        )
        for label, done, total, color, x0, x1, active, counts in parts:
            self.create_rectangle(x0, 1, x1, h - 1, fill="#ececec", outline="#9a9a9a")
            if total:
                self.create_rectangle(x0 + 1, 2, x0 + 1 + (x1 - x0 - 2) * done / total, h - 2,
                                      fill=color, outline="")
            if active:
                self.create_rectangle(x0, 1, x1, h - 1, outline="#222222", width=2)
            text = f"{label}  {done}/{total}" if counts else label
            self.create_text((x0 + x1) / 2, h / 2, text=text, fill="#222222",
                             font=("TkDefaultFont", 10, "bold" if active else "normal"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YAAAS")
        self.geometry("1180x920")
        self.minsize(960, 720)
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.yt = None
        self.albums = []
        self.imported = {}   # text line -> json entry it came from (keeps the album info)
        self.items = {}   # tree id -> ("album", album) or ("track", track, album)
        self.busy = False
        self.mode = "download"   # what the review list holds: "download" or "folder"
        core.set_logger(lambda s: self.q.put(("log", s)))

        self.expand_var = tk.BooleanVar(value=True)
        self.thr_var = tk.IntVar(value=70)
        self.order_var = tk.StringVar(value="auto")
        self.out_var = tk.StringVar(value=os.path.expanduser("~/Music"))
        self.fmt_var = tk.StringVar(value="mp3")
        self.lyrics_var = tk.StringVar(value="all")
        self.device_var = tk.StringVar(value="auto")
        self.lang_var = tk.StringVar()
        self.model_var = tk.StringVar(value="htdemucs")
        self.force_var = tk.BooleanVar(value=False)
        self.transcribe_var = tk.BooleanVar(value=True)
        self.tmodel_var = tk.StringVar(value="auto")
        self.scan_var = tk.StringVar(value=os.path.expanduser("~/Music"))
        self.upgrade_var = tk.BooleanVar(value=False)

        self.build()
        self.after(100, self.poll)

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(3, weight=2)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        top = ttk.Frame(nb, padding=8)
        nb.add(top, text="Songs / links")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="One per line: \"Artist - Title\" or \"Title - Artist\" (auto-detected), or a "
                            "YouTube / YouTube Music link (video, album or playlist).").grid(row=0, column=0, columnspan=2, sticky="w")
        self.txt = tk.Text(top, height=5, wrap="none", undo=True)
        self.txt.grid(row=1, column=0, sticky="ew", pady=4)
        side = ttk.Frame(top)
        side.grid(row=1, column=1, sticky="n", padx=(8, 0), pady=4)
        ttk.Button(side, text="Open file...", command=self.open_file).pack(fill="x")
        ttk.Button(side, text="Clear", command=lambda: self.txt.delete("1.0", "end")).pack(fill="x", pady=(4, 0))
        row = ttk.Frame(top)
        row.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(row, text="Video links get the whole album", variable=self.expand_var).pack(side="left")
        ttk.Label(row, text="   Min score:").pack(side="left")
        ttk.Spinbox(row, from_=40, to=100, width=4, textvariable=self.thr_var).pack(side="left")
        ttk.Label(row, text="   Line order:").pack(side="left")
        ttk.Combobox(row, width=13, state="readonly", textvariable=self.order_var,
                     values=["auto", "artist-title", "title-artist"]).pack(side="left")
        self.btn_preview = ttk.Button(row, text="Find tracks", command=self.on_preview)
        self.btn_preview.pack(side="right")

        ft = ttk.Frame(nb, padding=8)
        nb.add(ft, text="Existing folder")
        ft.columnconfigure(1, weight=1)
        ttk.Label(ft, text="Look through a folder and everything under it for audio files that have no .lrc "
                           "next to them, and make word-synced lyrics for those.").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(ft, text="Folder:").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(ft, textvariable=self.scan_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(ft, text="Browse...", command=self.browse_scan).grid(row=1, column=2)
        ttk.Checkbutton(ft, text="Also redo .lrc files that only have line timing (the old one is kept as .lrc.bak)",
                        variable=self.upgrade_var).grid(row=2, column=0, columnspan=3, sticky="w")
        self.btn_scan = ttk.Button(ft, text="Scan folder", command=self.on_scan)
        self.btn_scan.grid(row=3, column=2, sticky="e", pady=(6, 0))

        mid = ttk.LabelFrame(self, text="Review (nothing is downloaded yet)", padding=8)
        mid.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        cols = ("sel", "artist", "length", "match", "url")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="extended")
        for c, text, width, anchor in (("#0", "Title", 360, "w"), ("sel", "Get", 50, "center"),
                                       ("artist", "Artist", 190, "w"), ("length", "Length", 60, "e"),
                                       ("match", "Match", 150, "w"), ("url", "Link", 320, "w")):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor=anchor, stretch=(c in ("#0", "url")))
        self.tree.tag_configure("off", foreground="#999999")
        self.tree.tag_configure("low", foreground="#b35c00")
        self.tree.tag_configure("album", font=("TkDefaultFont", 10, "bold"))
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<space>", self.on_space)

        bar = ttk.Frame(mid)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.review_btns = []
        for text, cmd in (("Check all", lambda: self.set_all(True)), ("Uncheck all", lambda: self.set_all(False)),
                          ("Open link", self.open_link), ("Change link...", self.change_link),
                          ("Copy links", self.copy_links), ("Remove", self.remove)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.review_btns.append(b)
        self.count_lbl = ttk.Label(bar, text="")
        self.count_lbl.pack(side="right")
        ttk.Label(mid, foreground="#666666",
                  text="Click the Get column to include or skip a track. Orange rows are weak matches; "
                       "double-click a link to open it.").grid(row=2, column=0, columnspan=2, sticky="w")

        run = ttk.LabelFrame(self, text="Download", padding=8)
        run.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        run.columnconfigure(1, weight=1)
        ttk.Label(run, text="Save to:").grid(row=0, column=0, sticky="w")
        ttk.Entry(run, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(run, text="Browse...", command=self.browse).grid(row=0, column=2)
        opts = ttk.Frame(run)
        opts.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        for label, var, vals, width in (("Format", self.fmt_var, ["mp3", "m4a", "opus", "flac"], 6),
                                        ("Lyrics for", self.lyrics_var, ["all", "requested", "none"], 10),
                                        ("Device", self.device_var, ["auto", "cpu", "cuda"], 6),
                                        ("Demucs model", self.model_var, ["htdemucs", "htdemucs_ft"], 12)):
            ttk.Label(opts, text=label + ":").pack(side="left", padx=(0, 2))
            ttk.Combobox(opts, width=width, state="readonly", textvariable=var, values=vals).pack(side="left", padx=(0, 10))
        ttk.Label(opts, text="Language (blank = detect):").pack(side="left")
        ttk.Entry(opts, width=5, textvariable=self.lang_var).pack(side="left", padx=(2, 10))
        ttk.Checkbutton(opts, text="Redo existing lyrics", variable=self.force_var).pack(side="left")
        opts2 = ttk.Frame(run)
        opts2.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Checkbutton(opts2, text="Transcribe the vocals when lrclib has no lyrics",
                        variable=self.transcribe_var).pack(side="left")
        ttk.Label(opts2, text="   Transcribe model:").pack(side="left", padx=(0, 2))
        ttk.Combobox(opts2, width=9, state="readonly", textvariable=self.tmodel_var,
                     values=["auto", "small", "medium", "large-v3"]).pack(side="left")
        r = ttk.Frame(run)
        r.grid(row=3, column=0, columnspan=3, sticky="ew")
        r.columnconfigure(0, weight=1)
        self.bar = SegmentedBar(r)
        self.bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.status = ttk.Label(r, text="Idle", width=48, anchor="w")
        self.status.grid(row=0, column=1)
        self.btn_cancel = ttk.Button(r, text="Cancel", command=self.on_cancel, state="disabled")
        self.btn_cancel.grid(row=0, column=2, padx=4)
        self.btn_start = ttk.Button(r, text="Start", command=self.on_start, state="disabled")
        self.btn_start.grid(row=0, column=3)

        lf = ttk.LabelFrame(self, text="Log", padding=4)
        lf.grid(row=3, column=0, sticky="nsew", padx=8, pady=(4, 8))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log_txt = tk.Text(lf, height=8, state="disabled", wrap="word")
        ls = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=ls.set)
        self.log_txt.grid(row=0, column=0, sticky="nsew")
        ls.grid(row=0, column=1, sticky="ns")

    def log(self, s):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", s + "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_preview.configure(state=state)
        self.btn_scan.configure(state=state)
        for b in self.review_btns:
            b.configure(state=state)
        self.btn_start.configure(state="disabled" if busy or not self.albums else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def open_file(self):
        path = filedialog.askopenfilename(title="Song list", filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if not path:
            return
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if core.looks_like_json(path, text):
            try:
                entries, skipped = core.parse_json_entries(text)
            except ValueError as e:
                messagebox.showerror("YAAAS", f"Couldn't read the json: {e}")
                return
            for e in entries:
                line = core.entry_line(e)
                self.imported[line] = e
                self.txt.insert("end", line + "\n")
            self.log(f"Imported {len(entries)} entries from {os.path.basename(path)}"
                     + (f" ({skipped} skipped, no title)" if skipped else ""))
        else:
            self.txt.insert("end", text.rstrip("\n") + "\n")

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or os.path.expanduser("~"))
        if d:
            self.out_var.set(d)

    def browse_scan(self):
        d = filedialog.askdirectory(initialdir=self.scan_var.get() or os.path.expanduser("~"))
        if d:
            self.scan_var.set(d)

    def on_scan(self):
        folder = self.scan_var.get().strip()
        if not folder:
            messagebox.showinfo("YAAAS", "Pick a folder first.")
            return
        upgrade = self.upgrade_var.get()
        self.populate([])
        self.status.configure(text="Scanning...")
        self.bar.set(0, 0, 0, 0)

        def work():
            self.q.put(("scan", core.scan_folder(folder, upgrade)))

        self.run_bg(work)

    def run_bg(self, fn):
        """Run fn on a worker thread; an 'idle' event always follows."""
        def wrapper():
            try:
                fn()
            except core.Cancelled:
                self.q.put(("log", "\nCancelled."))
            except Exception as e:
                self.q.put(("error", str(e)))
            finally:
                self.q.put(("idle", None))
        self.set_busy(True)
        threading.Thread(target=wrapper, daemon=True).start()

    def on_preview(self):
        lines = [l.strip() for l in self.txt.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            messagebox.showinfo("YAAAS", "Enter at least one song or link first.")
            return
        thr, expand, order = self.thr_var.get(), self.expand_var.get(), self.order_var.get()
        self.populate([])
        self.status.configure(text="Resolving...")
        self.bar.set(0, 0, 0, 0)

        def work():
            if self.yt is None:
                from ytmusicapi import YTMusic
                self.yt = YTMusic()
            items = [self.imported.get(l, l) for l in lines]   # imported lines keep their album info
            self.q.put(("plan", core.build_plan(self.yt, items, thr, expand, order)))

        self.run_bg(work)

    def populate(self, albums):
        self.albums = albums
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        for a in albums:
            label = (f"{a.artist} - " if a.artist else "") + a.title + (f" ({a.year})" if a.year else "")
            aid = self.tree.insert("", "end", text=label, open=True, tags=("album",),
                                   values=("", "", "", f"{len(a.tracks)} tracks", ""))
            self.items[aid] = ("album", a)
            for t in a.tracks:
                tid = self.tree.insert(aid, "end", text=t.title)
                self.items[tid] = ("track", t, a)
                self.refresh_track(tid)
            self.refresh_album(aid)
        self.refresh_count()

    def refresh_track(self, iid):
        t = self.items[iid][1]
        if t.score is not None:
            match = f"{t.score:.0f}%" + ("  (low)" if t.score < LOW_SCORE else "")
        else:
            match = t.source
        tags = []
        if not t.selected:
            tags.append("off")
        elif t.score is not None and t.score < LOW_SCORE:
            tags.append("low")
        self.tree.item(iid, text=(f"{t.number:02d}  " if t.number else "") + t.title, tags=tags,
                       values=("[x]" if t.selected else "[ ]", t.artist, fmt_len(t.duration), match, t.url))

    def refresh_album(self, aid):
        a = self.items[aid][1]
        n = sum(t.selected for t in a.tracks)
        self.tree.set(aid, "sel", "[x]" if n == len(a.tracks) else ("[ ]" if n == 0 else "[-]"))
        self.tree.set(aid, "match", f"{n}/{len(a.tracks)} selected")

    def refresh_all(self):
        for iid, it in self.items.items():
            if self.tree.exists(iid):
                (self.refresh_track if it[0] == "track" else self.refresh_album)(iid)
        self.refresh_count()

    def refresh_count(self):
        total = sum(len(a.tracks) for a in self.albums)
        sel = sum(t.selected for a in self.albums for t in a.tracks)
        self.count_lbl.configure(text=f"{sel} of {total} tracks selected")
        if not self.busy:
            self.btn_start.configure(state="normal" if sel else "disabled")

    def toggle(self, iid):
        it = self.items.get(iid)
        if not it:
            return
        if it[0] == "track":
            it[1].selected = not it[1].selected
        else:
            value = not all(t.selected for t in it[1].tracks)
            for t in it[1].tracks:
                t.selected = value
        self.refresh_all()

    def on_click(self, ev):
        if self.busy or self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        if self.tree.identify_column(ev.x) == "#1":
            iid = self.tree.identify_row(ev.y)
            if iid:
                self.toggle(iid)
                return "break"

    def on_double(self, ev):
        if self.tree.identify_region(ev.x, ev.y) == "cell" and self.tree.identify_column(ev.x) == "#5":
            self.open_link(self.tree.identify_row(ev.y))

    def on_space(self, ev):
        if not self.busy:
            for iid in self.tree.selection():
                self.toggle(iid)
        return "break"

    def set_all(self, value):
        for a in self.albums:
            for t in a.tracks:
                t.selected = value
        self.refresh_all()

    def selected_track(self, iid=None):
        iid = iid or (self.tree.selection() or [None])[0]
        it = self.items.get(iid) if iid else None
        return (iid, it[1], it[2]) if it and it[0] == "track" else (None, None, None)

    def open_link(self, iid=None):
        _, t, _ = self.selected_track(iid if isinstance(iid, str) else None)
        if t:
            webbrowser.open(t.url if t.url.startswith("http") else Path(t.url).as_uri())
        else:
            messagebox.showinfo("YAAAS", "Select a track first.")

    def copy_links(self):
        urls = [t.url for a in self.albums for t in a.tracks if t.selected]
        self.clipboard_clear()
        self.clipboard_append("\n".join(urls))
        self.log(f"Copied {len(urls)} link(s).")

    def remove(self):
        for iid in self.tree.selection():
            it = self.items.get(iid)
            if not it or not self.tree.exists(iid):
                continue
            if it[0] == "album":
                self.albums.remove(it[1])
                for c in self.tree.get_children(iid):
                    self.items.pop(c, None)
                self.tree.delete(iid)
                self.items.pop(iid, None)
            else:
                _, t, a = it
                a.tracks.remove(t)
                parent = self.tree.parent(iid)
                self.tree.delete(iid)
                self.items.pop(iid, None)
                if not a.tracks:
                    self.albums.remove(a)
                    self.tree.delete(parent)
                    self.items.pop(parent, None)
        self.refresh_all()

    def change_link(self):
        if self.mode == "folder":
            messagebox.showinfo("YAAAS", "Links only apply to downloads.")
            return
        iid, t, _ = self.selected_track()
        if not t:
            messagebox.showinfo("YAAAS", "Select a track first.")
            return
        url = simpledialog.askstring("Change link", f"Link to use instead of:\n{t.title}", parent=self)
        if not url:
            return
        kind = core.classify_url(url)
        if not kind or kind[0] != "video":
            messagebox.showerror("YAAAS", "That isn't a link to a single video.")
            return
        vid = kind[1]

        def work():
            self.q.put(("retrack", (iid, vid, core.video_meta(vid))))

        self.run_bg(work)

    def on_start(self):
        chosen = [t for a in self.albums for t in a.tracks if t.selected]
        if not chosen:
            return
        lang = self.lang_var.get().strip() or None
        self.cancel.clear()

        if self.mode == "folder":
            opts = core.Options(demucs_model=self.model_var.get(), language=lang,
                                device=self.device_var.get(), force=self.force_var.get(),
                                transcribe=self.transcribe_var.get(), transcribe_model=self.tmodel_var.get())
            n = len(chosen)
            self.bar.set(0, 0, 0, n, "sync")

            def step(done, total, title):
                self.q.put(("progress", (0, 0, done, total, "sync", title)))

            def work_folder():
                core.sync_many(chosen, opts, step, self.cancel)
                self.q.put(("progress", (0, 0, n, n, "done", "finished")))
                self.q.put(("log", "\nDone."))

            self.run_bg(work_folder)
            return

        out = self.out_var.get().strip()
        if not out:
            messagebox.showerror("YAAAS", "Pick an output folder.")
            return
        opts = core.Options(out=out, fmt=self.fmt_var.get(), lyrics=self.lyrics_var.get(),
                            demucs_model=self.model_var.get(), language=lang,
                            device=self.device_var.get(), force=self.force_var.get(),
                            transcribe=self.transcribe_var.get(), transcribe_model=self.tmodel_var.get())
        wants = {"all": lambda t: True, "requested": lambda t: t.requested, "none": lambda t: False}[opts.lyrics]
        self.bar.set(0, len(chosen), 0, sum(1 for t in chosen if wants(t)), "download")

        def work():
            core.execute(self.albums, opts, progress=lambda *a: self.q.put(("progress", a)), cancel=self.cancel)

        self.run_bg(work)

    def on_cancel(self):
        self.cancel.set()
        self.status.configure(text="Cancelling after this step...")

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "scan":
                    albums, seen, skipped = data
                    self.mode = "folder"
                    self.populate(albums)
                    count = sum(len(a.tracks) for a in albums)
                    self.log(f"\n{seen} audio file(s) found, {skipped} already have lyrics, {count} need them.")
                    if not count:
                        messagebox.showinfo("YAAAS", "Nothing to do. Every audio file already has a .lrc.")
                elif kind == "plan":
                    albums, unmatched = data
                    self.mode = "download"
                    self.populate(albums)
                    count = sum(len(a.tracks) for a in albums)
                    self.log(f"\nFound {count} track(s) in {len(albums)} album(s)/single(s).")
                    if unmatched:
                        self.log("Not resolved:\n  " + "\n  ".join(unmatched))
                        messagebox.showwarning("Some lines were not resolved", "\n".join(unmatched[:15]) +
                                               ("\n..." if len(unmatched) > 15 else ""))
                elif kind == "retrack":
                    iid, vid, meta = data
                    if iid in self.items and self.tree.exists(iid):
                        t = self.items[iid][1]
                        t.video_id, t.title, t.artist = vid, meta["title"], meta["artist"]
                        t.duration, t.score, t.source = meta["duration"], None, "manual link"
                        self.refresh_track(iid)
                        self.log(f"Link replaced: {t.artist} - {t.title}")
                elif kind == "progress":
                    dd, dt, sd, st, stage, text = data
                    self.bar.set(dd, dt, sd, st, stage)
                    prefix = {"download": "Downloading", "sync": "Syncing", "done": ""}[stage]
                    self.status.configure(text=(f"{prefix}: {text}" if prefix else text)[:60])
                elif kind == "error":
                    self.log("Error: " + data)
                    messagebox.showerror("YAAAS", data)
                elif kind == "idle":
                    if self.status.cget("text").startswith(("Resolving", "Cancelling")):
                        self.status.configure(text="Idle")
                    self.set_busy(False)
                    self.refresh_count()
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == "__main__":
    App().mainloop()
