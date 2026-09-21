"""Detached supervisor; logs and completion survive closing the app."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from drive_audio import download_drive_audio, is_drive_url
from paths import FROZEN, NO_WINDOW, cache_dir
from jobs import ROOT, read_json, write_json, run_layout


def stage(source, directory):
    """Copy an input into the run's own folder so the run is self-contained."""
    source, target = Path(source), Path(directory)/Path(source).name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target


def run(folder):
    folder = Path(folder)
    state = read_json(folder/'run.json')
    state.update(status='running', pid=os.getpid())
    write_json(folder/'run.json', state)
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        with (cache_dir()/'worker.lock').open('a+b') as lock:
            try:
                if os.name == 'nt':
                    import msvcrt
                    lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Another build is already running. Retry after it finishes.')
            c = state['config']
            layout = run_layout(folder, c.get('media_dir'))
            for path in layout.values():
                path.mkdir(parents=True, exist_ok=True)
            docx = stage(c['docx'], layout['script'])
            audio = c['audio']
            if is_drive_url(audio):
                audio = download_drive_audio(audio, layout['audio'])
            else:
                audio = stage(audio, layout['audio'])
            # A packaged app has no separate script files: it re-runs itself in builder mode.
            launcher = [sys.executable, '--builder'] if FROZEN else [sys.executable, '-u', str(ROOT/'skeleton_builder.py')]
            command = launcher + ['--docx', str(docx),
                       '--audio', str(audio), '--output', state['result'],
                       '--media-dir', str(layout['media']), '--audio-dir', str(layout['audio']),
                       '--whisper-model', c.get('whisper_model', 'small.en'), '--device', c.get('device', 'auto')]
            if c.get('models_dir'): command += ['--models-dir', c['models_dir']]
            if c.get('videos', True):
                command += ['--download-videos', '--handles', str(c['buffer_minutes']*60),
                            '--full-video-limit', str(c['limit_minutes']*60)]
            if c.get('width'): command += ['--width', str(c['width'])]
            if c.get('height'): command += ['--height', str(c['height'])]
            print('Working folder:', folder, flush=True)
            print('You may close and reopen the app; this run continues in the background.', flush=True)
            result = subprocess.run(command, stdin=subprocess.DEVNULL, **NO_WINDOW)
            state.update(status='completed' if result.returncode == 0 else 'failed', exit_code=result.returncode)
            print('Build completed.' if result.returncode == 0 else 'Build failed. Details are above.', flush=True)
    except BaseException as error:
        state.update(status='failed', error=str(error))
        traceback.print_exc()
    finally:
        write_json(folder/'run.json', state)

if __name__ == '__main__':
    run(sys.argv[1])
