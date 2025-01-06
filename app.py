import configparser
import os
import re
import subprocess
import sys

import gi
from paramiko import AutoAddPolicy, SSHClient

gi.require_version("Notify", "0.7")
gi.require_version("AppIndicator3", "0.1")


UPDATE_COMMAND = "kitty -o hide_window_decorations=no /bin/sh -c apt-dater"

from gi.repository import Notify as notify  #  noqa
from gi.repository import AppIndicator3 as appindicator  # noqa
from gi.repository import Gtk as gtk  # noqa
from gi.repository import GLib  # noqa

APPINDICATOR_ID = "remote-apt-dater"


class App(object):
    def __init__(self, config):
        self.config = config

        self._indicator = appindicator.Indicator.new(
            APPINDICATOR_ID,
            os.path.abspath(self.config["icons"]["icon_no_updates"]),
            appindicator.IndicatorCategory.SYSTEM_SERVICES,
        )

    def build_menu(self, updates=[]):
        menu = gtk.Menu()

        for app, version in updates:
            menu.append(gtk.MenuItem(label=f"{app} {version}"))

        item_update = gtk.MenuItem(label="Update")
        item_update.connect("activate", self.update)
        menu.append(item_update)

        if updates or True:
            item_upgrade = gtk.MenuItem(label="Run upgrades")
            item_upgrade.connect("activate", self.upgrade)
            menu.append(item_upgrade)

        item_quit = gtk.MenuItem(label="Quit")
        item_quit.connect("activate", gtk.main_quit)
        menu.append(item_quit)

        menu.show_all()

        return menu

    def main(self):
        self._indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self._indicator.set_menu(self.build_menu())
        GLib.timeout_add_seconds(
            int(self.config["update"]["update_interval"]), self.update_loop
        )
        self.update()

        gtk.main()

    def update_loop(self, *args, **kwargs):
        self.update(*args, **kwargs)
        return True

    def update(self, *args, **kwargs):
        print("updating...")
        self._indicator.set_icon_full(
            os.path.abspath(self.config["icons"]["icon_no_updates"]), "updating"
        )

        ssh = SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        ssh.connect(
            self.config["ssh"]["ssh_hostname"], username=self.config["ssh"]["ssh_user"]
        )
        stdin_, stdout_, stderr_ = ssh.exec_command(
            "sudo apt-get -q -y --ignore-hold --allow-change-held-packages -s dist-upgrade"
        )
        stdout_.channel.recv_exit_status()
        lines = [
            re.match(r"Inst (?P<app>\w+) \[(?P<version>[^\]]+)\]", line).groups()
            for line in stdout_.readlines()
            if line.startswith("Inst ")
        ]
        self._indicator.set_menu(self.build_menu(lines))

        if len(lines):
            self._indicator.set_icon_full(
                os.path.abspath(self.config["icons"]["icon"]), str(len(lines))
            )
        else:
            self._indicator.set_icon_full(
                os.path.abspath(self.config["icons"]["icon_no_updates"]),
                str(len(lines)),
            )

        print("updated")

        # Avoid looping when called with timeout_add_seconds
        return None

    def upgrade(self, *args, **kwargs):
        proc = subprocess.Popen(self.config["update"]["update_command"].split(" "))
        try:
            outs, errs = proc.communicate()
        finally:
            GLib.timeout_add_seconds(1, self.update)


if __name__ == "__main__":
    try:
        config = configparser.ConfigParser()
        config.read("config.ini")

        App(config).main()
    except KeyboardInterrupt:
        notify.uninit()
        sys.exit(0)
