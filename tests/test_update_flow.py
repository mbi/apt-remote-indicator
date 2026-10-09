"""App.update: thread handoff to the main thread, and the update_loop contract."""

import types

import pytest

import app as app_module


class InlineThread:
    """Runs the worker synchronously; can defer instead, to inspect in-flight state."""

    defer = False

    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target
        self.daemon = daemon

    def start(self):
        if not InlineThread.defer:
            self.target()


class InlinePool:
    """Synchronous stand-in for ThreadPoolExecutor."""

    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def map(self, fn, iterable):
        return [fn(item) for item in iterable]


@pytest.fixture
def inline_execution(monkeypatch):
    """Polls run synchronously: no worker threads, results applied immediately."""
    InlineThread.defer = False
    monkeypatch.setattr(
        app_module, "threading", types.SimpleNamespace(Thread=InlineThread)
    )
    monkeypatch.setattr(app_module, "ThreadPoolExecutor", InlinePool)
    monkeypatch.setattr(app_module.GLib, "idle_add", lambda fn, *args: fn(*args))
    return InlineThread


def test_update_sets_attention_then_applies_results(
    make_app, stub_notify, inline_execution
):
    obj = make_app()
    obj._poll_host = lambda ssh_host: ({("docker", "1", "2")}, True)

    obj.update()

    assert (
        obj._indicator.statuses[0] == app_module.appindicator.IndicatorStatus.ATTENTION
    )
    assert obj._indicator.status == app_module.appindicator.IndicatorStatus.ACTIVE
    assert obj._indicator.label == "1"
    assert obj._polling is False
    assert obj._notification is not None


def test_update_runs_the_poll_off_the_main_thread(make_app, inline_execution):
    """The GTK main loop must not be blocked by the SSH poll."""
    obj = make_app()
    obj._poll_host = lambda ssh_host: (set(), True)
    inline_execution.defer = True  # capture the worker without running it

    obj.update()

    assert obj._polling is True  # in flight until results are applied
    assert obj._indicator.label == ""


def test_update_closes_stale_notification(make_app, inline_execution):
    obj = make_app()
    old = app_module.notify.Notification(
        "Upgrades available", "2 upgrades", "sleeping.svg"
    )
    obj._notification = old
    obj._poll_host = lambda ssh_host: (set(), True)

    obj.update()

    assert old.closed is True


def test_poll_host_exception_marks_every_host_failed(make_app, inline_execution):
    obj = make_app()

    def boom(ssh_host):
        raise OSError("connection reset")

    obj._poll_host = boom

    obj.update()

    assert obj._indicator.icon == "locked.svg"
    assert obj._ssh_agent_locked is True
    assert obj._indicator.label == ""


def test_update_loop_reschedules_itself(make_app):
    """A GLib timeout only repeats while the callback returns True."""
    obj = make_app()
    calls = []
    obj.update = lambda *args, **kwargs: calls.append(args)

    assert obj.update_loop() is True
    assert len(calls) == 1
