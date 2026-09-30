"""Put text on the operating system's clipboard right away, and check that it arrived.

Tk's own clipboard (`clipboard_append`) hands the text over lazily on Windows: another program
only receives it if this app's window is processing events at the moment it asks. Launching
Premiere (or anything else that freezes the window for a moment) can therefore leave the
clipboard empty. Writing through the system API puts the text there immediately.
"""
import subprocess
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _windows_api():
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CreateWindowExW.restype = wintypes.HWND
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    return ctypes, user32, kernel32


def _open_windows_clipboard(user32, owner, attempts=20):
    # Another program (a clipboard manager, Premiere starting up) may hold it for a moment.
    for _ in range(attempts):
        if user32.OpenClipboard(owner):
            return True
        time.sleep(0.05)
    return False


def _windows_copy(text):
    ctypes, user32, kernel32 = _windows_api()
    # A clipboard opened without an owner window can't be written to, so borrow a hidden one.
    owner = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    try:
        if not _open_windows_clipboard(user32, owner):
            return False
        try:
            user32.EmptyClipboard()
            data = (text + "\0").encode("utf-16-le")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
            kernel32.GlobalUnlock(handle)
            return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
        finally:
            user32.CloseClipboard()
    finally:
        if owner:
            user32.DestroyWindow(owner)


def _windows_read():
    ctypes, user32, kernel32 = _windows_api()
    if not _open_windows_clipboard(user32, None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _mac_copy(text):
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=10)
    return True


def _mac_read():
    return subprocess.run(["pbpaste"], capture_output=True, timeout=10, check=True).stdout.decode("utf-8")


def copy_text(text, fallback=None):
    """Copy `text` to the system clipboard. Returns True only if reading the clipboard back
    gives the same text. `fallback(text)` is tried when the system route fails (e.g. Tk's
    clipboard on Linux), and is not trusted: its result is reported as False unless it can be
    read back."""
    routes = []
    if sys.platform == "win32":
        routes.append((_windows_copy, _windows_read))
    elif sys.platform == "darwin":
        routes.append((_mac_copy, _mac_read))
    for write, read in routes:
        try:
            if write(text) and read() == text:
                return True
        except Exception:
            pass
    if fallback:
        try:
            fallback(text)
        except Exception:
            pass
    return False
