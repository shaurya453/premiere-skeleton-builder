"""Desktop interface for persistent, detached local builds."""
from __future__ import annotations

import datetime
import glob
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # drag and drop is optional
    DND_FILES = TkinterDnD = None

from jobs import (ROOT, SETTINGS, DEFAULT_PROJECTS, DEFAULT_MEDIA, DEFAULT_MODELS, read_json, write_json,
                   runs, start_job, stop_job, projects_dir, transfer_folder_contents,
                   read_queue, enqueue, dequeue_next, remove_from_queue)
from skeleton_builder import case_range_for, inspect_docx, preview_cues
from google_docs import is_google_doc_url, download_google_doc, missing_tab_warning
from drive_audio import is_drive_url
from paths import FROZEN
from system_clipboard import copy_text
import updater

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
WARN_FG = "#e2a03f"


def enable_dpi_awareness():
    """Windows only: opt in to real pixels so text is crisp on scaled displays instead of being
    bitmap-stretched by the OS. Must run before the first Tk window exists (Tk reads the screen's
    DPI when it initialises). Harmless if it was already set."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def ui_scale(window):
    """1.0 at 96 dpi, 1.5 at 150% and so on (follows Tk's own scaling)."""
    try:
        return max(0.75, window.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


def work_area(window):
    """(x, y, width, height) of the usable desktop: the Windows work area excludes the taskbar."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def place_window(window, scale):
    """Initial size from the screen (never more than ~92% of it, so it also fits a 1366x768
    laptop), a minimum small enough for that screen, centred on the usable desktop."""
    ax, ay, aw, ah = work_area(window)
    width = min(round(920 * scale), int(aw * 0.92))
    height = min(round(780 * scale), int(ah * 0.92))
    window.minsize(min(round(740 * scale), width), min(round(520 * scale), height))
    x = ax + max(0, (aw - width) // 2)
    y = ay + max(0, (ah - height - round(40 * scale)) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def wheel_units(event, notch=3, window_system=None):
    """Signed scroll distance (in canvas/list units, positive = down) for a mouse-wheel event.
    Windows/Linux-wheel: one notch is +-120 delta (or Button-4/5 on X11) and scrolls `notch` units;
    macOS reports small deltas (1, 2, 3...) that are already a distance. Never 0 for a real event."""
    num = getattr(event, "num", 0)
    if num in (4, 5):
        return -notch if num == 4 else notch
    delta = getattr(event, "delta", 0)
    if not delta:
        return 0
    if window_system == "aqua":
        steps = -delta
    else:
        notches = max(1, abs(delta) // 120)  # touchpads send partial notches: still move
        steps = (-notches if delta > 0 else notches) * notch
    return steps


def _can_scroll(widget, units):
    lo, hi = widget.yview()
    return not ((units < 0 and lo <= 0.0001) or (units > 0 and hi >= 0.9999))


def route_wheel(root, event, under=None):
    """One wheel handler for the whole app. Finds what is under the pointer and scrolls the
    innermost scrollable *page* (a make_scrollable canvas) that can still move that way, so the
    wheel works over any child widget and a list at its end hands off to the page around it.
    Widgets that scroll themselves (Text, Listbox, Treeview) are left to Tk unless their content
    fits entirely, in which case the wheel passes through to the page. Returns True if it scrolled."""
    if event.state & 0x1:  # Shift+wheel is horizontal: not ours
        return False
    try:
        widget = under if under is not None else root.winfo_containing(event.x_root, event.y_root)  # `under`: for tests
    except (KeyError, tk.TclError):
        return False
    system = root.tk.call("tk", "windowingsystem")
    while widget is not None:
        if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview)):
            lo, hi = widget.yview()
            if lo > 0.0 or hi < 1.0:
                return False  # scrolls itself
        page = getattr(widget, "_wheel_canvas", None)
        if page is not None:
            units = wheel_units(event, page._wheel_notch, system)
            if units and _can_scroll(page, units):
                page.yview_scroll(units, "units")
                return True
        widget = widget.master
    return False


def reveal_focus(event):
    """Keep the focused widget on screen: Tab-ing into something below the fold of a scrolling page
    (or of the case list) scrolls it into view."""
    widget = event.widget
    if not isinstance(widget, tk.Misc):
        return
    try:
        height = widget.winfo_height()
        node = widget.master
        while node is not None:
            if getattr(node, "_wheel_canvas", None) is node:  # a scrolling canvas (not its frame)
                view = node.winfo_height()
                step = max(1, node._wheel_increment)
                top = widget.winfo_rooty() - node.winfo_rooty()
                if 1 < height <= view and (top < 0 or top + height > view):
                    if top < 0:
                        node.yview_scroll(top // step, "units")  # floor of a negative: rounds away from 0
                    else:
                        node.yview_scroll(-(-(top + height - view) // step), "units")  # ceil
                    node.update_idletasks()  # canvas items only move on redraw
            node = node.master
    except tk.TclError:
        pass


def install_wheel_router(root):
    system = root.tk.call("tk", "windowingsystem")
    sequences = ["<MouseWheel>"] + (["<Button-4>", "<Button-5>"] if system == "x11" else [])
    for sequence in sequences:
        root.bind_all(sequence, lambda e: route_wheel(root, e), add="+")
        # A readonly combobox would otherwise change its value under a wheel meant for the page.
        root.bind_class("TCombobox", sequence, lambda e: (route_wheel(root, e), "break")[1])
    root.bind_all("<FocusIn>", reveal_focus, add="+")


class Scrollable:
    """A vertically scrolling area: a canvas holding `inner` (put your widgets there) plus a
    scrollbar. `inner` always matches the canvas width, the scroll region follows its content, and
    the wheel works anywhere over it (see route_wheel). `fill=True` makes the content at least as
    tall as the view so expanding children (a log box) stretch; use it only for content whose size
    does not change by itself. Used for every tab and for the case list."""

    def __init__(self, parent, padding=0, increment=16, notch=3, fill=False):
        self.fill = fill
        self.outer = ttk.Frame(parent)
        self.outer.columnconfigure(0, weight=1)
        self.outer.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.outer, bg=BG, highlightthickness=0, bd=0, width=1, height=1,
                                yscrollincrement=increment)
        self.scrollbar = ttk.Scrollbar(self.outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.inner = ttk.Frame(self.canvas, padding=padding)
        self._item = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._applied = None
        for target in (self.canvas, self.outer):  # outer: the wheel over the scrollbar scrolls this too
            target._wheel_canvas = self.canvas
        self.canvas._wheel_notch, self.canvas._wheel_increment = notch, increment
        self.canvas.bind("<Configure>", self.sync)
        self.inner.bind("<Configure>", self.sync)

    def sync(self, _event=None):
        width, view = self.canvas.winfo_width(), self.canvas.winfo_height()
        need = self.inner.winfo_reqheight()
        height = max(view, need) if self.fill else need
        applied = (width, height)
        if width <= 1 or applied == self._applied:
            return
        self._applied = applied
        # 0 = no forced height unless filling: the frame then follows what its content asks for.
        self.canvas.itemconfigure(self._item, width=width, height=height if self.fill else 0)
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def set_increment(self, pixels):
        self.canvas.configure(yscrollincrement=pixels)
        self.canvas._wheel_increment = pixels

    def show_scrollbar(self, show):
        if show:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()


def autowrap(label, container, margin=0, minimum=120):
    """Make `label` wrap at the width actually available (`container`'s width minus `margin`, an
    int or a callable) instead of a fixed pixel width, so long text neither gets cut off nor makes
    the window grow sideways."""
    def update(_event=None):
        room = margin() if callable(margin) else margin
        width = max(minimum, container.winfo_width() - room)
        if str(label.cget("wraplength")) != str(width):
            label.configure(wraplength=width)
    container.bind("<Configure>", update, add="+")
    update()


def flow_layout(container, widgets, gap=8):
    """Lay `widgets` out left to right inside `container`, starting a new row whenever the next one
    would not fit, so a row of buttons never runs off the edge of a narrow window."""
    last = [None]

    def layout(_event=None):
        width = container.winfo_width()
        if width <= 1:
            width = 10 ** 6  # not laid out yet: one row, corrected on the first <Configure>
        if last[0] == width:
            return
        last[0] = width
        row = column = used = 0
        for widget in widgets:
            need = widget.winfo_reqwidth()
            if column and used + need > width:
                row, column, used = row + 1, 0, 0
            widget.grid(row=row, column=column, sticky="w", padx=(0, gap), pady=(0, gap if row else 0))
            column += 1
            used += need + gap
    container.bind("<Configure>", layout, add="+")
    layout()


def other_columns(group, column, base):
    """Margin for autowrap() of a label in grid column `column` of `group`: the widths of the
    other columns plus `base` for padding."""
    def room():
        return base + sum(group.grid_bbox(c, 0)[2] for c in range(group.grid_size()[0]) if c != column)
    return room


def apply_dark_theme(window):
    style = ttk.Style(window)
    scale = ui_scale(window)
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
    # The case-selection rows: a big indicator and larger text so they are easy to see and hit.
    style.configure("Case.TCheckbutton", font=(UI_FONT, 12), padding=(round(8 * scale), round(6 * scale)),
                    indicatorsize=round(22 * scale), indicatormargin=(0, 0, round(10 * scale), 0),
                    indicatorbackground=FIELD, indicatorforeground="#ffffff",
                    upperbordercolor=MUTED, lowerbordercolor=MUTED)
    style.map("Case.TCheckbutton", background=[("active", PANEL)],
              indicatorbackground=[("selected", ACCENT), ("active", "#323846")],
              upperbordercolor=[("selected", ACCENT), ("active", FG)], lowerbordercolor=[("selected", ACCENT), ("active", FG)])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, bordercolor=BORDER,
                    rowheight=round(22 * scale))
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


def _premiere_from_windows_registry():
    """Installed-apps registry, so a Premiere install on a non-C: drive is still found —
    Creative Cloud's custom install location is common for Premiere given its size, unlike
    macOS where apps are essentially always under /Applications."""
    import winreg
    found = []
    for path in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                 r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as entry:
                            name = winreg.QueryValueEx(entry, "DisplayName")[0]
                            if "Premiere Pro" not in name:
                                continue
                            install = Path(winreg.QueryValueEx(entry, "InstallLocation")[0])
                            exe = install / "Adobe Premiere Pro.exe"
                            if exe.is_file():
                                found.append((name, str(exe)))
                    except OSError:
                        continue
        except OSError:
            continue
    return sorted(found)[-1][1] if found else None


def find_premiere():
    """Best-guess Premiere Pro program on this computer."""
    if os.name == "nt":
        try:
            found = _premiere_from_windows_registry()
            if found:
                return found
        except Exception:
            pass
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
    enable_dpi_awareness()  # before the first Tk window, so Tk sees the real screen DPI
    window = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    if smoke_test:
        window.withdraw()
    window.title("Premiere Pro Skeleton Builder")
    scale = ui_scale(window)

    def px(n):
        return round(n * scale)

    place_window(window, scale)
    window.configure(padx=px(16), pady=px(12))
    apply_dark_theme(window)
    install_wheel_router(window)
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

    ttk.Label(window, text="Premiere Pro Skeleton Builder", font=(UI_FONT, 17, "bold")).pack(anchor="w", pady=(0, px(8)))

    tabs = ttk.Notebook(window)
    tabs.pack(fill="both", expand=True)
    # Every tab is a scrolling page, so nothing is ever out of reach in a small window. The Runs tab
    # stretches its tree/log to fill a tall window (fill=True), the others are as tall as their content.
    page_increment = px(16)
    build_page = Scrollable(tabs, padding=px(14), increment=page_increment)
    runs_page = Scrollable(tabs, padding=px(14), increment=page_increment, fill=True)
    paths_page = Scrollable(tabs, padding=px(14), increment=page_increment)
    update_page = Scrollable(tabs, padding=px(14), increment=page_increment)
    build_tab, runs_tab, paths_tab, update_tab = build_page.inner, runs_page.inner, paths_page.inner, update_page.inner
    tabs.add(build_page.outer, text="  Build  ")
    tabs.add(runs_page.outer, text="  Runs  ")
    tabs.add(paths_page.outer, text="  Paths & Options  ")
    tabs.add(update_page.outer, text="  Update  ")

    # ---------------- Paths & Options tab (settings live here) ----------------
    settings_vars = {
        "projects_dir": tk.StringVar(value=saved.get("projects_dir") or str(DEFAULT_PROJECTS)),
        # Blank by default: each run then keeps its media inside its own project folder
        # (Projects/<title>/Media/) instead of a separate top-level Media/<title>/ tree,
        # so "Open project folder" shows everything for a run in one place.
        "media_dir": tk.StringVar(value=saved.get("media_dir", "")),
        "models_dir": tk.StringVar(value=saved.get("models_dir") or str(DEFAULT_MODELS)),
        "premiere_exe": tk.StringVar(value=saved.get("premiere_exe") or find_premiere()),
        "videos": tk.BooleanVar(value=saved.get("videos", True)),
        "limit_minutes": tk.StringVar(value=str(saved.get("limit_minutes", 2))),
        "buffer_minutes": tk.StringVar(value=str(saved.get("buffer_minutes", 1))),
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
        value = settings_vars[key].get().strip()
        if not value:  # media_dir may be blank on purpose (each run uses its own Media folder)
            continue
        try:
            Path(value).mkdir(parents=True, exist_ok=True)
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
            if new == old:
                return
            if not new:
                # Only the media folder can be intentionally blank (per-project Media);
                # projects/models always need a real folder, so an empty field there is
                # just an incomplete edit — leave the previous value in place.
                if key != "media_dir":
                    settings_vars[key].set(old)
                    return
                if Path(old).is_dir() and any(Path(old).iterdir()) and not messagebox.askyesno(
                    "Use each project's own Media folder?",
                    f"Switch to keeping each new run's media inside its own project folder, "
                    f"instead of the shared folder at:\n\n{old}\n\n"
                    "Existing files there stay untouched; only new runs are affected."
                ):
                    settings_vars[key].set(old)
                    return
                committed_locations[key] = new
                refresh()
                return
            try:
                Path(new).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                messagebox.showerror("Cannot use that folder", str(error))
                settings_vars[key].set(old)
                return
            # A blank `old` (media_dir's per-project default) has nothing in one place to
            # offer moving — each existing run's media is already inside its own project
            # folder — so skip straight to accepting the new shared location.
            if not old:
                committed_locations[key] = new
                refresh()
            elif offer_transfer(key, old, new):
                committed_locations[key] = new
                refresh()  # the Runs list re-scans the (possibly new) projects folder
        return handler

    def pick_folder(key, title, suggested=None):
        def choose_folder():
            path = filedialog.askdirectory(title=title, initialdir=settings_vars[key].get() or suggested or str(ROOT))
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
        ("Media download folder", "media_dir",
         pick_folder("media_dir", "Choose media download folder", str(DEFAULT_MEDIA)),
         "Leave blank (default) to keep each run's images/video inside its own project folder — "
         "the clearest layout to browse. Set a folder here only to keep media separately (e.g. on "
         "a bigger drive), organized as <this folder>/<project name>/."),
        ("Speech model folder", "models_dir", pick_folder("models_dir", "Choose speech model folder"),
         "faster-whisper downloads the chosen model here the first time it is used (0.5–1.6 GB), then reuses it."),
        ("Premiere Pro program", "premiere_exe", pick_program,
         "Used by 'Copy Skeleton Path & Open Premiere'. Detected automatically when possible."),
    ]
    for r, (label, key, command, hint) in enumerate(location_rows):
        ttk.Label(paths_group, text=label, font=(UI_FONT, 10, "bold")).grid(row=r * 2, column=0, sticky="w", pady=(6, 0))
        entry = ttk.Entry(paths_group, textvariable=settings_vars[key])
        entry.grid(row=r * 2, column=1, sticky="ew", padx=8, pady=(6, 0))
        if key in FOLDER_LABELS:
            entry.bind("<Return>", commit_location(key))
            entry.bind("<FocusOut>", commit_location(key))
        ttk.Button(paths_group, text="Browse…", command=command).grid(row=r * 2, column=2, pady=(6, 0))
        hint_label = ttk.Label(paths_group, text=hint, foreground=MUTED, font=(UI_FONT, 9), justify="left")
        hint_label.grid(row=r * 2 + 1, column=1, sticky="w", padx=8)
        autowrap(hint_label, paths_group, other_columns(paths_group, 1, px(2 * 12 + 16 + 8)))

    options_group = ttk.LabelFrame(paths_tab, text=" Options ", padding=12)
    options_group.pack(fill="x", pady=(12, 0))
    ttk.Checkbutton(options_group, text="Download linked videos (YouTube, other sites, direct files, Drive) and keep extra handles",
                    variable=settings_vars["videos"]).grid(row=0, column=0, columnspan=6, sticky="w")

    def labelled(row, col, text, var, width, unit="", bounds=None):
        ttk.Label(options_group, text=text).grid(row=row, column=col, sticky="w", pady=6, padx=(0, 6))
        entry = ttk.Entry(options_group, textvariable=var, width=width)
        entry.grid(row=row, column=col + 1, sticky="w")
        hint = ttk.Label(options_group, text=unit, foreground=MUTED)
        if unit or bounds:
            hint.grid(row=row, column=col + 2, sticky="w", padx=(4, 18))
        if bounds:
            # Validated on FocusOut, not just at Build time - typing garbage here previously
            # sat silent (auto-saved as-is) until the next Build click produced a generic
            # ValueError with no indication of which field was wrong.
            lo, hi = bounds
            def validate(_=None):
                try:
                    value = float(var.get())
                    if not (lo <= value <= hi):
                        raise ValueError
                    hint.configure(text=unit, foreground=MUTED)
                except ValueError:
                    hint.configure(text=f"needs {lo:g}–{hi:g}", foreground=WARN_FG)
            entry.bind("<FocusOut>", validate)
        return entry

    labelled(1, 0, "Download whole videos up to", settings_vars["limit_minutes"], 6, "min", bounds=(1, 1440))
    labelled(1, 3, "Handles per side", settings_vars["buffer_minutes"], 6, "min", bounds=(0, 60))
    width_entry = labelled(2, 0, "Resolution", settings_vars["width"], 6, "×")
    height_entry = ttk.Entry(options_group, textvariable=settings_vars["height"], width=6)
    height_entry.grid(row=2, column=3, sticky="w")
    resolution_hint = ttk.Label(options_group, text="", foreground=MUTED)
    resolution_hint.grid(row=2, column=4, sticky="w", padx=(4, 18))

    def validate_resolution(_=None):
        # Matches finish_launch()'s actual check below (positive numbers only, no upper cap) -
        # showing a stricter range here would tell the user something is wrong that Build would
        # actually accept.
        try:
            width, height = float(settings_vars["width"].get()), float(settings_vars["height"].get())
            if width <= 0 or height <= 0:
                raise ValueError
            resolution_hint.configure(text="", foreground=MUTED)
        except ValueError:
            resolution_hint.configure(text="width/height must be positive numbers", foreground=WARN_FG)

    width_entry.bind("<FocusOut>", validate_resolution)
    height_entry.bind("<FocusOut>", validate_resolution)
    ttk.Label(options_group, text="Speech model").grid(row=3, column=0, sticky="w", pady=6)
    ttk.Combobox(options_group, textvariable=settings_vars["whisper_model"], values=WHISPER_MODELS, width=18).grid(row=3, column=1, sticky="w")
    ttk.Label(options_group, text="Runs on").grid(row=3, column=3, sticky="w", padx=(0, 6))
    ttk.Combobox(options_group, textvariable=settings_vars["device"], values=["auto", "cpu", "cuda"], state="readonly", width=8).grid(row=3, column=4, sticky="w")
    speech_hint = ttk.Label(options_group, text="Speech recognition runs on your computer (faster-whisper). 'auto' uses the graphics card when available.",
                            foreground=MUTED, font=(UI_FONT, 9), justify="left")
    speech_hint.grid(row=4, column=0, columnspan=6, sticky="w", pady=(6, 0))
    autowrap(speech_hint, options_group, px(2 * 12 + 8))

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

    inputs_group = ttk.LabelFrame(build_tab, text=" 1 · Inputs ", padding=12)
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
        found = []
        value = _resolve_script(source, quiet, update_var, found)
        if update_var:
            # The case list always describes the script currently in the field: a failed or
            # emptied field must not leave the previous script's cases ticked.
            window.after(0, lambda: show_cases(found if value else [], ok=bool(value)))
        return value

    def _resolve_script(source, quiet, update_var, found):
        value = (source if source is not None else script_var.get()).strip()
        if not value:
            return None
        tab_warning = missing_tab_warning(value)
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
        found.extend(result.get("case_list") or [])
        if found:
            note += f" {len(found)} case(s) found."
        if result.get("warnings"):
            note += f" {len(result['warnings'])} warning(s): {result['warnings'][0]}"
        if tab_warning:
            note += f"\n⚠ {tab_warning}"
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
        hint_label = ttk.Label(inputs_group, text=hint, foreground=MUTED, font=(UI_FONT, 9), justify="left")
        hint_label.grid(row=row * 2 + 1, column=1, sticky="w", padx=8)
        autowrap(hint_label, inputs_group, other_columns(inputs_group, 1, px(2 * 12 + 16 + 8)))
        if on_change:
            entry.bind("<Return>", lambda _e: on_change())
            entry.bind("<FocusOut>", lambda _e: on_change())
        return entry

    script_entry = add_input(0, "Script", script_var, "Google Doc link (must be viewable by anyone with the link) or a .docx file.", "*.docx", resolve_script_async)
    audio_entry = add_input(1, "Voiceover", audio_var, "Drop an audio file here, paste a Google Drive link, or browse.", "*.mp3 *.wav *.m4a *.aac *.flac")
    status_label = ttk.Label(inputs_group, textvariable=inspect_status, justify="left")
    status_label.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
    autowrap(status_label, inputs_group, px(2 * 12 + 8))

    # Stage 2: which cases this editor is building. Per-run, deliberately not remembered.
    cases_group = ttk.LabelFrame(build_tab, text=" 2 · Cases ", padding=12)
    cases_group.pack(fill="x", pady=(10, 0))
    case_note = tk.StringVar(value="Add a script to see its cases.")
    case_note_label = ttk.Label(cases_group, textvariable=case_note, foreground=MUTED, font=(UI_FONT, 9), justify="left")
    case_note_label.pack(anchor="w")
    autowrap(case_note_label, cases_group, px(2 * 12 + 8))
    case_hint_label = ttk.Label(cases_group, text="Tick only the cases your voiceover covers: it may cover all of them or just some.",
                                foreground=MUTED, font=(UI_FONT, 9), justify="left")
    case_hint_label.pack(anchor="w")
    autowrap(case_hint_label, cases_group, px(2 * 12 + 8))
    # The list scrolls on its own (wheel anywhere over it, including over the rows, and the keyboard);
    # at either end the wheel passes on to the page behind it. Its height follows the room available.
    case_list = Scrollable(cases_group, increment=px(32))
    case_body, case_canvas, case_rows = case_list.outer, case_list.canvas, case_list.inner  # packed only while the script has cases
    case_buttons = ttk.Frame(cases_group)
    ttk.Button(case_buttons, text="Select all", command=lambda: set_all_cases(True)).pack(side="left")
    ttk.Button(case_buttons, text="Clear", command=lambda: set_all_cases(False)).pack(side="left", padx=(6, 0))
    case_info, case_vars, filling = [], {}, [False]

    def ticked_cases():
        return {n for n, var in case_vars.items() if var.get()}

    def refresh_case_note():
        numbers = [c["number"] for c in case_info]
        ticked = ticked_cases()
        if not ticked:
            case_note.set("Tick at least one case.")
            return
        covered = [n for n in numbers if min(ticked) <= n <= max(ticked)]
        if len(covered) == len(numbers):
            case_note.set(f"Building the whole video ({len(numbers)} cases).")
            return
        span = f"case {covered[0]}" if len(covered) == 1 else f"cases {covered[0]}–{covered[-1]}"
        text = f"Building {span} of {len(numbers)}: the voiceover is cut in the pause between cases."
        if covered[0] == numbers[0]:
            text += " The intro goes with this share."
        if covered[-1] == numbers[-1]:
            text += " The end of the voiceover goes with this share."
        case_note.set(text)

    def on_case_toggled(_number=None):
        # One contiguous stretch only: ticking 1 and 3 also ticks 2.
        if filling[0]:
            return
        ticked = ticked_cases()
        if ticked:
            filling[0] = True
            for n, var in case_vars.items():
                if min(ticked) <= n <= max(ticked):
                    var.set(True)
            filling[0] = False
        refresh_case_note()

    def set_all_cases(value):
        for var in case_vars.values():
            var.set(value)
        refresh_case_note()

    case_row_widgets = []  # (Checkbutton, case) in list order
    case_font = tkfont.Font(family=UI_FONT, size=12)  # same font as the Case.TCheckbutton style

    def set_case_tab_stop(active):
        for row, _case in case_row_widgets:
            row.configure(takefocus=1 if row is active else 0)

    def focus_case_row(index):
        if case_row_widgets:
            case_row_widgets[max(0, min(index, len(case_row_widgets) - 1))][0].focus_set()
        return "break"

    def fit_case_rows(_event=None):
        """Row text is cut with an ellipsis (title only, never the cue counts) to the room the row
        really has, so the list never needs a horizontal scrollbar."""
        room = max(px(160), case_canvas.winfo_width() - px(2 * 8 + 22 + 10 + 12))
        for row, c in case_row_widgets:
            title, tail = c["title"], f"   ·   {c['image_cues']} image, {c['video_cues']} video cue(s)"
            if case_font.measure(title + tail) <= room:
                text = title + tail
            else:  # longest title prefix that still fits next to the ellipsis and the counts
                lo, hi = 0, len(title)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if case_font.measure(title[:mid].rstrip() + "…" + tail) <= room:
                        lo = mid
                    else:
                        hi = mid - 1
                text = title[:lo].rstrip() + "…" + tail
            if row.cget("text") != text:
                row.configure(text=text)

    def size_case_list(_event=None):
        """List height: every case when there are few, otherwise about half the page (never fewer than
        four rows), in whole rows of whatever height the font/DPI makes them."""
        if not case_row_widgets:
            return
        count = len(case_row_widgets)
        row_height = max(1, case_row_widgets[0][0].winfo_reqheight())
        viewport = build_page.canvas.winfo_height()
        if viewport <= 1:
            viewport = window.winfo_height() * 0.7
        visible = min(count, max(4, int(viewport * 0.5 // row_height)))
        case_list.set_increment(row_height)
        case_canvas.configure(height=visible * row_height)
        case_list.show_scrollbar(count > visible)

    case_canvas.bind("<Configure>", fit_case_rows, add="+")
    build_page.canvas.bind("<Configure>", size_case_list, add="+")

    def show_cases(cases, ok=True):
        """Rebuild the checkbox list for `cases` ([{number, title, image_cues, video_cues}]);
        `ok` is False when there is no usable script. The same list again (a re-check of the
        same script) keeps the editor's ticks."""
        if cases and cases == case_info:
            return
        same_cases = [c["number"] for c in cases] == [c["number"] for c in case_info]
        keep = {n: var.get() for n, var in case_vars.items()} if same_cases else {}
        for child in case_rows.winfo_children():
            child.destroy()
        case_row_widgets.clear()
        case_info[:] = list(cases)
        case_vars.clear()
        if not cases:
            case_body.pack_forget()
            case_buttons.pack_forget()
            case_note.set("No case headings found — the whole video will be built." if ok
                          else "Add a valid script to see its cases.")
            return
        for index, c in enumerate(cases):
            var = tk.BooleanVar(value=keep.get(c["number"], True))
            case_vars[c["number"]] = var
            # The whole row is the Checkbutton, so a click anywhere on it toggles the case.
            row = ttk.Checkbutton(case_rows, style="Case.TCheckbutton", variable=var, command=on_case_toggled,
                                  takefocus=1 if index == 0 else 0)  # one Tab stop for the list
            row.pack(fill="x")
            row.bind("<FocusIn>", lambda _e, row=row: set_case_tab_stop(row))
            row.bind("<Up>", lambda _e, i=index: focus_case_row(i - 1))
            row.bind("<Down>", lambda _e, i=index: focus_case_row(i + 1))
            row.bind("<Home>", lambda _e: focus_case_row(0))
            row.bind("<End>", lambda _e: focus_case_row(len(case_row_widgets) - 1))
            case_row_widgets.append((row, c))
        case_body.pack_forget()
        case_buttons.pack_forget()
        case_body.pack(fill="x", pady=(6, 0))
        case_buttons.pack(anchor="w", pady=(6, 0))
        window.update_idletasks()
        fit_case_rows()
        size_case_list()
        on_case_toggled()

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
    build_buttons = ttk.Frame(build_frame)  # Build / Preview / Stop, with the progress bar on its own row below
    build_buttons.pack(fill="x")
    progress_row = ttk.Frame(build_frame)
    progress_row.pack(fill="x", pady=(px(8), 0))
    progress_bar = ttk.Progressbar(progress_row, mode="indeterminate")
    phase_label = ttk.Label(progress_row, text="", foreground=MUTED)

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
                # "" (no case list, or every case ticked) builds the whole video.
                cases = case_range_for(ticked_cases(), [c["number"] for c in case_info]) if case_info else ""
                width, height = int(settings_vars["width"].get()), int(settings_vars["height"].get())
                limit_minutes, buffer_minutes = float(settings_vars["limit_minutes"].get()), float(settings_vars["buffer_minutes"].get())
                if width <= 0 or height <= 0:
                    raise ValueError("Width and height must be positive numbers.")
                if not 1 <= limit_minutes <= 1440 or not 0 <= buffer_minutes <= 60:
                    raise ValueError("Use a limit of 1–1440 minutes and handles of 0–60 minutes.")
                root = projects_dir({"projects_dir": settings_vars["projects_dir"].get().strip()})
                config = {
                    "docx": docx, "docx_source": script_var.get().strip(), "audio": audio,
                    # Named per share so two editors' runs of one script never collide or get confused.
                    "title": f"{Path(docx).stem} (cases {cases})" if cases else Path(docx).stem,
                    "cases": cases,
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

    build_btn = ttk.Button(build_buttons, text="⚡  Build Skeleton", command=launch, padding=(14, 7))
    build_btn.pack(side="left")
    preview_btn = ttk.Button(build_buttons, text="👁  Preview cues", command=preview, padding=(10, 7))
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

    stop_btn = ttk.Button(build_buttons, text="⏹  Stop", command=stop_current_run, padding=(10, 7))
    stop_btn.pack(side="left", padx=(8, 0))
    phase_label.pack(side="left", padx=(0, 14))
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
    current_label = ttk.Label(result_group, textvariable=current_var, justify="left")
    current_label.pack(anchor="w", pady=(0, 8))
    autowrap(current_label, result_group, px(2 * 12 + 8))

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
        # The system clipboard, written immediately and read back - Tk's own clipboard hands the
        # text over lazily, so it can arrive empty if this window is busy when Premiere asks.
        def copy_path():
            def tk_copy(text):
                window.clipboard_clear()
                window.clipboard_append(text)
            return copy_text(str(xml), fallback=tk_copy)

        copied = copy_path()
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
        show_copy_result(str(xml), opened, copied, copy_path)

    def show_copy_result(xml, opened, copied, copy_path):
        top = tk.Toplevel(window)
        top.title("Copy Skeleton Path")
        top.configure(bg=BG, padx=16, pady=14)
        top.transient(window)
        top.resizable(False, False)
        state = tk.StringVar()

        def set_state(ok):
            state.set("✓ The path is on your clipboard." if ok else
                      "⚠ Couldn't confirm the path was copied. Select it below and copy it (Ctrl+C / Cmd+C), "
                      "or press Copy again.")

        set_state(copied)
        ttk.Label(top, text=("Premiere Pro is opening. " if opened else "Couldn't launch Premiere Pro automatically — "
                             "open it yourself, or set its location in Paths & Options. ") +
                  "Premiere has no way to import an XML automatically from outside the app, so finish it "
                  "manually:\n\n1. In Premiere, press Ctrl+I (Cmd+I on Mac), or File > Import.\n"
                  "2. Paste the path into the filename box and press Enter.",
                  wraplength=560, justify="left").pack(anchor="w")
        ttk.Label(top, textvariable=state, wraplength=560, justify="left").pack(anchor="w", pady=(10, 4))
        path_box = ttk.Entry(top, width=80)
        path_box.insert(0, xml)
        path_box.configure(state="readonly")
        path_box.pack(fill="x")
        row = ttk.Frame(top)
        row.pack(anchor="e", pady=(12, 0))
        ttk.Button(row, text="Copy again", command=lambda: set_state(copy_path())).pack(side="left")
        ttk.Button(row, text="OK", command=top.destroy).pack(side="left", padx=(8, 0))
        top.grab_set()
        top.wait_window()

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

    def delete_run():
        run = chosen_run()
        if not run:
            return
        if run["display_status"] in ("Running", "Starting"):
            messagebox.showwarning("Cannot delete", "Stop the build first before deleting its folder.")
            return
        folder = Path(run["folder"])
        if not messagebox.askyesno("Delete run?",
                f'Permanently delete "{folder.name}" and everything inside it '
                '(downloaded media, transcripts, output)? This cannot be undone.'):
            return
        try:
            shutil.rmtree(folder)
        except OSError as error:
            messagebox.showerror("Could not delete", str(error))
            return
        known.pop(str(folder), None)
        if selected[0] == str(folder):
            selected[0], last_log[0] = "", None
        refresh()

    button_row = ttk.Frame(result_group)
    button_row.pack(fill="x")
    premiere_btn = ttk.Button(button_row, text="📋  Copy Skeleton Path & Open Premiere", command=run_premiere, padding=(10, 5))
    open_btn = ttk.Button(button_row, text="Open project folder", command=open_project, padding=(10, 5))
    retry_btn = ttk.Button(button_row, text="⟳  Retry script & audio fetch", command=retry_run, padding=(10, 5))
    delete_btn = ttk.Button(button_row, text="🗑  Delete run", command=delete_run, padding=(10, 5))
    flow_layout(button_row, [premiere_btn, open_btn, retry_btn, delete_btn], gap=px(8))  # wraps onto more rows when narrow

    # ---------------- Update tab ----------------
    version_group = ttk.LabelFrame(update_tab, text=" Version ", padding=12)
    version_group.pack(fill="x")
    current_full_commit = updater.current_commit()
    ttk.Label(version_group, text=f"Current version: {current_full_commit[:7]}",
              font=(UI_FONT, 10, "bold")).pack(anchor="w")
    ttk.Label(version_group, text=current_full_commit, foreground=MUTED, font=(UI_FONT, 9)).pack(anchor="w")

    update_group = ttk.LabelFrame(update_tab, text=" Update ", padding=12)
    update_group.pack(fill="x", pady=(12, 0))
    update_status_var = tk.StringVar(value="Checking for updates…" if FROZEN else
                                      "Updates are only available in the packaged app.")
    update_status_label = ttk.Label(update_group, textvariable=update_status_var, foreground=MUTED, justify="left")
    update_status_label.pack(anchor="w", pady=(0, 8))
    autowrap(update_status_label, update_group, px(2 * 12 + 8))
    update_buttons_row = ttk.Frame(update_group)
    update_buttons_row.pack(anchor="w")
    update_btn = ttk.Button(update_buttons_row, text="⬆  Update Now", state="disabled")
    update_btn.pack(side="left")
    cancel_update_btn = ttk.Button(update_buttons_row, text="Cancel", state="disabled")
    cancel_update_btn.pack(side="left", padx=(8, 0))
    update_progress_var = tk.StringVar(value="")
    update_progress_label = ttk.Label(update_group, textvariable=update_progress_var, foreground=MUTED, justify="left")
    update_progress_label.pack(anchor="w", pady=(6, 0))
    autowrap(update_progress_label, update_group, px(2 * 12 + 8))

    update_info = [{}]
    updating = [False]
    cancel_requested = [False]
    # True while update_progress_var is showing a result (failed/cancelled) that must survive
    # until the user acts again, rather than being blanked by the very next poll() tick's call
    # to update_button_state() a moment later - previously a failure message could disappear
    # under 2 seconds after appearing, before anyone had a chance to read it.
    sticky_message = [False]

    def blocking_reason():
        """Why Update Now should stay disabled right now, or None if it's clear to update -
        the update must not run while literally anything else in the app is active."""
        if updating[0]:
            return "Update already in progress…"
        if resolving[0]:
            return "Waiting for the script/audio fetch to finish…"
        if read_queue():
            return "Queued build(s) waiting — clear the queue first."
        if any(r["display_status"] in ("Running", "Starting") for r in runs(settings_vars["projects_dir"].get().strip() or None)):
            return "A build is currently running."
        return None

    def update_button_state():
        """Cheap, local-only recheck (no network) - safe to call on every poll() tick so the
        button reacts immediately to a build starting/finishing elsewhere in the app."""
        if not FROZEN:
            update_btn.configure(state="disabled")
            cancel_update_btn.configure(state="disabled")
            return
        cancel_update_btn.configure(state="normal" if updating[0] else "disabled")
        reason = blocking_reason()
        info = update_info[0]
        if reason:
            update_btn.configure(state="disabled")
            if not updating[0] and not sticky_message[0]:
                update_progress_var.set(reason)
        elif info.get("available"):
            update_btn.configure(state="normal")
            if not sticky_message[0]:
                update_progress_var.set("")
        else:
            update_btn.configure(state="disabled")

    def check_for_updates():
        if not FROZEN:
            return

        def worker():
            info = updater.update_available()
            def done():
                update_info[0] = info
                sticky_message[0] = False
                if info.get("error"):
                    update_status_var.set(f"Couldn't check for updates: {info['error']}")
                elif info.get("available"):
                    update_status_var.set(f"Update available: {info['latest'][:7]} (current: {info['current'][:7]})")
                else:
                    update_status_var.set(f"Up to date (commit {info['current'][:7]})")
                update_button_state()
            window.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def cancel_update():
        if updating[0]:
            cancel_requested[0] = True
            update_progress_var.set("Cancelling…")

    cancel_update_btn.configure(command=cancel_update)

    def do_update():
        reason = blocking_reason()
        if reason:
            messagebox.showwarning("Cannot update", reason)
            return
        info = update_info[0]
        if not info.get("available"):
            return
        if not messagebox.askyesno("Update Skeleton Builder",
                f"This will close and restart Skeleton Builder to install version {info['latest'][:7]}.\n\n"
                "Your Projects, Media, Models and Cache folders are not affected.\n\nContinue?"):
            return
        updating[0] = True
        cancel_requested[0] = False
        sticky_message[0] = False
        update_btn.configure(state="disabled")
        cancel_update_btn.configure(state="normal")
        update_progress_var.set("Starting update…")

        def worker():
            try:
                zip_path = updater.download_update(info["asset_url"],
                    progress=lambda m: window.after(0, lambda: update_progress_var.set(m)),
                    should_cancel=lambda: cancel_requested[0])
                window.after(0, lambda: update_progress_var.set("Restarting…"))
                updater.launch_updater_and_exit(zip_path)
                window.after(200, window.destroy)
            except updater.UpdateCancelled:
                def cancelled():
                    updating[0] = False
                    sticky_message[0] = True
                    update_progress_var.set("Update cancelled.")
                    update_button_state()
                window.after(0, cancelled)
            except Exception as error:
                def failed():
                    updating[0] = False
                    sticky_message[0] = True
                    update_progress_var.set(f"Update failed: {error}")
                    update_button_state()
                window.after(0, failed)

        threading.Thread(target=worker, daemon=True).start()

    update_btn.configure(command=do_update)
    check_for_updates()

    def update_poll():
        try:
            if not window.winfo_exists():
                return
        except tk.TclError:
            return
        check_for_updates()
        window.after(10 * 60 * 1000, update_poll)  # 10 min - a GitHub API call, keep it infrequent

    if FROZEN:
        window.after(10 * 60 * 1000, update_poll)

    # ---------------- Runs tab ----------------
    tree = ttk.Treeview(runs_tab, columns=("status", "date"), height=6)
    tree.heading("#0", text="Project")
    tree.heading("status", text="Status")
    tree.heading("date", text="Created")
    tree.column("#0", width=px(260), minwidth=px(140))  # modest widths: the columns stretch to fill, but never force the tab wider
    tree.column("status", width=px(150), minwidth=px(90))
    tree.column("date", width=px(130), minwidth=px(90))
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
        delete_btn.configure(state="normal" if (r and r["display_status"] not in ("Running", "Starting")) else "disabled")
        update_button_state()
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
