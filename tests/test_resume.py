"""Resume-from-sleep handling: logind's PrepareForSleep triggers an update."""

import types

import app as app_module


def fake_params(sleeping):
    return types.SimpleNamespace(unpack=lambda: (sleeping,))


def signal(app, sleeping):
    app._on_prepare_for_sleep(None, None, None, None, None, fake_params(sleeping))


def test_resume_triggers_update(make_app, stub_notify, inline_execution):
    """Waking up runs a poll, which completes inline and records a check time."""
    obj = make_app()
    signal(obj, False)

    assert obj._last_update is not None
    assert obj._indicator.statuses == [
        app_module.appindicator.IndicatorStatus.ATTENTION,
        app_module.appindicator.IndicatorStatus.ACTIVE,
    ]


def test_going_to_sleep_does_not_update(make_app, stub_notify, inline_execution):
    obj = make_app()
    signal(obj, True)

    assert obj._last_update is None
    assert obj._indicator.statuses == []


def test_resume_skipped_while_poll_in_flight(make_app, stub_notify, inline_execution):
    """A poll already running must not be duplicated by the wake signal."""
    obj = make_app()
    obj._polling = True
    signal(obj, False)

    assert obj._last_update is None
    assert obj._indicator.statuses == []
    assert obj._polling is True
