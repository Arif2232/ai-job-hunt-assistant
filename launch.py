"""Start the local app, installing its dependencies on first use."""
import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def setup(root, env):
    data = root / '.local'
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = root / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not python.exists():
        print('Setting up the app for the first time...', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(root / '.venv')], check=True, cwd=root, env=env)
    requirements = root / 'requirements.txt'
    version = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = data / 'setup-version'
    installed = marker.exists() and marker.read_text().strip() == version
    if not installed:
        print('Installing app dependencies. This can take a few minutes...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)], check=True, cwd=root, env=env)
    browsers = data / 'playwright'
    if not installed or not any(browsers.glob('chromium-[0-9]*')):
        print('Preparing the application browser...', flush=True)
        subprocess.run([str(python), '-m', 'playwright', 'install', 'chromium'], check=True, cwd=root, env=env)
    marker.write_text(version)
    return python


def main():
    if sys.version_info < (3, 11):
        print('Please install Python 3.11 or newer, then start the app again.', flush=True)
        return 1
    env = os.environ.copy()
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.local' / 'playwright')
    process = None
    try:
        python = setup(ROOT, env)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}/'
        process = subprocess.Popen([str(python), '-m', 'uvicorn', 'app.server:app', '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'], cwd=ROOT, env=env)
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError('The app could not start.')
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.5)
        else:
            raise RuntimeError('The app did not start in time.')
        print(f'Opening {url}\nKeep this window open while using the app. Press Ctrl+C to stop.', flush=True)
        webbrowser.open(url)
        return process.wait()
    except KeyboardInterrupt:
        print('\nStopping the app.', flush=True)
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f'Could not start the app: {exc}', flush=True)
        return 1
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
