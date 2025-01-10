import configparser
import logging
import os
import re
import shlex
import subprocess
import sys

import gi
from gi.repository import AppIndicator3 as appindicator  # noqa
from gi.repository import GLib  # noqa
from gi.repository import Gtk as gtk  # noqa
from gi.repository import Notify as notify  # noqa
from paramiko import AutoAddPolicy, SSHClient
from systemd.journal import JournalHandler

gi.require_version("Notify", "0.7")  # noqa
gi.require_version("AppIndicator3", "0.1")  # noqa


APPINDICATOR_ID = "remote-apt-dater"

logger = logging.getLogger(APPINDICATOR_ID)
logger.addHandler(JournalHandler(SYSLOG_IDENTIFIER=APPINDICATOR_ID))
logger.setLevel(logging.INFO)


class App(object):
    def __init__(self):
        self._config = configparser.ConfigParser()
        self._config.read(os.path.join(os.path.dirname(__file__), "config.ini"))

        if self._config["update"].get("ssh_agent_socket"):
            os.environ["SSH_AUTH_SOCK"] = self._config["update"].get("ssh_agent_socket")

        self._indicator = appindicator.Indicator.new_with_path(
            APPINDICATOR_ID,
            "sleeping.svg",
            appindicator.IndicatorCategory.SYSTEM_SERVICES,
            os.path.join(os.path.dirname(__file__)),
        )

        self._indicator.set_attention_icon_full("updating.svg", "Updating")
        self._ssh_agent_locked = False
        logger.info("Startup complete")
        self._last_update = None

        notify.init(APPINDICATOR_ID)
        self._notification = None

    def build_menu(self, updates=[]):
        menu = gtk.Menu()

        if updates:
            mi = gtk.MenuItem(label=f"{len(updates)} update(s) pending")
            menu.append(mi)
            submenu = gtk.Menu()
            for update in updates:
                app, version = update
                smi = gtk.MenuItem(label=f"{app} {version}")
                smi.set_sensitive(False)
                submenu.append(smi)
            mi.set_submenu(submenu)
        else:
            mi = gtk.MenuItem(label="Up to date")
            mi.set_sensitive(False)
            menu.append(mi)

        if self._last_update:
            updated_time = GLib.DateTime.format(self._last_update, "%c")
            updated_item = gtk.MenuItem(label=f"Last checked {updated_time}")
            updated_item.set_sensitive(False)
            menu.append(updated_item)
        menu.append(gtk.SeparatorMenuItem.new())

        if updates:
            item_upgrade = gtk.MenuItem(label="Update now")
            item_upgrade.connect("activate", self.upgrade)
            menu.append(item_upgrade)

        item_update = gtk.MenuItem(label="Check now")
        item_update.connect("activate", self.update)
        menu.append(item_update)

        if self._ssh_agent_locked:
            item_unlock = gtk.MenuItem(label="Unlock SSH Agent")
            item_unlock.connect("activate", self.unlock_agent)
            menu.append(item_unlock)

        menu.append(gtk.SeparatorMenuItem.new())

        item_quit = gtk.MenuItem(label="Quit")
        item_quit.connect("activate", gtk.main_quit)
        menu.append(item_quit)

        menu.show_all()

        return menu

    def main(self):
        self._indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self._indicator.set_menu(self.build_menu())
        GLib.timeout_add_seconds(
            int(self._config["update"]["update_interval"]), self.update_loop
        )
        GLib.timeout_add_seconds(2, self.update)

        gtk.main()

    def update_loop(self, *args, **kwargs):
        self.update(*args, **kwargs)
        return True

    def update(self, *args, **kwargs):
        logger.info("Updating...")
        self._indicator.set_status(appindicator.IndicatorStatus.ATTENTION)
        # print("Updating")
        self._indicator.set_label("", "")

        ssh = SSHClient()
        available_updates = []
        try:
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(
                self._config["ssh"]["ssh_hostname"],
                username=self._config["ssh"]["ssh_user"],
            )
            stdin_, stdout_, stderr_ = ssh.exec_command(
                "sudo apt-get update -q -y && "
                "sudo apt-get -q -y --ignore-hold --allow-change-held-packages "
                "-s dist-upgrade"
            )
            stdout_.channel.recv_exit_status()
            lines = stdout_.readlines()
            logger.debug(
                "Response from remote server:\n"
                + "\n".join([line.strip() for line in lines])
            )
            available_updates = [
                re.match(r"Inst (?P<app>\w+) \[(?P<version>[^\]]+)\]", line).groups()
                for line in lines
                if line.startswith("Inst ")
            ]
            updates_count = len(available_updates)

            if self._notification:
                self._notification.close()

            if updates_count:
                self._indicator.set_label(str(updates_count), str(updates_count))

                self._notification = notify.Notification.new(
                    "Upgrades available",
                    f"{updates_count} upgrades ready to install",
                    "sleeping.svg",
                )
                self._notification.add_action(
                    "activate", label="Launch updates", callback=self.upgrade
                )

                self._notification.show()

            else:
                self._indicator.set_label("", "")

            # Avoid looping when called with timeout_add_seconds
        except Exception as e:
            # print("Updating failed, settings locked state")
            self._indicator.set_icon_full(
                "locked.svg",
                "Error connecting",
            )
            self._indicator.set_label("", "")
            self._ssh_agent_locked = True

            logger.warning("Can't connect: " + str(e))

        else:
            #  print("Update done")
            self._indicator.set_icon_full(
                "sleeping.svg",
                "Update success",
            )
            self._ssh_agent_locked = False

        finally:
            self._last_update = GLib.DateTime.new_now_local()
            self._indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
            self._indicator.set_menu(self.build_menu(available_updates))

            logger.info("Update done")

        return None

    def upgrade(self, *args, **kwargs):
        logger.info("Running upgrade")
        proc = subprocess.Popen(
            shlex.split(self._config["update"]["upgrade_command"], posix=False)
        )
        try:
            outs, errs = proc.communicate()
        finally:
            GLib.timeout_add_seconds(1, self.update)

    def unlock_agent(self, *args, **kwargs):
        cmd = shlex.split(self._config["update"]["unlock_agent_command"], posix=True)
        logger.info("Running unlock command:" + str(cmd))
        proc = subprocess.Popen(cmd)
        try:
            outs, errs = proc.communicate()
        finally:
            GLib.timeout_add_seconds(1, self.update)


if __name__ == "__main__":
    try:
        App().main()

    except KeyboardInterrupt:
        logger.info("Shutting down")
        notify.uninit()
        sys.exit(0)
