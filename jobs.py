"""Persistent jobs independent of the desktop window."""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from paths import FROZEN, ROOT, cache_dir, default_data_root

SETTINGS = cache_dir() / 'desktop-settings.json'


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
        if os.name == "nt":
            # os.kill(pid, 0) would TERMINATE the process on Windows, so query it instead.
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        return bool(status) and not status.startswith("Z")
    except (OSError, ValueError):
        return False


def inspect_run(folder):
    folder = Path(folder)
    state = read_json(folder/'run.json')
    result = Path(state.get('result', folder))
    active = state.get('status') in ('running', 'starting') and alive(state.get('pid'))
    if active:
        status = 'Running'
    elif state.get('status') == 'starting' and (datetime.datetime.now().timestamp()-state.get('created', 0)) < 15:
        status = 'Starting'
    elif state.get('status') == 'failed':
        status = 'Failed — see log'
    elif (result/'Skeleton_full.xml').exists() and (result/'START HERE.txt').exists():
        manifest = read_json(result/'manifest.json')
        notes = manifest.get('warnings', []) + [n for c in manifest.get('cues', []) for n in c.get('review', [])]
        status = 'Completed — review notes' if notes else 'Completed'
    else:
        status = 'Interrupted / incomplete'
    return {**state, 'folder': str(folder), 'result': str(result), 'display_status': status}


# Self-contained by default: Projects/Media/Models live next to the app itself
# (the .exe and its "_internal" folder on Windows, or the .app bundle on macOS),
# not tucked away in the user's Documents folder. Configurable in Paths & Options.
DEFAULT_PROJECTS = default_data_root() / 'Projects'
DEFAULT_MEDIA = default_data_root() / 'Media'
DEFAULT_MODELS = default_data_root() / 'Models'  # speech-recognition model files (downloaded on first use)


def projects_dir(settings=None):
    """Folder that holds one sub-folder per run; configurable in the Paths tab."""
    chosen = (settings if settings is not None else read_json(SETTINGS)).get('projects_dir')
    return Path(chosen) if chosen else DEFAULT_PROJECTS


def runs(root=None):
    root = Path(root) if root else projects_dir()
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (p/'run.json').exists()]
    return [inspect_run(p) for p in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)]


def safe_name(title, limit=80):
    cleaned = ''.join('_' if c in '<>:"/|?*' + chr(92) or ord(c) < 32 else c for c in str(title)).strip(' .')
    return cleaned[:limit].strip(' .') or 'Untitled script'


def unique_folder(root, title):
    root = Path(root)
    base = safe_name(title)
    folder, n = root/base, 2
    while folder.exists():
        folder, n = root/f'{base} ({n})', n + 1
    return folder


def transfer_folder_contents(old_dir, new_dir):
    """Move everything from old_dir into new_dir (merging same-named sub-folders one level
    deep). Used when the user points a Locations setting at a new folder. Returns a list of
    per-item error strings; an empty list means everything moved cleanly."""
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    if not old_dir.is_dir() or old_dir.resolve() == new_dir.resolve():
        return []
    new_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for item in list(old_dir.iterdir()):
        target = new_dir / item.name
        try:
            if target.exists():
                if target.is_dir() and item.is_dir():
                    for sub in list(item.iterdir()):
                        shutil.move(str(sub), str(target / sub.name))
                    item.rmdir()
                else:
                    errors.append(f'{item.name}: already exists at the destination, left in place')
            else:
                shutil.move(str(item), str(target))
        except OSError as error:
            errors.append(f'{item.name}: {error}')
    try:
        old_dir.rmdir()  # only succeeds if now empty
    except OSError:
        pass
    return errors


QUEUE = cache_dir() / 'queue.json'


def read_queue():
    return read_json(QUEUE, default=[])


def write_queue(items):
    write_json(QUEUE, items)


def enqueue(config):
    """Add a fully-resolved build config to the queue; advanced automatically once the
    current run (if any) finishes. Returns the new entry's id (for removal)."""
    items = read_queue()
    entry = {'id': uuid.uuid4().hex, 'title': config.get('title') or 'Untitled',
              'added': datetime.datetime.now().timestamp(), 'config': config}
    items.append(entry)
    write_queue(items)
    return entry['id']


def dequeue_next():
    items = read_queue()
    if not items:
        return None
    entry = items.pop(0)
    write_queue(items)
    return entry


def remove_from_queue(entry_id):
    write_queue([i for i in read_queue() if i['id'] != entry_id])


def stop_job(folder):
    """Kill a running job's whole process tree (the detached supervisor and the builder
    subprocess it spawned) and mark the run failed so it stops showing as active. There
    is no graceful cancellation point inside the pipeline, so this is an immediate kill,
    not a request the job can decline."""
    folder = Path(folder)
    state = read_json(folder / 'run.json')
    pid = state.get('pid')
    if pid and alive(pid):
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
            else:
                import signal
                os.killpg(int(pid), signal.SIGKILL)  # job_worker runs start_new_session=True,
                                                       # so its pid is also its process group id
        except OSError:
            pass
    state.update(status='failed', error='Stopped by user')
    write_json(folder / 'run.json', state)


def run_layout(folder, media_root=None):
    """Common structure for every run."""
    folder = Path(folder)
    media = Path(media_root)/folder.name if media_root else folder/'Media'
    return {'script': folder/'Script', 'audio': folder/'Audio', 'timeline': folder/'Timeline', 'media': media}


def start_job(config):
    root = Path(config['projects_dir']) if config.get('projects_dir') else projects_dir()
    if any(r['display_status'] in ('Running', 'Starting') for r in runs(root)):
        raise ValueError('A run is already working. Select it below to see its progress.')
    folder = unique_folder(root, config.get('title') or Path(config['docx']).stem)
    layout = run_layout(folder, config.get('media_dir'))
    for path in (folder, *layout.values()):
        path.mkdir(parents=True, exist_ok=True)
    state = {'status':'starting', 'created':datetime.datetime.now().timestamp(), 'config':config, 'result':str(layout['timeline'])}
    write_json(folder/'run.json', state)
    env = {**os.environ, 'PYTHONUNBUFFERED':'1', 'PYTHONIOENCODING':'utf-8'}
    env['PATH'] = str(Path.home()/'.local/share/skeleton-builder-tools/runtime/bin')+os.pathsep+env.get('PATH','')
    try:
        options = ({'creationflags':subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session':True})
        # A packaged app re-runs itself in worker mode; from source it runs job_worker.py.
        worker = [sys.executable, '--worker', str(folder)] if FROZEN else [sys.executable, str(ROOT/'job_worker.py'), str(folder)]
        with (folder/'run.log').open('ab') as log:
            subprocess.Popen(worker, cwd=folder,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **options)
    except Exception as error:
        write_json(folder/'run.json', {**state, 'status':'failed', 'error':str(error)})
        raise
    return folder
