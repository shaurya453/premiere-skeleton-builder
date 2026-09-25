"""In-app self-update: check the rolling "latest" GitHub Release, download it, and swap it
into place without touching Projects/Media/Models/Cache/Temp.

The running (old) app only confirms, downloads, and hands off - see app.py's Update tab and
skeleton_app.py's "--finish-update" mode. The actual file swap in finish_update() always runs
from a *freshly launched, detached* copy of the app (the same frozen executable, re-invoked
with a hidden flag - see skeleton_app.py), after the old process has exited, so it never
fights the running app for its own open file handles and needs no separate helper script.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zipfile import ZipFile

from jobs import alive
from paths import BUNDLE, FROZEN, NO_WINDOW, ROOT, app_dir, cache_dir

REPO = "shaurya453/premiere-skeleton-builder"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "SkeletonBuilder-Windows.zip" if os.name == "nt" else "SkeletonBuilder-macOS.zip"
UPDATE_LOG = cache_dir() / "update.log"
_USER_AGENT = {"User-Agent": "SkeletonBuilderUpdater"}


def _log(message):
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with UPDATE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")


def current_commit() -> str:
    """The commit this running build was made from, or "unknown" if it can't be determined
    (not a git checkout and not a packaged build with a bundled VERSION.txt)."""
    version_file = BUNDLE / "VERSION.txt"
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    if not FROZEN:
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                     text=True, check=True, **NO_WINDOW)
            return result.stdout.strip()
        except Exception:
            pass
    return "unknown"


def latest_release(timeout=15):
    """{"commit", "asset_url", "size"} for this platform's asset in the rolling "latest"
    release. Raises ValueError/OSError with a message safe to show directly in the UI."""
    request = urllib.request.Request(LATEST_RELEASE_API, headers={
        **_USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ValueError(f"GitHub returned HTTP {error.code} checking for updates") from error
    except urllib.error.URLError as error:
        raise ValueError(f"Network error checking for updates: {error.reason}") from error
    commit = None
    for line in (data.get("body") or "").splitlines():
        if line.strip().startswith("commit:"):
            commit = line.split(":", 1)[1].strip()
    if not commit:
        raise ValueError("Latest release has no recorded commit")
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset:
        raise ValueError(f"Latest release has no {ASSET_NAME} asset")
    return {"commit": commit, "asset_url": asset["browser_download_url"], "size": asset.get("size", 0)}


def update_available():
    """{"current", "latest", "available", "asset_url"} or {"error": "..."} - what the Update
    tab polls. Never raises."""
    current = current_commit()
    try:
        info = latest_release()
    except Exception as error:
        return {"current": current, "error": str(error)}
    return {"current": current, "latest": info["commit"], "asset_url": info["asset_url"],
            "available": current != "unknown" and info["commit"] != current}


def download_update(asset_url, progress=lambda m: None):
    """Download the new build's zip to a temp file; returns its path."""
    dest_dir = Path(cache_dir()) / "update_download"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "update.zip"
    request = urllib.request.Request(asset_url, headers=_USER_AGENT)
    progress("Downloading update...")
    with urllib.request.urlopen(request, timeout=180) as response, dest.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done, next_report = 0, 5 * 1024 * 1024
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done >= next_report:
                progress(f"Downloading update: {done / 1024**2:.0f}"
                          + (f" / {total / 1024**2:.0f}" if total else "") + " MB")
                next_report = done + 5 * 1024 * 1024
    return dest


def _mac_app_bundle_path():
    """The .app bundle's own path (unlike paths.app_dir(), which returns its *parent* - the
    folder Projects/Media/etc. live in)."""
    exe = Path(sys.executable).resolve()
    for ancestor in exe.parents:
        if ancestor.suffix == ".app":
            return ancestor
    raise RuntimeError("Could not locate the .app bundle from the running executable")


def launch_updater_and_exit(zip_path):
    """Called by the running (old) app once the update is downloaded and confirmed: launches
    a detached copy of this same executable in --finish-update mode, which will wait for this
    process to exit and then do the actual swap. Caller must exit right after (e.g.
    window.destroy() then sys.exit()) - this only spawns the helper, it doesn't wait."""
    install_dir = str(_mac_app_bundle_path() if sys.platform == "darwin" else app_dir())
    options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    launcher = ([sys.executable, "--finish-update", str(zip_path), install_dir, str(os.getpid())] if FROZEN
                else [sys.executable, str(ROOT / "skeleton_app.py"), "--finish-update",
                      str(zip_path), install_dir, str(os.getpid())])
    _log(f"Launching updater: {launcher}")
    subprocess.Popen(launcher, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, **options)


def _extract(zip_path, extract_dir):
    """Extract the downloaded zip. macOS must use ditto, not zipfile: the packaging step
    (build.yml) archives with ditto specifically to preserve the AppleDouble metadata behind
    the app's code signature (_CodeSignature/CodeResources) - zipfile can't reconstruct that
    on extraction, and the result fails Gatekeeper with "is damaged" (this exact bug was
    debugged and fixed for the *shipped* build earlier; the updater must not reintroduce it
    for the *downloaded* build)."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(extract_dir)], check=True)
    else:
        with ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)


def finish_update(zip_path, install_dir, old_pid, wait_timeout=30):
    """Run headlessly (no Tkinter) from a freshly launched, detached process - see
    launch_updater_and_exit(). Waits for the old process to exit, then replaces only the
    app's own files (the exe/_internal on Windows, the whole .app bundle on macOS) and
    relaunches. Projects/Media/Models/Cache/Temp are never referenced by this function at
    all, so they survive untouched regardless of what's in the downloaded zip (which also
    ships its own fresh, empty copies of them for a first-time install)."""
    zip_path, install_dir, old_pid = Path(zip_path), Path(install_dir), int(old_pid)
    try:
        _log(f"finish_update starting: zip={zip_path} install_dir={install_dir} old_pid={old_pid}")
        deadline = time.time() + wait_timeout
        while alive(old_pid) and time.time() < deadline:
            time.sleep(0.5)
        if alive(old_pid):
            _log(f"Old process {old_pid} never exited after {wait_timeout}s; aborting update")
            return False

        extract_dir = zip_path.parent / "extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        _extract(zip_path, extract_dir)

        if sys.platform == "darwin":
            new_app = next(extract_dir.glob("*.app"))
            if install_dir.exists():
                shutil.rmtree(install_dir)
            subprocess.run(["ditto", str(new_app), str(install_dir)], check=True)
            relaunch = ["open", str(install_dir)]
            backup_dir = None
        else:
            new_root = next(extract_dir.iterdir())  # the single "SkeletonBuilder" folder
            old_internal = install_dir / "_internal"
            old_exe = install_dir / "SkeletonBuilder.exe"
            # This helper process is itself launched as a copy of old_exe (see
            # launch_updater_and_exit), so old_exe/_internal are this process's own running
            # image and loaded DLLs. Windows blocks opening those for in-place overwrite, but
            # (like most self-updating Windows apps) permits renaming/deleting them
            # (FILE_SHARE_DELETE) - so rename them aside first, then copy the new files in
            # under the original names, then best-effort clean up the renamed originals.
            backup_dir = install_dir / "_update_old"
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            backup_dir.mkdir()
            if old_internal.exists():
                os.replace(old_internal, backup_dir / "_internal")
            os.replace(old_exe, backup_dir / "SkeletonBuilder.exe")
            shutil.copytree(new_root / "_internal", old_internal)
            shutil.copy2(new_root / "SkeletonBuilder.exe", old_exe)
            relaunch = [str(old_exe)]

        _log(f"Update applied; relaunching: {relaunch}")
        options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        subprocess.Popen(relaunch, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, **options)

        if backup_dir is not None:
            shutil.rmtree(backup_dir, ignore_errors=True)  # best-effort; a leftover is harmless
        shutil.rmtree(extract_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        _log("finish_update done")
        return True
    except Exception as error:
        _log(f"finish_update failed: {error}")
        return False
