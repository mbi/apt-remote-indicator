"""Launched commands are parsed shell-style and never block the GTK main loop.

Regression: shlex.split(..., posix=False) kept the quote characters, so
`/bin/sh -c "cmd --flag"` received one quoted word and exited 127
("No such file or directory") instead of running the command. And
communicate() used to run on the main thread, freezing the tray for as long
as the launched terminal window stayed open.
"""

import threading
import types

import pytest

import app as app_module


class FakeProc:
    def __init__(self, argv=None):
        self.argv = argv
        self.communicated = False

    def communicate(self):
        self.communicated = True
        return (None, None)


class InlineThread:
    """Runs the worker synchronously so argv assertions need no waiting."""

    def __init__(self, target=None, daemon=None, args=(), **kwargs):
        self.target = target
        self.daemon = daemon
        self.args = args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def command_app(make_app, monkeypatch):
    """App whose launched commands are recorded and whose timers are inert."""
    launched = []
    monkeypatch.setattr(
        app_module.subprocess,
        "Popen",
        lambda argv, *a, **k: launched.append(FakeProc(argv)) or launched[-1],
    )
    monkeypatch.setattr(app_module.threading, "Thread", InlineThread)
    monkeypatch.setattr(app_module.GLib, "timeout_add_seconds", lambda *a: None)
    return make_app(), launched


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("upgrade", "upgrade_command"),
        ("unlock_agent", "unlock_agent_command"),
    ],
)
def test_command_quotes_are_stripped(command_app, method, key):
    application, launched = command_app
    application._config["update"][key] = (
        'kitty -o hide_window_decorations=no /bin/sh -c "apt-dater --refresh-all-on-launch"'
    )

    getattr(application, method)()

    (proc,) = launched
    assert proc.argv == [
        "kitty",
        "-o",
        "hide_window_decorations=no",
        "/bin/sh",
        "-c",
        "apt-dater --refresh-all-on-launch",
    ]
    assert proc.communicated


class BlockingProc:
    """communicate() blocks until the test releases it."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.communicated = False

    def communicate(self):
        self.started.set()
        self.release.wait(timeout=10)
        self.communicated = True
        return (None, None)


def test_upgrade_does_not_wait_for_the_command(make_app, monkeypatch):
    proc = BlockingProc()
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda argv, *a, **k: proc)

    refreshed = threading.Event()
    scheduled = {}

    def fake_timeout(delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback
        refreshed.set()

    monkeypatch.setattr(
        app_module.GLib, "idle_add", lambda fn, *args: (fn(*args), False)
    )
    monkeypatch.setattr(app_module.GLib, "timeout_add_seconds", fake_timeout)

    application = make_app()
    application.update = types.MethodType(lambda self, *a, **k: None, application)

    application.upgrade()

    assert proc.started.wait(timeout=5), "command was never launched"
    assert not proc.communicated, "upgrade() blocked until the command exited"

    proc.release.set()
    assert refreshed.wait(timeout=5), (
        "no refresh was scheduled after the command exited"
    )
    assert scheduled["callback"] == application.update
    assert scheduled["delay"] == 1
