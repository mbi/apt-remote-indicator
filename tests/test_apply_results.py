"""App._apply_results: aggregate host results into tray state."""

from conftest import find_item

import app as app_module


def test_all_hosts_failed_locks_the_indicator(make_app):
    obj = make_app()

    obj._apply_results([(set(), False), (set(), False)])

    assert obj._indicator.icon == "locked.svg"
    assert obj._ssh_agent_locked is True
    assert obj._indicator.label == ""
    # "Unlock SSH Agent" is offered while locked.
    assert find_item(obj._indicator.menu, lambda i: i.label == "Unlock SSH Agent")


def test_partial_failure_with_updates_succeeds(make_app, stub_notify):
    obj = make_app()

    obj._apply_results([({("docker", "1.29.8-2", "1.29.9.0-1")}, True), (set(), False)])

    assert obj._indicator.icon == "sleeping.svg"
    assert obj._ssh_agent_locked is False
    # Partial failure must not surface a locked/failed state.
    assert not find_item(obj._indicator.menu, lambda i: i.label == "Unlock SSH Agent")
    assert obj._indicator.label == "1"


def test_successful_poll_with_updates_notifies_and_labels(make_app, stub_notify):
    obj = make_app()

    obj._apply_results([({("docker", "1.29.8-2", "1.29.9.0-1")}, True)])

    assert obj._indicator.label == "1"
    (notification,) = stub_notify.instances
    assert notification.title == "Upgrades available"
    assert notification.body == "1 upgrades ready to install"
    assert notification.shown is True
    assert "activate" in notification.actions


def test_successful_poll_without_updates_is_quiet(make_app, stub_notify):
    obj = make_app()

    obj._apply_results([(set(), True), (set(), True)])

    assert obj._indicator.icon == "sleeping.svg"
    assert obj._indicator.label == ""
    assert stub_notify.instances == []
    assert find_item(obj._indicator.menu, lambda i: i.label == "Up to date")


def test_updates_from_multiple_hosts_are_merged_and_deduped(make_app):
    obj = make_app()

    obj._apply_results(
        [
            ({("docker", "1", "2"), ("uv", "0.12.23", "0.12.24")}, True),
            ({("docker", "1", "2")}, True),  # same update on a second host
        ]
    )

    assert obj._indicator.label == "2"
    header = find_item(obj._indicator.menu, lambda i: i.label.endswith("pending"))
    assert header.label == "2 updates pending"


def test_polling_flag_is_cleared(make_app):
    obj = make_app()
    obj._polling = True

    obj._apply_results([(set(), True)])

    assert obj._polling is False
    assert obj._indicator.status == app_module.appindicator.IndicatorStatus.ACTIVE
    assert obj._last_update is not None
