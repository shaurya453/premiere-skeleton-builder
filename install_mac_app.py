"""Create a local Mac app launcher after completing README setup."""
from pathlib import Path
import plistlib
import shlex
import sys


def install(root=None, shortcuts=True):
    root = Path(root or Path(__file__).resolve().parent).resolve()
    if sys.platform != 'darwin':
        raise SystemExit('This launcher installer is for macOS. Use app.py or the Windows launcher elsewhere.')
    if not (root/'.venv/bin/python').exists():
        raise SystemExit('Create .venv and install requirements first; see README.md.')
    app = root/'Skeleton Builder.app'
    contents = app/'Contents'
    (contents/'MacOS').mkdir(parents=True,exist_ok=True)
    (contents/'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleName':'Skeleton Builder','CFBundleDisplayName':'Skeleton Builder',
        'CFBundleIdentifier':'local.skeletonbuilder.desktop','CFBundleVersion':'1.0',
        'CFBundleShortVersionString':'1.0','CFBundleExecutable':'SkeletonBuilder',
        'CFBundlePackageType':'APPL','NSHighResolutionCapable':True}))
    launcher=contents/'MacOS/SkeletonBuilder'
    launcher.write_text('#!/bin/bash\nset -e\ncd '+shlex.quote(str(root))+'\n'
        'export PATH="$HOME/.local/share/skeleton-builder-tools/runtime/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
        'mkdir -p .cache\nexec .venv/bin/python app.py >> .cache/desktop.log 2>&1\n')
    launcher.chmod(0o755)
    if shortcuts:
        for directory in (Path.home()/'Desktop',Path.home()/'Applications'):
            directory.mkdir(exist_ok=True)
            link=directory/app.name
            if not link.exists() and not link.is_symlink(): link.symlink_to(app)
    print('Created:',app)
    return app


if __name__=='__main__':
    install()
