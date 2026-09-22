"""Desktop interface for persistent, detached local builds."""
from __future__ import annotations

import datetime
import glob
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # drag and drop is optional
    DND_FILES = TkinterDnD = None

from jobs import (ROOT, SETTINGS, DEFAULT_PROJECTS, DEFAULT_MEDIA, DEFAULT_MODELS, read_json, write_json,
                   runs, start_job, stop_job, projects_dir, transfer_folder_contents,
                   read_queue, enqueue, dequeue_next, remove_from_queue)
from skeleton_builder import inspect_docx, preview_cues
from google_docs import is_google_doc_url, download_google_doc
from drive_audio import is_drive_url

PRESETS = {
    "YouTube 1080p Standard (16:9 @ 29.97 fps)": {"width": 1920, "height": 1080, "limit": 90, "handles": 10},
    "YouTube 4K Ultra HD (16:9 @ 29.97 fps)": {"width": 3840, "height": 2160, "limit": 90, "handles": 10},
    "YouTube Shorts / TikTok (9:16 Vertical)": {"width": 1080, "height": 1920, "limit": 30, "handles": 5},
    "Cinematic Longform (16:9 @ 24 fps)": {"width": 1920, "height": 1080, "limit": 120, "handles": 15},
}
WHISPER_MODELS = ["small.en", "medium.en", "large-v3-turbo", "distil-large-v3"]
UI_FONT = "Segoe UI" if os.name == "nt" else ("Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov"}

BG, PANEL, FIELD, FG, MUTED, ACCENT, BORDER = "#16181d", "#1e2128", "#262a33", "#e6e8ec", "#9aa1ad", "#4c8dff", "#333846"


def apply_dark_theme(window):
    style = ttk.Style(window)
    style.theme_use("clam")
    window.configure(bg=BG)
    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, troughcolor=PANEL, focuscolor=ACCENT, insertcolor=FG)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
    style.configure("TButton", background=FIELD, foreground=FG, bordercolor=BORDER, padding=(8, 4))
    style.map("TButton", background=[("active", "#323846"), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
    style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG)
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=FG)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
    style.configure("TCheckbutton", background=BG, foreground=FG, indicatorcolor=FIELD)
    style.map("TCheckbutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT)])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, bordercolor=BORDER)
    style.configure("Treeview.Heading", background=FIELD, foreground=FG, bordercolor=BORDER)
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, bordercolor=BORDER)
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(10, 6), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", FG)])
    window.option_add("*TCombobox*Listbox.background", FIELD)
    window.option_add("*TCombobox*Listbox.foreground", FG)
    window.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    window.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    if os.name == "nt":  # dark title bar
        try:
            import ctypes
            window.update()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass


def find_premiere():
    """Best-guess Premiere Pro program on this computer."""
    if os.name == "nt":
        hits = glob.glob(r"C:\Program Files\Adobe\Adobe Premiere Pro*\Adobe Premiere Pro.exe")
    elif sys.platform == "darwin":
        hits = glob.glob("/Applications/Adobe Premiere Pro*/Adobe Premiere Pro*.app")
    else:
        hits = []
    return sorted(hits)[-1] if hits else ""


def skeleton_xml(run):
    """Skeleton_full.xml of a run record from jobs.runs(), if it has been built."""
    xml = Path(run["result"]) / "Skeleton_full.xml" if run else None
    return xml if xml and xml.exists() else None


def main(smoke_test: bool = False):
    window = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    if smoke_test:
        window.withdraw()
    window.title("Premiere Pro Skeleton Builder")
    window.geometry("900x720")
    window.minsize(820, 640)
    window.configure(padx=16, pady=12)
    apply_dark_theme(window)
    if not smoke_test:
        window.lift()
        window.attributes("-topmost", True)
        window.after(300, lambda: window.attributes("-topmost", False))
        window.focus_force()

    saved = read_json(SETTINGS)
    selected = [None]
    known = {}
    last_log = [None]
    notified_runs = set()

    ttk.Label(window, text="Premiere Pro Skeleton Builder", font=(UI_FONT, 17, "bold")).pack(anchor="w")
    ttk.Label(window, text="Script + voiceover in, editable Premiere timeline out. Builds keep running if you close this window.",
              foreground=MUTED).pack(anchor="w", pady=(0, 8))

    tabs = ttk.Notebook(window)
    tabs.pack(fill="both", expand=True)
    build_tab = ttk.Frame(tabs, padding=14)
    runs_tab = ttk.Frame(tabs, padding=14)
    paths_tab = ttk.Frame(tabs, padding=14)
    tabs.add(build_tab, text="  Build  ")
    tabs.add(runs_tab, text="  Runs  ")
    tabs.add(paths_tab, text="  Paths & Options  ")

    # ---------------- Paths & Options tab (settings live here) ----------------
    settings_vars = {
        "projects_dir": tk.StringVar(value=saved.get("projects_dir") or str(DEFAULT_PROJECTS)),
        "media_dir": tk.StringVar(value=saved.get("media_dir") or str(DEFAULT_MEDIA)),
        "models_dir": tk.StringVar(value=saved.get("models_dir") or str(DEFAULT_MODELS)),
        "premiere_exe": tk.StringVar(value=saved.get("premiere_exe") or find_premiere()),
        "videos": tk.BooleanVar(value=saved.get("videos", True)),
        "limit_minutes": tk.StringVar(value=str(saved.get("limit_minutes", 90))),
        "buffer_minutes": tk.StringVar(value=str(saved.get("buffer_minutes", 10))),
        "width": tk.StringVar(value=str(saved.get("width", 1920))),
        "height": tk.StringVar(value=str(saved.get("height", 1080))),
        "whisper_model": tk.StringVar(value=saved.get("whisper_model", "small.en")),
        "device": tk.StringVar(value=saved.get("device", "auto")),
        "preset": tk.StringVar(value=saved.get("preset", list(PRESETS)[0])),
    }

    def save_settings(*_):
        write_json(SETTINGS, {k: v.get() for k, v in settings_vars.items()})

    for var in settings_vars.values():
        var.trace_add("write", save_settings)
    for key in ("projects_dir", "media_dir", "models_dir"):  # make sure the default locations exist
        try:
            Path(settings_vars[key].get()).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    save_settings()

    # Folders whose contents can be offered a move when the user points them somewhere new
    # (Projects/Media/Models). The Premiere program path is not one of these.
    FOLDER_LABELS = {"projects_dir": "projects", "media_dir": "media", "models_dir": "speech model"}
    committed_locations = {key: settings_vars[key].get().strip() for key in FOLDER_LABELS}

    def offer_transfer(key, old, new):
        """Ask whether to move existing files from the old folder to the new one. Returns
        True to accept the new location (with or without moving files), False to keep the old one."""
        label = FOLDER_LABELS[key]
        has_contents = Path(old).is_dir() and any(Path(old).iterdir())
        if not has_contents:
            return True
        choice = messagebox.askyesnocancel(
            "Move existing files?",
            f"You changed the {label} folder:\n\nFrom: {old}\nTo: {new}\n\n"
            f"Move the existing {label} files there now?\n\n"
            "Yes — move them into the new folder.\n"
            "No — leave them where they are and start fresh at the new folder.\n"
            "Cancel — keep using the previous folder.\n\n"
            "Note: runs already built keep the file paths they were built with, so a "
            "finished Premiere timeline may need re-linking in Premiere if its media moves."
        )
        if choice is None:
            settings_vars[key].set(old)
            return False
        if choice:
            errors = transfer_folder_contents(old, new)
            if errors:
                messagebox.showwarning("Some items were not moved",
                                        "\n".join(errors[:10]) + ("\n…" if len(errors) > 10 else ""))
        return True

    def commit_location(key):
        def handler(_e=None):
            new = settings_vars[key].get().strip()
            old = committed_locations[key]
            if not new or new == old:
                return
            try:
                Path(new).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                messagebox.showerror("Cannot use that folder", str(error))
                settings_vars[key].set(old)
                return
            if offer_transfer(key, old, new):
                committed_locations[key] = new
                refresh()  # the Runs list re-scans the (possibly new) projects folder
        return handler

    def pick_folder(key, title):
        def choose_folder():
            path = filedialog.askdirectory(title=title, initialdir=settings_vars[key].get() or str(ROOT))
            if path:
                settings_vars[key].set(path)
                commit_location(key)()
        return choose_folder

    def pick_program():
        path = filedialog.askopenfilename(title="Choose Adobe Premiere Pro",
                                          filetypes=[("Program", "*.exe *.app"), ("All files", "*.*")])
        if path:
            settings_vars["premiere_exe"].set(path)

    paths_group = ttk.LabelFrame(paths_tab, text=" Locations ", padding=12)
    paths_group.pack(fill="x")
    paths_group.columnconfigure(1, weight=1)
    location_rows = [
        ("Projects folder", "projects_dir", pick_folder("projects_dir", "Choose projects folder"),
         "Each run gets its own sub-folder here. Created automatically if missing."),
        ("Media download folder", "media_dir", pick_folder("media_dir", "Choose media download folder"),
         "Images and downloaded video clips, one sub-folder per run. Created automatically if missing."),
        ("Speech model folder", "models_dir", pick_folder("models_dir", "Choose speech model folder"),
         "faster-whisper downloads the chosen model here the first time it is used (0.5–1.6 GB), then reuses it."),
        ("Premiere Pro program", "premiere_exe", pick_program,
         "Used by 'Run Premiere Pro with Skeleton'. Detected automatically when possible."),
    ]
    for r, (label, key, command, hint) in enumerate(location_rows):
        ttk.Label(paths_group, text=label, font=(UI_FONT, 10, "bold")).grid(row=r * 2, column=0, sticky="w", pady=(6, 0))
        entry = ttk.Entry(paths_group, textvariable=settings_vars[key])
        entry.grid(row=r * 2, column=1, sticky="ew", padx=8, pady=(6, 0))
        if key in FOLDER_LABELS:
            entry.bind("<Return>", commit_location(key))
            entry.bind("<FocusOut>", commit_location(key))
        ttk.Button(paths_group, text="Browse…", command=command).grid(row=r * 2, column=2, pady=(6, 0))
        ttk.Label(paths_group, text=hint, foreground=MUTED, font=(UI_FONT, 9)).grid(row=r * 2 + 1, column=1, sticky="w", padx=8)

    options_group = ttk.LabelFrame(paths_tab, text=" Options ", padding=12)
    options_group.pack(fill="x", pady=(12, 0))
    ttk.Checkbutton(options_group, text="Download linked videos (YouTube, other sites, direct files, Drive) and keep extra handles",
                    variable=settings_vars["videos"]).grid(row=0, column=0, columnspan=6, sticky="w")

    def labelled(row, col, text, var, width, unit=""):
        ttk.Label(options_group, text=text).grid(row=row, column=col, sticky="w", pady=6, padx=(0, 6))
        ttk.Entry(options_group, textvariable=var, width=width).grid(row=row, column=col + 1, sticky="w")
        if unit:
            ttk.Label(options_group, text=unit, foreground=MUTED).grid(row=row, column=col + 2, sticky="w", padx=(4, 18))

    labelled(1, 0, "Download whole videos up to", settings_vars["limit_minutes"], 6, "min")
    labelled(1, 3, "Handles per side", settings_vars["buffer_minutes"], 6, "min")
    labelled(2, 0, "Resolution", settings_vars["width"], 6, "×")
    ttk.Entry(options_group, textvariable=settings_vars["height"], width=6).grid(row=2, column=3, sticky="w")
    ttk.Label(options_group, text="Speech model").grid(row=3, column=0, sticky="w", pady=6)
    ttk.Combobox(options_group, textvariable=settings_vars["whisper_model"], values=WHISPER_MODELS, width=18).grid(row=3, column=1, sticky="w")
    ttk.Label(options_group, text="Runs on").grid(row=3, column=3, sticky="w", padx=(0, 6))
    ttk.Combobox(options_group, textvariable=settings_vars["device"], values=["auto", "cpu", "cuda"], state="readonly", width=8).grid(row=3, column=4, sticky="w")
    ttk.Label(options_group, text="Speech recognition runs on your computer (faster-whisper). 'auto' uses the graphics card when available.",
              foreground=MUTED, font=(UI_FONT, 9)).grid(row=4, column=0, columnspan=6, sticky="w", pady=(6, 0))

    # ---------------- Build tab ----------------
    preset_row = ttk.Frame(build_tab)
    preset_row.pack(fill="x", pady=(0, 10))
    ttk.Label(preset_row, text="Channel / preset", font=(UI_FONT, 10, "bold")).pack(side="left")
    preset_combo = ttk.Combobox(preset_row, textvariable=settings_vars["preset"], values=list(PRESETS), state="readonly", width=40)
    preset_combo.pack(side="left", padx=8)

    def on_preset_change(_=None):
        chosen = PRESETS.get(settings_vars["preset"].get())
        if chosen:
            settings_vars["width"].set(str(chosen["width"]))
            settings_vars["height"].set(str(chosen["height"]))
            settings_vars["limit_minutes"].set(str(chosen["limit"]))
            settings_vars["buffer_minutes"].set(str(chosen["handles"]))

    preset_combo.bind("<<ComboboxSelected>>", on_preset_change)

    inputs_group = ttk.LabelFrame(build_tab, text=" Inputs ", padding=12)
    inputs_group.pack(fill="x")
    inputs_group.columnconfigure(1, weight=1)
    script_var, audio_var = tk.StringVar(), tk.StringVar()
    inspect_status = tk.StringVar(value="Paste a Google Doc link or choose a .docx script.")

    def status(text):
        window.after(0, lambda: inspect_status.set(text))

    resolving = [False]

    def resolve_script(source=None, quiet=True, update_var=True):
        """Download a Google Doc link (if given), then check the script. Returns the local path or None.

        Blocking: does network I/O. Only call this off the UI thread (see resolve_script_async).
        `source` defaults to the Script field's value; pass it explicitly (e.g. to retry a past
        run's original link) without touching the Build tab's own field by also passing
        update_var=False.
        """
        value = (source if source is not None else script_var.get()).strip()
        if not value:
            return None
        if is_google_doc_url(value):
            status("⏳ Downloading Google Doc…")
            try:
                value = str(download_google_doc(value, progress=lambda m: status(f"⏳ {m}")))
            except Exception as err:
                status(f"❌ Google Doc error: {err}")
                if not quiet:
                    window.after(0, lambda: messagebox.showerror("Google Doc Error", str(err)))
                return None
            if update_var:
                window.after(0, lambda: script_var.set(value))
        if not Path(value).is_file():
            status(f"❌ Script file not found: {value}")
            return None
        result = inspect_docx(value)
        if not result.get("valid"):
            status(f"❌ Script check failed: {result.get('error')}")
            return None
        note = f"✓ {Path(value).stem}: {result['image_cues']} image cue(s), {result['video_cues']} video cue(s), {result['word_count']} words."
        if result.get("warnings"):
            note += f" {len(result['warnings'])} warning(s): {result['warnings'][0]}"
        status(note)
        return value

    def resolve_script_async(source=None, quiet=True, then=None, update_var=True):
        """Non-blocking wrapper: runs resolve_script() off the UI thread so a Google Doc
        download (or a slow/unreachable link) never freezes the window."""
        if resolving[0]:
            return  # a resolve is already in flight; avoid piling up overlapping downloads
        resolving[0] = True
        script_entry.configure(state="disabled")

        def worker():
            try:
                result = resolve_script(source=source, quiet=quiet, update_var=update_var)
            finally:
                def done():
                    resolving[0] = False
                    script_entry.configure(state="normal")
                    if then:
                        then(result)
                window.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def add_input(row, label, var, hint, extensions, on_change=None):
        ttk.Label(inputs_group, text=label, font=(UI_FONT, 10, "bold")).grid(row=row * 2, column=0, sticky="w", pady=(4, 0))
        entry = ttk.Entry(inputs_group, textvariable=var)
        entry.grid(row=row * 2, column=1, sticky="ew", padx=8, pady=(4, 0))

        def browse():
            path = filedialog.askopenfilename(title=f"Choose {label}", filetypes=[(label, extensions), ("All files", "*.*")])
            if path:
                var.set(path)
                if on_change:
                    on_change()

        ttk.Button(inputs_group, text="Browse…", command=browse).grid(row=row * 2, column=2, pady=(4, 0))
        ttk.Label(inputs_group, text=hint, foreground=MUTED, font=(UI_FONT, 9)).grid(row=row * 2 + 1, column=1, sticky="w", padx=8)
        if on_change:
            entry.bind("<Return>", lambda _e: on_change())
            entry.bind("<FocusOut>", lambda _e: on_change())
        return entry

    script_entry = add_input(0, "Script", script_var, "Google Doc link (must be viewable by anyone with the link) or a .docx file.", "*.docx", resolve_script_async)
    audio_entry = add_input(1, "Voiceover", audio_var, "Drop an audio file here, paste a Google Drive link, or browse.", "*.mp3 *.wav *.m4a *.aac *.flac")
    ttk.Label(inputs_group, textvariable=inspect_status, wraplength=780, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def on_drop(event):
        for raw in window.tk.splitlist(event.data):
            ext = Path(raw).suffix.lower()
            if ext == ".docx":
                script_var.set(raw)
                resolve_script_async()
            elif ext in AUDIO_EXT:
                audio_var.set(raw)
        return event.action

    if TkinterDnD:
        for widget in (build_tab, inputs_group, script_entry, audio_entry):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", on_drop)

    build_frame = ttk.Frame(build_tab)
    build_frame.pack(fill="x", pady=(14, 0))
    progress_bar = ttk.Progressbar(build_frame, mode="indeterminate")
    phase_label = ttk.Label(build_frame, text="", foreground=MUTED)

    def open_path(path):
        try:
            p = Path(path)
            if not p.exists():
                raise ValueError("This folder or file has not been created yet.")
            if sys.platform == "darwin":
                subprocess.run(["open", str(p)], check=True)
            elif os.name == "nt":
                os.startfile(str(p))
            else:
                subprocess.run(["xdg-open", str(p)], check=True)
        except Exception as error:
            messagebox.showerror("Could not open", str(error))

    def launch():
        # resolve_script_async does the (network) Google Doc download off the UI thread so a
        # slow/unreachable link can't freeze the window; finish_launch runs back on the UI thread.
        build_btn.configure(state="disabled")

        def finish_launch(docx):
            try:
                if not docx:
                    raise ValueError("Choose a script first (.docx or Google Doc link).")
                audio = audio_var.get().strip()
                if not audio or not (is_drive_url(audio) or Path(audio).is_file()):
                    raise ValueError("Choose the voiceover: drop an audio file or paste a Google Drive link.")
                width, height = int(settings_vars["width"].get()), int(settings_vars["height"].get())
                limit_minutes, buffer_minutes = float(settings_vars["limit_minutes"].get()), float(settings_vars["buffer_minutes"].get())
                if width <= 0 or height <= 0:
                    raise ValueError("Width and height must be positive numbers.")
                if not 1 <= limit_minutes <= 1440 or not 0 <= buffer_minutes <= 60:
                    raise ValueError("Use a limit of 1–1440 minutes and handles of 0–60 minutes.")
                root = projects_dir({"projects_dir": settings_vars["projects_dir"].get().strip()})
                config = {
                    "docx": docx, "docx_source": script_var.get().strip(), "audio": audio, "title": Path(docx).stem,
                    "projects_dir": str(root),
                    "media_dir": settings_vars["media_dir"].get().strip(),
                    "videos": settings_vars["videos"].get(), "limit_minutes": limit_minutes, "buffer_minutes": buffer_minutes,
                    "width": width, "height": height, "preset": settings_vars["preset"].get(),
                    "whisper_model": settings_vars["whisper_model"].get().strip() or "small.en",
                    "device": settings_vars["device"].get(),
                    "models_dir": settings_vars["models_dir"].get().strip(),
                }
                busy = any(r["display_status"] in ("Running", "Starting") for r in runs(root))
                if busy:
                    enqueue(config)
                    render_queue()
                    messagebox.showinfo("Added to queue",
                                        f'A build is already running — "{config["title"]}" will start automatically once it finishes.')
                else:
                    folder = start_job(config)
                    selected[0] = str(folder)
                refresh()
            except Exception as error:
                traceback.print_exc()
                messagebox.showerror("Cannot start run", str(error))
            finally:
                build_btn.configure(state="normal")

        resolve_script_async(quiet=False, then=finish_launch)

    def render_preview(title, cues, warnings):
        top = tk.Toplevel(window)
        top.title(f"Cue preview — {title}")
        top.geometry("820x520")
        top.configure(bg=BG)
        ttk.Label(top, text=f"{len(cues)} cue(s) matched — no download or transcription was run.",
                  foreground=MUTED, background=BG).pack(anchor="w", padx=12, pady=(10, 4))

        tree = ttk.Treeview(top, columns=("kind", "target"), height=16)
        tree.heading("#0", text="Script passage")
        tree.heading("kind", text="Kind")
        tree.heading("target", text="Target")
        tree.column("#0", width=420)
        tree.column("kind", width=70, anchor="center")
        tree.column("target", width=280)
        tree.pack(fill="both", expand=True, padx=12)
        for i, cue in enumerate(cues):
            tree.insert("", "end", iid=str(i), text=cue["passage"],
                        values=(cue["kind"], "; ".join(cue["targets"]) or (cue["asset_path"] or "")))

        if warnings:
            ttk.Label(top, text=f"{len(warnings)} warning(s):", foreground=MUTED, background=BG).pack(
                anchor="w", padx=12, pady=(10, 0))
            box = tk.Text(top, height=5, wrap="word", bg=PANEL, fg=FG, insertbackground=FG,
                          relief="flat", highlightthickness=1, highlightbackground=BORDER)
            box.pack(fill="x", padx=12, pady=(2, 12))
            box.insert("end", "\n".join(warnings))
            box.configure(state="disabled")
        else:
            ttk.Frame(top, height=12, style="TFrame").pack()

    def preview():
        preview_btn.configure(state="disabled")

        def after_resolve(docx):
            if not docx:
                preview_btn.configure(state="normal")
                return  # resolve_script_async already showed the error (quiet=False)

            def worker():
                try:
                    cues, warnings = preview_cues(docx)
                    window.after(0, lambda: render_preview(Path(docx).stem, cues, warnings))
                except Exception as error:
                    traceback.print_exc()
                    window.after(0, lambda: messagebox.showerror("Cannot preview", str(error)))
                finally:
                    window.after(0, lambda: preview_btn.configure(state="normal"))

            threading.Thread(target=worker, daemon=True).start()

        resolve_script_async(quiet=False, then=after_resolve)

    build_btn = ttk.Button(build_frame, text="⚡  Build Skeleton", command=launch, padding=(14, 7))
    build_btn.pack(side="left")
    preview_btn = ttk.Button(build_frame, text="👁  Preview cues", command=preview, padding=(10, 7))
    preview_btn.pack(side="left", padx=(8, 0))

    def stop_current_run():
        active = next((r for r in runs(settings_vars["projects_dir"].get().strip() or None)
                       if r["display_status"] in ("Running", "Starting")), None)
        if not active:
            return
        if messagebox.askyesno("Stop build?",
                               f'Stop "{Path(active["folder"]).name}"? Partial output stays on disk, '
                               'but this run cannot be resumed — you would need to retry it.'):
            stop_job(active["folder"])
            refresh()

    stop_btn = ttk.Button(build_frame, text="⏹  Stop", command=stop_current_run, padding=(10, 7))
    stop_btn.pack(side="left", padx=(8, 0))
    phase_label.pack(side="left", padx=14)
    progress_bar.pack(side="right", fill="x", expand=True)

    # ---------------- Queue: extra submissions while a build is already running ----------------
    queue_group = ttk.LabelFrame(build_tab, text=" Queue ", padding=12)
    queue_group.pack(fill="x", pady=(14, 0))
    queue_list = tk.Listbox(queue_group, height=3, bg=PANEL, fg=FG, selectbackground=ACCENT,
                            selectforeground="#ffffff", relief="flat", highlightthickness=1,
                            highlightbackground=BORDER, activestyle="none")
    queue_list.pack(side="left", fill="x", expand=True)
    queue_ids = []

    def remove_selected_queue_item():
        selection = queue_list.curselection()
        if not selection or selection[0] >= len(queue_ids):
            return
        remove_from_queue(queue_ids[selection[0]])
        render_queue()

    ttk.Button(queue_group, text="Remove", command=remove_selected_queue_item).pack(side="left", padx=(8, 0), anchor="n")

    def render_queue():
        items = read_queue()
        queue_ids[:] = [i["id"] for i in items]
        queue_list.delete(0, "end")
        for i in items:
            queue_list.insert("end", i.get("title") or "Untitled")
        queue_group.configure(text=f" Queue ({len(items)} waiting) " if items else " Queue (empty — builds run immediately) ")

    render_queue()

    current_var = tk.StringVar(value="No run yet.")
    result_group = ttk.LabelFrame(build_tab, text=" Latest / selected run ", padding=12)
    result_group.pack(fill="x", pady=(14, 0))
    ttk.Label(result_group, textvariable=current_var, wraplength=780, justify="left").pack(anchor="w", pady=(0, 8))

    def chosen_run():
        return known.get(selected[0]) or next(iter(known.values()), None)

    def run_premiere():
        # Premiere Pro has no supported way to open/import an FCP7 XML from outside the
        # app — its executable only recognizes a .prproj on the command line, so passing
        # the XML path is silently ignored and Premiere just shows its normal startup
        # screen. There's no reliable unattended fix short of building and installing a
        # Premiere CEP/UXP scripting extension, so the real workflow is: launch Premiere
        # as a convenience, copy the path, and tell the user to paste it into Import.
        xml = skeleton_xml(chosen_run())
        if not xml:
            messagebox.showwarning("Not ready", "This run has no Skeleton_full.xml yet.")
            return
        window.clipboard_clear()
        window.clipboard_append(str(xml))
        program = settings_vars["premiere_exe"].get().strip() or find_premiere()
        opened = False
        if program:
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-a", program])
                else:
                    subprocess.Popen([program])
                opened = True
            except Exception:
                opened = False
        messagebox.showinfo(
            "Copy Skeleton Path",
            ("Premiere Pro is opening. " if opened else "Couldn't launch Premiere Pro automatically — "
             "open it yourself, or set its location in Paths & Options. ") +
            "Premiere has no way to import an XML automatically from outside the app, so finish it "
            "manually:\n\n"
            "1. In Premiere, press Ctrl+I (Cmd+I on Mac), or File > Import.\n"
            "2. Paste the path (already on your clipboard) into the filename box and press Enter.\n\n"
            f"Path: {xml}")

    def open_project():
        run = chosen_run()
        if run:
            open_path(run["folder"])

    def retry_run():
        run = chosen_run()
        cfg = dict((run or {}).get("config") or {})
        if not cfg:
            messagebox.showwarning("Cannot retry", "This run has no saved configuration to retry.")
            return
        retry_btn.configure(state="disabled")
        source = cfg.get("docx_source") or cfg.get("docx")

        def after_resolve(docx_path):
            try:
                if not docx_path:
                    raise ValueError("Could not fetch the script again — see the status message above.")
                folder = start_job({**cfg, "docx": docx_path})
                selected[0] = str(folder)
                refresh()
            except Exception as error:
                traceback.print_exc()
                messagebox.showerror("Cannot retry run", str(error))
            finally:
                retry_btn.configure(state="normal")

        # Re-downloads the script (if it was a Google Doc link) and, via start_job below,
        # re-stages the voiceover too (re-downloads it if it was a Google Drive link) —
        # without touching whatever the Build tab currently has typed in.
        resolve_script_async(source=source, quiet=False, then=after_resolve, update_var=False)

    button_row = ttk.Frame(result_group)
    button_row.pack(fill="x")
    premiere_btn = ttk.Button(button_row, text="📋  Copy Skeleton Path & Open Premiere", command=run_premiere, padding=(10, 5))
    premiere_btn.pack(side="left", padx=(0, 8))
    ttk.Button(button_row, text="Open project folder", command=open_project, padding=(10, 5)).pack(side="left", padx=(0, 8))
    retry_btn = ttk.Button(button_row, text="⟳  Retry script & audio fetch", command=retry_run, padding=(10, 5))
    retry_btn.pack(side="left")

    # ---------------- Runs tab ----------------
    tree = ttk.Treeview(runs_tab, columns=("status", "date"), height=6)
    tree.heading("#0", text="Project")
    tree.heading("status", text="Status")
    tree.heading("date", text="Created")
    tree.column("#0", width=380)
    tree.column("status", width=200)
    tree.column("date", width=140)
    tree.pack(fill="x", pady=(0, 8))
    ttk.Label(runs_tab, text="Log", foreground=MUTED).pack(anchor="w")
    log_box = tk.Text(runs_tab, height=12, wrap="word", font=("Consolas" if os.name == "nt" else "Menlo", 9), state="disabled",
                      bg=PANEL, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=BORDER)
    log_box.pack(fill="both", expand=True)

    def choose(_=None):
        if tree.selection() and tree.selection()[0] != selected[0]:
            selected[0] = tree.selection()[0]
            last_log[0] = None
            refresh()

    tree.bind("<<TreeviewSelect>>", choose)

    PHASES = [
        (r"Downloading voiceover", "Step 1/5: Fetching voiceover from Google Drive…"),
        (r"Loading .* locally|Timed through|Using cached word", "Step 2/5: Transcribing voiceover locally…"),
        (r"Extracting bookmarked", "Step 3/5: Reading script & extracting images…"),
        (r"Downloading (?!voiceover)|Preparing", "Step 4/5: Fetching images & video clips…"),
        (r"validate_xml|Built ", "Step 5/5: Writing Premiere timeline…"),
    ]

    MODEL_DL_RE = re.compile(r"MODEL_DL (\d+) (\d+) (\d+)")

    def phase_of(text):
        """Return (label, download_percent). download_percent is None unless the speech
        model download is the most recent event, in which case the progress bar switches
        to a determinate percentage instead of the usual indeterminate spinner."""
        best, label = -1, "Working…"
        for pattern, name in PHASES:
            hits = [m.start() for m in re.finditer(pattern, text)]
            if hits and hits[-1] > best:
                best, label = hits[-1], name
        dl_hits = list(MODEL_DL_RE.finditer(text))
        if dl_hits and dl_hits[-1].start() > best:
            pct, done, total = dl_hits[-1].groups()
            mb = lambda b: f"{int(b) / 1_000_000:.0f} MB"
            return f"Step 2/5: Downloading speech model — {pct}% ({mb(done)} / {mb(total)})…", int(pct)
        return label, None

    def refresh():
        current = runs(settings_vars["projects_dir"].get().strip() or None)
        known.clear()
        known.update({r["folder"]: r for r in current})
        for iid in tree.get_children():
            if iid not in known:
                tree.delete(iid)
        for r in current:
            created = datetime.datetime.fromtimestamp(r["created"]).strftime("%Y-%m-%d %H:%M") if r.get("created") else ""
            args = {"text": Path(r["folder"]).name, "values": (r["display_status"], created)}
            if tree.exists(r["folder"]):
                tree.item(r["folder"], **args)
            else:
                tree.insert("", "end", iid=r["folder"], **args)

        running = any(r["display_status"] in ("Running", "Starting") for r in current)
        if not running:
            entry = dequeue_next()
            if entry:
                try:
                    folder = start_job(entry["config"])
                    selected[0] = str(folder)
                    running = True
                except Exception as error:
                    traceback.print_exc()
                    messagebox.showerror("Cannot start queued run", f'{entry.get("title") or "Untitled"}: {error}')
                render_queue()
        # A busy run no longer blocks Build — a new submission is queued and starts
        # automatically once the current one finishes (see the queue-advance above).
        build_btn.configure(state="disabled" if resolving[0] else "normal")
        preview_btn.configure(state="disabled" if resolving[0] else "normal")
        stop_btn.configure(state="normal" if running else "disabled")
        if not selected[0] and current:
            selected[0] = next((r["folder"] for r in current if r["display_status"] == "Running"), current[0]["folder"])
        if selected[0] and tree.exists(selected[0]) and tree.selection() != (selected[0],):
            tree.selection_set(selected[0])

        r = known.get(selected[0])
        premiere_btn.configure(state="normal" if skeleton_xml(r) else "disabled")
        retry_status = ("Failed — see log", "Interrupted / incomplete")
        can_retry = bool(r) and not running and not resolving[0] and r["display_status"] in retry_status and r.get("config")
        retry_btn.configure(state="normal" if can_retry else "disabled")
        if not r:
            return
        log_path = Path(r["folder"]) / "run.log"
        text = "No saved log is available for this run."
        if log_path.exists():
            with log_path.open("rb") as f:
                f.seek(max(0, log_path.stat().st_size - 40000))
                text = f.read().decode("utf-8", errors="replace")
        d_status = r["display_status"]
        current_var.set(f"{Path(r['folder']).name} — {d_status}")

        if d_status in ("Running", "Starting"):
            label, dl_pct = phase_of(text)
            if dl_pct is not None:
                progress_bar.stop()
                progress_bar.configure(mode="determinate")
                progress_bar["value"] = dl_pct
            else:
                progress_bar.configure(mode="indeterminate")
                progress_bar.start(10)
            phase_label.config(text=label)
        else:
            progress_bar.stop()
            progress_bar.configure(mode="determinate")
            done = d_status.startswith("Completed")
            progress_bar["value"] = 100 if done else 0
            phase_label.config(text="✓ Done — ready for Premiere Pro." if done else ("❌ Build failed. See the Runs tab." if "Failed" in d_status else ""))
        if d_status not in ("Running", "Starting") and r["folder"] not in notified_runs:
            notified_runs.add(r["folder"])
            window.bell()
        if last_log[0] != text:
            log_box.configure(state="normal")
            log_box.delete("1.0", "end")
            log_box.insert("end", text)
            log_box.see("end")
            log_box.configure(state="disabled")
            last_log[0] = text

    def poll():
        try:
            if not window.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            refresh()
        except Exception as error:
            try:
                current_var.set(f"Status refresh error: {error}")
            except tk.TclError:
                return
        window.after(1500, poll)

    def close():
        if any(r["display_status"] in ("Running", "Starting") for r in known.values()):
            if not messagebox.askokcancel(
                "Keep working in background?",
                "The build will continue in the background. Reopen Skeleton Builder any time to check progress.\n\nClose window?",
            ):
                return
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", close)
    poll()
    if smoke_test:
        window.after(250, window.destroy)
    window.mainloop()


if __name__ == "__main__":
    main()
