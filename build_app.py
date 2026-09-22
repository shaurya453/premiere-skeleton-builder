"""Build the packaged app with PyInstaller.

  python build_app.py            -> dist/SkeletonBuilder/ (Windows) or dist/Premiere Skeleton Builder.app (macOS)
  python build_app.py --node X   also bundles the Node.js executable X (needed for YouTube downloads)

PyInstaller cannot cross-compile: build on Windows for Windows and on a Mac for macOS
(the GitHub workflow in .github/workflows/build.yml does both).
"""
import argparse
import os
from pathlib import Path
import sys

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", help="path to a Node.js executable to bundle")
    args = parser.parse_args()
    mac = sys.platform == "darwin"
    options = [
        str(HERE / "skeleton_app.py"),
        "--name", "Premiere Skeleton Builder" if mac else "SkeletonBuilder",
        "--noconfirm", "--clean", "--windowed", "--onedir",
        "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
        "--paths", str(HERE),
        "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "onnxruntime",
        "--collect-all", "tkinterdnd2", "--collect-all", "imageio_ffmpeg",
        "--collect-all", "yt_dlp", "--collect-all", "yt_dlp_ejs",
        "--collect-submodules", "av",
        "--collect-data", "certifi",  # cacert.pem: macOS frozen builds need this for HTTPS to verify at all
        "--hidden-import", "web_media", "--hidden-import", "drive_audio", "--hidden-import", "transcribe_words",
        "--hidden-import", "job_worker", "--hidden-import", "skeleton_builder", "--hidden-import", "app",
        "--exclude-module", "torch", "--exclude-module", "matplotlib", "--exclude-module", "IPython",
    ]
    if mac:
        options += ["--osx-bundle-identifier", "com.skeletonbuilder.app"]
    if args.node:
        options += ["--add-binary", f"{args.node}{os.pathsep}tools"]
    PyInstaller.__main__.run(options)


if __name__ == "__main__":
    main()
