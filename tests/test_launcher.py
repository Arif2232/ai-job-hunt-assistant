import shutil
import subprocess
from pathlib import Path

import pytest

import launch


def test_setup_installs_once_and_reinstalls_when_requirements_change(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('example==1\n')
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[2] == 'venv':
            python = tmp_path / '.venv' / ('Scripts/python.exe' if launch.sys.platform == 'win32' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
        if command[2:4] == ['playwright', 'install']:
            (tmp_path / '.local' / 'playwright' / 'chromium-123').mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(launch.subprocess, 'run', fake_run)
    env = {'PLAYWRIGHT_BROWSERS_PATH': str(tmp_path / '.local' / 'playwright')}
    python = launch.setup(tmp_path, env)
    assert python.exists() and len(calls) == 3
    assert (tmp_path / '.local' / 'setup-version').exists()
    launch.setup(tmp_path, env)
    assert len(calls) == 3
    requirements.write_text('example==2\n')
    launch.setup(tmp_path, env)
    assert len(calls) == 5
    shutil.rmtree(tmp_path / '.local' / 'playwright')
    launch.setup(tmp_path, env)
    assert len(calls) == 6 and calls[-1][2:4] == ['playwright', 'install']


def test_failed_install_does_not_mark_setup_complete(tmp_path, monkeypatch):
    (tmp_path / 'requirements.txt').write_text('example==1\n')
    python = tmp_path / '.venv' / ('Scripts/python.exe' if launch.sys.platform == 'win32' else 'bin/python')
    python.parent.mkdir(parents=True)
    python.touch()
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(launch.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        launch.setup(tmp_path, {})
    assert not (tmp_path / '.local' / 'setup-version').exists()


def test_launcher_opens_only_a_local_server(tmp_path, monkeypatch):
    opened = []
    commands = []
    class Process:
        def __init__(self, command, **kwargs):
            commands.append((command, kwargs))
            self.status = None
        def poll(self): return self.status
        def wait(self, timeout=None):
            self.status = 0
            return 0
        def terminate(self): self.status = 0
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(launch, 'ROOT', tmp_path)
    monkeypatch.setattr(launch, 'setup', lambda root, env: Path('/fake/python'))
    monkeypatch.setattr(launch.subprocess, 'Popen', Process)
    monkeypatch.setattr(launch.urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    monkeypatch.setattr(launch.webbrowser, 'open', lambda url: opened.append(url))
    assert launch.main() == 0
    assert len(opened) == 1 and opened[0].startswith('http://127.0.0.1:')
    assert '--host' in commands[0][0] and '127.0.0.1' in commands[0][0]


def test_setup_uses_windows_virtual_environment_path(tmp_path, monkeypatch):
    (tmp_path / 'requirements.txt').write_text('example==1\n')
    monkeypatch.setattr(launch.sys, 'platform', 'win32')
    def fake_run(command, **kwargs):
        if command[2] == 'venv':
            python = tmp_path / '.venv' / 'Scripts' / 'python.exe'
            python.parent.mkdir(parents=True)
            python.touch()
        if command[2:4] == ['playwright', 'install']:
            (tmp_path / '.local' / 'playwright' / 'chromium-123').mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(launch.subprocess, 'run', fake_run)
    assert launch.setup(tmp_path, {}) == tmp_path / '.venv' / 'Scripts' / 'python.exe'
