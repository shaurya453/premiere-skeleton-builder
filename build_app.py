"""Build the packaged app with PyInstaller.

  python build_app.py            -> dist/SkeletonBuilder/ (Windows) or dist/Premiere Skeleton Builder.app (macOS)
  python build_app.py --node X   also bundles the Node.js executable X (needed for YouTube downloads)

PyInstaller cannot cross-compile: build on Windows for Windows and on a Mac for macOS
(the GitHub workflow in .github/workflows/build.yml does both).
"""
import argparse
import os
import subprocess
from pathlib import Path
import sys

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent


def _windows_tcl_tk_dirs():
    """Find this Python's own Tcl/Tk library directories directly, rather than relying on
    PyInstaller's build-time auto-probe (which silently bundles nothing - no warning, no
    error - if it can't launch a working Tcl interpreter on the build machine)."""
    tcl_root = Path(sys.base_prefix) / "tcl"
    tcl_dir = next(iter(sorted(tcl_root.glob("tcl8.*"))), None)
    tk_dir = next(iter(sorted(tcl_root.glob("tk8.*"))), None)
    return tcl_dir, tk_dir


def _assert_tcl_bundled(dist_dir):
    """A frozen app with no Tcl/Tk data directory fails at launch with a cryptic 'Can't find
    a usable init.tcl' error instead of at build time - fail loudly here instead."""
    if not any(dist_dir.rglob("init.tcl")):
        raise SystemExit(f"Tcl/Tk was not bundled into {dist_dir} (no init.tcl found anywhere "
                          "in the build output) - the packaged app would fail to launch.")


def _codesign_ad_hoc(app_bundle):
    """Ad-hoc sign the whole .app bundle as one sealed unit (no Apple Developer account
    needed - `-` is the "no identity" ad-hoc signature).

    PyInstaller ad-hoc signs individual Mach-O binaries it collects (arm64 requires *some*
    signature just to load at all), but that leaves the .app itself without a top-level seal
    covering Info.plist and Resources. A downloaded (quarantined) app in that state fails
    Gatekeeper's bundle-integrity check with "<app> is damaged and can't be opened - move it
    to the Trash", which offers no override in Finder at all. Ad-hoc signing the whole bundle
    here gives it a real seal, so the same download instead gets the ordinary "unidentified
    developer" prompt, which right-click > Open (or Trust in System Settings) bypasses.
    (Downloaded via a browser or unzipped from one, macOS still quarantines it either way -
    that part is unavoidable without notarizing with a paid Apple Developer ID, so the README's
    "right-click > Open" / `xattr -cr` instructions are still needed for a clean first launch.)
    """
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app_bundle)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app_bundle)], check=True)


def _ship_default_folders(app_root):
    """Create the app's default Projects/Media/Models/Cache/Temp folders right in the
    packaged output, matching paths.py's default_data_root() (next to the .exe on Windows,
    next to the .app bundle on macOS), so they exist the moment the app is unzipped instead
    of only appearing after the first run creates them."""
    for name in ("Projects", "Media", "Models", "Cache", "Temp"):
        (app_root / name).mkdir(parents=True, exist_ok=True)


def _write_version_file():
    """Bake the commit this build was made from into the frozen app, so it can tell the
    in-app updater what version it is. Read back at runtime via paths.BUNDLE/"VERSION.txt"
    (paths.py's BUNDLE already points at PyInstaller's bundle dir, or the repo root when not
    frozen). Falls back to "unknown" rather than failing the build if this isn't a git
    checkout for some reason.

    Written outside HERE/"build" deliberately: that's PyInstaller's --workpath/--specpath
    below, and --clean wipes it before packaging - a file written there would be deleted
    before --add-data could bundle it."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True,
                                 text=True, check=True).stdout.strip()
    except Exception:
        commit = "unknown"
    version_dir = HERE / "build_version"
    version_dir.mkdir(parents=True, exist_ok=True)
    version_file = version_dir / "VERSION.txt"
    version_file.write_text(commit)
    return version_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", help="path to a Node.js executable to bundle")
    args = parser.parse_args()
    mac = sys.platform == "darwin"
    version_file = _write_version_file()
    options = [
        str(HERE / "skeleton_app.py"),
        "--name", "Premiere Skeleton Builder" if mac else "SkeletonBuilder",
        "--noconfirm", "--clean", "--windowed", "--onedir",
        "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
        "--paths", str(HERE),
        "--add-data", f"{version_file}{os.pathsep}.",
        "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "onnxruntime",
        "--collect-all", "tkinterdnd2", "--collect-all", "imageio_ffmpeg",
        "--collect-all", "yt_dlp", "--collect-all", "yt_dlp_ejs",
        "--collect-submodules", "av",
        "--collect-data", "certifi",  # cacert.pem: macOS frozen builds need this for HTTPS to verify at all
        "--hidden-import", "web_media", "--hidden-import", "drive_audio", "--hidden-import", "transcribe_words",
        "--hidden-import", "job_worker", "--hidden-import", "skeleton_builder", "--hidden-import", "app",
        "--hidden-import", "updater",
        "--exclude-module", "torch", "--exclude-module", "matplotlib", "--exclude-module", "IPython",
    ]
    if mac:
        options += ["--osx-bundle-identifier", "com.skeletonbuilder.app"]
    else:
        tcl_dir, tk_dir = _windows_tcl_tk_dirs()
        if not tcl_dir or not tk_dir:
            raise SystemExit(f"Could not find Tcl/Tk under {Path(sys.base_prefix) / 'tcl'} - "
                              "the packaged app would launch without a usable Tk.")
        options += ["--add-data", f"{tcl_dir}{os.pathsep}_tcl_data",
                    "--add-data", f"{tk_dir}{os.pathsep}_tk_data"]
    if args.node:
        options += ["--add-binary", f"{args.node}{os.pathsep}tools"]
    PyInstaller.__main__.run(options)

    dist_dir = HERE / "dist" / ("Premiere Skeleton Builder.app" if mac else "SkeletonBuilder")
    _assert_tcl_bundled(dist_dir)
    if mac:
        _codesign_ad_hoc(dist_dir)
    # Matches paths.app_dir(): one level above the .app bundle on macOS, the exe's own
    # folder on Windows.
    _ship_default_folders(dist_dir.parent if mac else dist_dir)


if __name__ == "__main__":
    main()
