"""Shared fixtures for the applet test suite.

The suite runs headless: no X/Wayland display, no session bus needed for the
covered code paths, no real SSH, no notifications. GTK/Notify interaction is
stubbed (AppIndicator menus are exported over DBusMenu anyway, so the tests
assert on the menu *model* we build, not on rendered pixels).
"""

import configparser
import types
from typing import ClassVar

import pytest

import app as app_module


class FakeWidget:
    """Stands in for Gtk.MenuItem / Gtk.Menu / separators."""

    def __init__(self, label=None):
        self.label = label
        self.sensitive = True
        self.submenu = None
        self.callbacks = {}
        self.children = []

    def set_sensitive(self, value):
        self.sensitive = value

    def set_submenu(self, submenu):
        self.submenu = submenu

    def connect(self, signal, callback):
        self.callbacks[signal] = callback

    def append(self, child):
        self.children.append(child)

    def show_all(self):
        pass


class FakeSeparator(FakeWidget):
    @staticmethod
    def new():
        return FakeSeparator()


class FakeGtk(types.SimpleNamespace):
    Menu = FakeWidget
    MenuItem = FakeWidget
    SeparatorMenuItem = FakeSeparator
    main_quit = staticmethod(lambda: None)


class FakeNotification:
    instances: ClassVar[list[FakeNotification]] = []

    @classmethod
    def new(cls, title, body, icon):
        return cls(title, body, icon)

    def __init__(self, title, body, icon):
        self.title = title
        self.body = body
        self.icon = icon
        self.actions = {}
        self.shown = False
        self.closed = False
        FakeNotification.instances.append(self)

    def add_action(self, name, label=None, callback=None):
        self.actions[name] = (label, callback)

    def show(self):
        self.shown = True

    def close(self):
        self.closed = True


class FakeIndicator:
    def __init__(self):
        self.calls = []
        self.statuses = []
        self.icon = None
        self.icon_description = None
        self.label = None
        self.status = None
        self.menu = None

    def set_icon_full(self, icon, description):
        self.calls.append("set_icon_full")
        self.icon = icon
        self.icon_description = description

    def set_attention_icon_full(self, icon, description):
        self.calls.append("set_attention_icon_full")

    def set_label(self, text, guide):
        self.calls.append("set_label")
        self.label = text

    def set_status(self, status):
        self.calls.append("set_status")
        self.statuses.append(status)
        self.status = status

    def set_menu(self, menu):
        self.calls.append("set_menu")
        self.menu = menu


@pytest.fixture(autouse=True)
def stub_gtk(monkeypatch):
    """Replace the GTK module used by app.py with lightweight fakes."""
    monkeypatch.setattr(app_module, "gtk", FakeGtk)


@pytest.fixture(autouse=True)
def stub_notify(monkeypatch):
    """Replace libnotify with a recording fake."""
    FakeNotification.instances = []
    monkeypatch.setattr(
        app_module,
        "notify",
        types.SimpleNamespace(
            Notification=FakeNotification,
            init=lambda *_args: None,
            uninit=lambda *_args: None,
        ),
    )
    return FakeNotification


@pytest.fixture
def make_app():
    """Build an App without touching AppIndicator/Notify/config.ini."""

    def _make(hosts="mbi@host1,mbi@host2", locked=False):
        obj = app_module.App.__new__(app_module.App)
        obj._config = configparser.ConfigParser()
        obj._config.read_string(
            "[ssh]\n"
            f"ssh_hosts = {hosts}\n"
            "[update]\n"
            "upgrade_command = /bin/true\n"
            "unlock_agent_command = /bin/true\n"
            "update_interval = 1200\n"
        )
        obj._indicator = FakeIndicator()
        obj._ssh_config = None
        obj._ssh_agent_locked = locked
        obj._last_update = None
        obj._polling = False
        obj._notification = None
        return obj

    return _make


def menu_labels(menu):
    """Flat list of item labels in menu order (submenus included as items)."""
    return [item.label for item in menu.children]


def find_item(menu, predicate):
    for item in menu.children:
        if item.label is not None and predicate(item):
            return item
    return None
