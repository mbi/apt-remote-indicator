"""Resume-from-sleep handling: probe the SSH agent, then unlock or poll."""

import types

import pytest

import app as app_module


def fake_params(sleeping):
    return types.SimpleNamespace(unpack=lambda: (sleeping,))


def signal(app, sleeping):
    app._on_prepare_for_sleep(None, None, None, None, None, fake_params(sleeping))


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.communicated = False

    def communicate(self):
        self.communicated = True
        return (None, None)


class ArgForwardingThread:
    """Runs the thread target synchronously, forwarding the stored args."""

    def __init__(self, target=None, daemon=None, args=(), **kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def resume_app(make_app, inline_execution, monkeypatch):
    """App with a controllable agent probe and recorded command launches."""
    application = make_app()
    launched = []
    state = {"identities": True, "timeouts": []}
    monkeypatch.setattr(
        app_module.subprocess,
        "Popen",
        lambda argv, *a, **k: launched.append(FakeProc(argv)) or launched[-1],
    )
    monkeypatch.setattr(app_module.threading, "Thread", ArgForwardingThread)
    monkeypatch.setattr(
        app_module.GLib,
        "timeout_add_seconds",
        lambda delay, callback: state["timeouts"].append((delay, callback)),
    )
    monkeypatch.setattr(
        app_module, "ssh_agent_has_identities", lambda: state["identities"]
    )
    application._poll_host = lambda ssh_host: (set(), None)
    return application, launched, state


def test_agent_probe_distinguishes_unknown_from_locked(monkeypatch):
    """Only a definite "no identities" may trigger an unlock terminal."""
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1),
    )
    assert app_module.ssh_agent_has_identities() is False

    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0),
    )
    assert app_module.ssh_agent_has_identities() is True

    # ssh-add exits 2 when it cannot reach the agent at all.
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=2),
    )
    assert app_module.ssh_agent_has_identities() is None

    def missing(*args, **kwargs):
        raise FileNotFoundError("ssh-add")

    monkeypatch.setattr(app_module.subprocess, "run", missing)
    assert app_module.ssh_agent_has_identities() is None


def run_delayed_poll(obj, state):
    ((delay, callback),) = state["timeouts"]
    assert delay == app_module.RESUME_POLL_DELAY
    assert callback == obj._resume_poll
    callback()


def test_resume_with_loaded_agent_polls_after_a_grace_period(resume_app):
    """Wake-up polls are deferred: Wi-Fi/DNS are not back the instant we wake."""
    obj, launched, state = resume_app
    signal(obj, False)

    assert launched == []
    assert obj._last_update is None  # not polled yet

    run_delayed_poll(obj, state)

    assert obj._last_update is not None
    assert obj._indicator.statuses == [
        app_module.appindicator.IndicatorStatus.ATTENTION,
        app_module.appindicator.IndicatorStatus.ACTIVE,
    ]


def test_resume_with_locked_agent_launches_unlock(resume_app):
    """Hibernation dropped the keys: run the configured unlock command."""
    obj, launched, state = resume_app
    state["identities"] = False
    signal(obj, False)

    assert [proc.argv for proc in launched] == [["/bin/true"]]
    assert launched[0].communicated
    assert obj._ssh_agent_locked is True
    assert obj._indicator.icon == "locked.svg"
    assert obj._last_update is None  # the poll waits for the unlock to finish
    assert obj._indicator.statuses == []


def test_resume_with_unknown_agent_state_polls(resume_app):
    """An unusable probe must not pop an unlock terminal."""
    obj, launched, state = resume_app
    state["identities"] = None
    signal(obj, False)

    assert launched == []
    assert obj._last_update is None
    run_delayed_poll(obj, state)
    assert obj._last_update is not None


def test_resume_without_unlock_command_polls(resume_app, monkeypatch):
    obj, launched, state = resume_app
    del obj._config["update"]["unlock_agent_command"]
    monkeypatch.setattr(
        app_module,
        "ssh_agent_has_identities",
        lambda: pytest.fail("agent probed without an unlock command configured"),
    )
    signal(obj, False)

    assert launched == []
    assert obj._last_update is None
    run_delayed_poll(obj, state)
    assert obj._last_update is not None


def test_going_to_sleep_does_not_probe_or_update(resume_app, monkeypatch):
    obj, _launched, _state = resume_app
    monkeypatch.setattr(
        app_module,
        "ssh_agent_has_identities",
        lambda: pytest.fail("agent probed while going to sleep"),
    )
    signal(obj, True)

    assert obj._last_update is None
    assert obj._indicator.statuses == []


def test_resume_skipped_while_poll_in_flight(resume_app, monkeypatch):
    """A poll already running must not be duplicated by the wake signal."""
    obj, _launched, _state = resume_app
    obj._polling = True
    monkeypatch.setattr(
        app_module,
        "ssh_agent_has_identities",
        lambda: pytest.fail("agent probed while a poll is in flight"),
    )
    signal(obj, False)

    assert obj._last_update is None
    assert obj._indicator.statuses == []
    assert obj._polling is True


def test_resume_recloses_the_auth_gate(resume_app):
    """Keys can lock while suspended; the next login must be serialized."""
    obj, _launched, _state = resume_app
    obj._auth_ready.set()

    signal(obj, False)

    assert not obj._auth_ready.is_set()
