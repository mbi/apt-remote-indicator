"""App._apply_results: aggregate host results into tray state."""

from conftest import find_item

import app as app_module


def test_all_hosts_rejecting_auth_locks_the_indicator(make_app):
    obj = make_app()

    obj._apply_results([(set(), "auth"), (set(), "auth")])

    assert obj._indicator.icon == "locked.svg"
    assert obj._ssh_agent_locked is True
    assert obj._indicator.label == ""
    # "Unlock SSH Agent" is offered while locked.
    assert find_item(obj._indicator.menu, lambda i: i.label == "Unlock SSH Agent")


def test_unreachable_hosts_do_not_offer_an_unlock(make_app):
    """A poll fired before the network is back must not blame the agent."""
    obj = make_app()

    obj._apply_results([(set(), "other"), (set(), "other")])

    assert obj._indicator.icon == "unreachable.svg"
    assert obj._ssh_agent_locked is False
    assert not find_item(obj._indicator.menu, lambda i: i.label == "Unlock SSH Agent")


def test_one_auth_failure_among_unreachable_hosts_still_offers_unlock(make_app):
    obj = make_app()

    obj._apply_results([(set(), "other"), (set(), "auth")])

    assert obj._ssh_agent_locked is True
    assert find_item(obj._indicator.menu, lambda i: i.label == "Unlock SSH Agent")


def test_partial_failure_with_updates_succeeds(make_app, stub_notify):
    obj = make_app()

    obj._apply_results(
        [({("docker", "1.29.8-2", "1.29.9.0-1")}, None), (set(), "other")]
    )

    assert obj._indicator.icon == "sleeping.svg"
    assert obj._ssh_agent_locked is False
    # Partial failure must not surface a locked/failed state.
    assert not find_item(obj._indicator.menu, lambda i: i.label == "Unlock SSH Agent")
    assert obj._indicator.label == "1"


def test_successful_poll_with_updates_notifies_and_labels(make_app, stub_notify):
    obj = make_app()

    obj._apply_results([({("docker", "1.29.8-2", "1.29.9.0-1")}, None)])

    assert obj._indicator.label == "1"
    (notification,) = stub_notify.instances
    assert notification.title == "Upgrades available"
    assert notification.body == "1 upgrades ready to install"
    assert notification.shown is True
    assert "activate" in notification.actions


def test_successful_poll_without_updates_is_quiet(make_app, stub_notify):
    obj = make_app()

    obj._apply_results([(set(), None), (set(), None)])

    assert obj._indicator.icon == "sleeping.svg"
    assert obj._indicator.label == ""
    assert stub_notify.instances == []
    assert find_item(obj._indicator.menu, lambda i: i.label == "Up to date")


def test_updates_from_multiple_hosts_are_merged_and_deduped(make_app):
    obj = make_app()

    obj._apply_results(
        [
            ({("docker", "1", "2"), ("uv", "0.12.23", "0.12.24")}, None),
            ({("docker", "1", "2")}, None),  # same update on a second host
        ]
    )

    assert obj._indicator.label == "2"
    header = find_item(obj._indicator.menu, lambda i: i.label.endswith("pending"))
    assert header.label == "2 updates pending"


def test_polling_flag_is_cleared(make_app):
    obj = make_app()
    obj._polling = True

    obj._apply_results([(set(), None)])

    assert obj._polling is False
    assert obj._indicator.status == app_module.appindicator.IndicatorStatus.ACTIVE
    assert obj._last_update is not None


def test_all_hosts_rejecting_auth_recloses_the_auth_gate(make_app):
    """A relocked agent must be probed serially by the next poll again."""
    obj = make_app()
    obj._auth_ready.set()

    obj._apply_results([(set(), "auth"), (set(), "auth")])

    assert not obj._auth_ready.is_set()


def test_partial_auth_failure_keeps_the_gate_open(make_app):
    """One host rejecting us says nothing about the agent being locked."""
    obj = make_app()
    obj._auth_ready.set()

    obj._apply_results([(set(), "auth"), (set(), None)])

    assert obj._auth_ready.is_set()
