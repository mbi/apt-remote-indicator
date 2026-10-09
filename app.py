import argparse
import configparser
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import gi

# MUST run before the gi.repository imports below: a typelib version has to be
# requested before the namespace is loaded, otherwise PyGObject falls back to a
# default version and warns (PyGIWarning).
gi.require_version("Notify", "0.7")
gi.require_version("AppIndicator3", "0.1")

from gi.repository import AppIndicator3 as appindicator
from gi.repository import GLib
from gi.repository import Gtk as gtk
from gi.repository import Notify as notify
from paramiko import AutoAddPolicy, SSHClient, SSHConfig
from systemd.journal import JournalHandler

APPINDICATOR_ID = "remote-apt-dater"

# Simulated dist-upgrade line: "Inst <pkg> [<installed>] (<candidate>, <origin> ...)"
INST_LINE_RE = re.compile(r"^Inst\s+(\S+)(?:\s+\[([^\]]*)\])?\s+\(([^),\s]+)")

logger = logging.getLogger(APPINDICATOR_ID)
logger.addHandler(JournalHandler(SYSLOG_IDENTIFIER=APPINDICATOR_ID))
logger.setLevel(logging.INFO)


class App:
    def __init__(self):
        self._config = configparser.ConfigParser()
        self._config.read(os.path.join(os.path.dirname(__file__), "config.ini"))

        if self._config["update"].get("ssh_agent_socket"):
            os.environ["SSH_AUTH_SOCK"] = self._config["update"].get("ssh_agent_socket")

        try:
            self._ssh_config = SSHConfig.from_path(os.path.expanduser("~/.ssh/config"))
        except FileNotFoundError:
            self._ssh_config = None
            logger.warning("No ~/.ssh/config found; connecting to hosts as given")

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
        self._polling = False

        notify.init(APPINDICATOR_ID)
        self._notification = None

    def build_menu(self, updates=None):
        if updates is None:
            updates = []
        menu = gtk.Menu()

        if updates:
            count = len(updates)
            mi = gtk.MenuItem(
                label=f"{count} update{'s' if count != 1 else ''} pending"
            )
            menu.append(mi)
            submenu = gtk.Menu()
            for pkg, old, new in updates:
                versions = f"{old} → {new}" if old else new
                smi = gtk.MenuItem(label=f"{pkg} {versions}")
                smi.set_sensitive(False)
                submenu.append(smi)
            mi.set_submenu(submenu)
        else:
            mi = gtk.MenuItem(label="Up to date")
            mi.set_sensitive(False)
            menu.append(mi)

        if self._last_update:
            updated_time = GLib.DateTime.format(
                self._last_update, "%-m/%-d/%Y %-I:%M %p"
            )
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

        self._indicator.set_label("", "")

        if self._notification:
            self._notification.close()

        ssh_hosts = self._config["ssh"]["ssh_hosts"].split(",")
        self._polling = True

        def poll_all():
            try:
                with ThreadPoolExecutor(max_workers=max(1, len(ssh_hosts))) as pool:
                    results = list(pool.map(self._poll_host, ssh_hosts))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Polling failed: {e}")
                results = [(set(), False)] * len(ssh_hosts)
            GLib.idle_add(self._apply_results, results)

        threading.Thread(target=poll_all, daemon=True).start()

    def _poll_host(self, ssh_host):
        """Poll one host off the main thread.

        Returns (set of (pkg, version), ok: bool); ok is False on any
        connection/exec failure. Must not touch GTK/AppIndicator/Notify.
        """
        available_updates = set()
        username, host = ssh_host.strip().split("@")
        host = host.strip()

        resolved = (
            self._ssh_config.lookup(host).get("hostname") if self._ssh_config else None
        )
        if resolved and resolved != host:
            logger.debug(f"{host} -> {resolved} via ~/.ssh/config")
            host = resolved

        ssh = SSHClient()
        try:
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(AutoAddPolicy())

            logger.debug(f"Connecting to {username}@{host}")

            ssh.connect(
                host,
                username=username.strip(),
                timeout=5,
                banner_timeout=5,
                auth_timeout=5,
            )
            _, stdout_, _ = ssh.exec_command(
                "sudo apt-get update -q -y && "
                "sudo apt-get -q -y --ignore-hold --allow-change-held-packages "
                "-s dist-upgrade"
            )
            stdout_.channel.recv_exit_status()
            lines = stdout_.readlines()
            logger.debug(
                f"Response from {host}:\n" + "\n".join([line.strip() for line in lines])
            )
            for match in (
                INST_LINE_RE.match(line.strip())
                for line in lines
                if line.startswith("Inst ")
            ):
                if match:
                    available_updates.add(match.groups())
            return available_updates, True

        except Exception as e:  # noqa: BLE001
            logger.warning(f"Can't connect to {username}@{host}: {e}")
            return available_updates, False
        finally:
            try:
                logger.debug("Closing ssh connection")
                ssh.close()
            except Exception:  # noqa: BLE001, S110
                pass

    def _apply_results(self, results):
        """Apply poll results on the main thread (GTK is not thread-safe)."""
        self._polling = False
        available_updates = set()
        failed_hosts = 0
        for updates, ok in results:
            available_updates |= updates
            if not ok:
                failed_hosts += 1

        if failed_hosts == len(results):
            # Everything failed: locked state, offer SSH agent unlock.
            logger.warning("Can't connect to any host")
            self._indicator.set_icon_full(
                "locked.svg",
                "Error connecting",
            )
            self._indicator.set_label("", "")
            self._ssh_agent_locked = True
        else:
            self._indicator.set_icon_full(
                "sleeping.svg",
                "Update success",
            )
            self._ssh_agent_locked = False

        updates_count = len(available_updates)

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

        self._last_update = GLib.DateTime.new_now_local()
        self._indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self._indicator.set_menu(self.build_menu(set(available_updates)))

        logger.info("Update done")

        # Avoid looping when called with timeout_add_seconds

    def upgrade(self, *args, **kwargs):
        logger.info("Running upgrade")
        proc = subprocess.Popen(
            shlex.split(self._config["update"]["upgrade_command"], posix=False)
        )
        try:
            _outs, _errs = proc.communicate()
        finally:
            GLib.timeout_add_seconds(1, self.update)

    def unlock_agent(self, *args, **kwargs):
        cmd = shlex.split(self._config["update"]["unlock_agent_command"], posix=True)
        logger.info("Running unlock command:" + str(cmd))
        proc = subprocess.Popen(cmd)
        try:
            _outs, _errs = proc.communicate()
        finally:
            GLib.timeout_add_seconds(1, self.update)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AppIndicator tray applet polling remote hosts for pending apt upgrades"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="set logging level to DEBUG (raw apt responses, SSH lifecycle)",
    )
    args = parser.parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    try:
        App().main()

    except KeyboardInterrupt:
        logger.info("Shutting down")
        notify.uninit()
        sys.exit(0)
