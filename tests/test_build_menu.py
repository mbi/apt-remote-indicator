"""App.build_menu: the menu model handed to AppIndicator (DBusMenu)."""

from conftest import find_item
from gi.repository import GLib


def labels(menu):
    return [item.label for item in menu.children]


def test_no_updates_shows_up_to_date(make_app):
    obj = make_app()

    menu = obj.build_menu()

    assert labels(menu)[0] == "Up to date"
    assert menu.children[0].sensitive is False  # informational, not clickable


def test_update_rows_show_old_and_new_version(make_app):
    obj = make_app()

    menu = obj.build_menu({("docker", "1.29.8-2", "1.29.9.0-1")})

    header = find_item(menu, lambda i: i.label.endswith("pending"))
    assert header.label == "1 update pending"
    (row,) = header.submenu.children
    assert row.label == "docker 1.29.8-2 → 1.29.9.0-1"
    assert row.sensitive is False


def test_update_row_without_installed_version(make_app):
    obj = make_app()

    menu = obj.build_menu({("python3-minimal", None, "3.11.2-1")})

    header = find_item(menu, lambda i: i.label.endswith("pending"))
    (row,) = header.submenu.children
    assert row.label == "python3-minimal 3.11.2-1"


def test_update_count_is_pluralized(make_app):
    obj = make_app()

    menu = obj.build_menu({("a", "1", "2"), ("b", "1", "2"), ("c", "1", "2")})

    assert labels(menu)[0] == "3 updates pending"


def test_last_checked_uses_gnome_style_date(make_app):
    obj = make_app()
    obj._last_update = GLib.DateTime.new_local(2026, 10, 9, 8, 5, 0.0)

    menu = obj.build_menu()

    item = find_item(menu, lambda i: i.label.startswith("Last checked"))
    # M/D/Y H:MM AM|PM, no zero padding (%-m, %-d, %-I).
    assert item.label == "Last checked 10/9/2026 8:05 AM"
    assert item.sensitive is False


def test_last_checked_is_omitted_before_first_poll(make_app):
    obj = make_app()
    assert obj._last_update is None

    menu = obj.build_menu()

    assert not find_item(menu, lambda i: i.label.startswith("Last checked"))


def test_update_now_only_with_pending_updates(make_app):
    obj = make_app()

    assert find_item(obj.build_menu(), lambda i: i.label == "Update now") is None

    menu = obj.build_menu({("docker", "1", "2")})
    item = find_item(menu, lambda i: i.label == "Update now")
    assert item is not None
    # Clicking it must run the configured upgrade command.
    assert item.callbacks["activate"] == obj.upgrade


def test_check_now_is_always_available(make_app):
    obj = make_app()

    for menu in (obj.build_menu(), obj.build_menu({("docker", "1", "2")})):
        item = find_item(menu, lambda i: i.label == "Check now")
        assert item.callbacks["activate"] == obj.update


def test_unlock_agent_only_when_locked(make_app):
    obj = make_app()

    assert find_item(obj.build_menu(), lambda i: i.label == "Unlock SSH Agent") is None

    locked = make_app(locked=True)
    item = find_item(locked.build_menu(), lambda i: i.label == "Unlock SSH Agent")
    assert item.callbacks["activate"] == locked.unlock_agent


def test_quit_is_always_available(make_app):
    obj = make_app()

    assert find_item(obj.build_menu(), lambda i: i.label == "Quit") is not None
