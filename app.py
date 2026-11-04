import argparse
import configparser
import fcntl
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import gi

# MUST run before the gi.repository imports below: a typelib version has to be
# requested before the namespace is loaded, otherwise PyGObject falls back to a
# default version and warns (PyGIWarning).
gi.require_version("Notify", "0.7")
gi.require_version("AppIndicator3", "0.1")

from gi.repository import AppIndicator3 as appindicator
from gi.repository import Gio, GLib
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

LOCK_FILE = APPINDICATOR_ID + ".lock"

AGENT_PROBE_TIMEOUT = 2


def ssh_agent_has_identities(timeout=AGENT_PROBE_TIMEOUT):
    """Report whether the running ssh-agent exposes at least one identity.

    Returns True when the agent lists at least one key, False when the agent is
    reachable but exposes no identities (empty, or locked by `ssh-add -x` /
    a keyring locking on suspend), and None when the state is unknown (no
    ssh-add, agent unreachable, probe hung). Callers must not treat None as
    "locked": spawning an unlock terminal on an unknown state is noise.
    """
    try:
        result = subprocess.run(
            ["ssh-add", "-l"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:  # "The agent has no identities."
        return False
    return None


def acquire_instance_lock():
    """Return an fd holding an exclusive instance lock, or None if one is held.

    flock releases when the process exits, so there are no stale locks to clean up.
    """
    directory = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    fd = os.open(os.path.join(directory, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


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
        self._check_now_item = None

        self._watch_resume()

    def _watch_resume(self):
        """React whenever the machine wakes from suspend.

        Subscribes to logind's PrepareForSleep signal: the boolean argument is
        True just before sleep and False on resume. The callback fires on the
        default main context (the GTK main loop), so touching UI is safe.
        """
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Resume detection unavailable ({e}); relying on interval polls"
            )
            return
        bus.signal_subscribe(
            "org.freedesktop.login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            "/org/freedesktop/login1",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_prepare_for_sleep,
        )
        # Keep the connection object alive; GLib drops the subscription otherwise.
        self._resume_bus = bus

    def _on_prepare_for_sleep(self, bus, sender, path, iface, signal, params):
        if params.unpack()[0]:
            return  # going to sleep, not waking
        logger.info("Resumed from sleep")
        if self._polling:
            return
        # Probing the agent can wait on a keyring that is still waking up, so
        # keep it off the main loop.
        threading.Thread(target=self._resume, daemon=True).start()

    def _resume(self):
        """Wake handler body, off the main thread: unlock the agent if needed.

        Agents that lock on suspend (OpenSSH's `ssh-add -x`, keyrings that
        lock with the session) stop exposing identities, so the first poll
        after a wake fails and the user has to unlock by hand. Launch the
        configured unlock command instead; the usual post-command refresh then
        runs the poll.
        """
        has_identities = None
        if self._config["update"].get("unlock_agent_command"):
            has_identities = ssh_agent_has_identities()
        GLib.idle_add(self._after_resume, has_identities)

    def _after_resume(self, has_identities):
        """Act on the resume decision on the main thread."""
        if self._polling:
            return False
        if has_identities is False:
            logger.info("SSH agent has no identities after resume; unlocking")
            self._ssh_agent_locked = True
            self._indicator.set_icon_full("locked.svg", "SSH agent locked")
            self.unlock_agent()
        else:
            self.update()
        return False

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

        item_update = gtk.MenuItem(label="Check now")
        item_update.connect("activate", self.update)
        # update() greys this out while a poll is in flight; the next
        # _apply_results rebuilds the menu with a fresh, sensitive item.
        self._check_now_item = item_update
        menu.append(item_update)

        menu.append(gtk.SeparatorMenuItem.new())

        item_upgrade = gtk.MenuItem(label="Launch apt-dater")
        item_upgrade.connect("activate", self.upgrade)
        menu.append(item_upgrade)

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

        if self._check_now_item is not None:
            self._check_now_item.set_sensitive(False)

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

    def _run_command(self, label, cmd):
        """Launch cmd without blocking the GTK main loop."""
        logger.info(f"Running {label}:" + str(cmd))
        proc = subprocess.Popen(cmd)
        threading.Thread(
            target=self._wait_for_command, args=(proc,), daemon=True
        ).start()

    def _wait_for_command(self, proc):
        """Reap a launched command off the main thread, then refresh the counts."""
        try:
            proc.communicate()
        finally:
            GLib.idle_add(self._refresh_after_command)

    def _refresh_after_command(self):
        GLib.timeout_add_seconds(1, self.update)
        return False  # idle handlers must not recur

    def upgrade(self, *args, **kwargs):
        self._run_command(
            "upgrade command",
            shlex.split(self._config["update"]["upgrade_command"], posix=True),
        )

    def unlock_agent(self, *args, **kwargs):
        self._run_command(
            "unlock command",
            shlex.split(self._config["update"]["unlock_agent_command"], posix=True),
        )


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

    lock_fd = acquire_instance_lock()
    if lock_fd is None:
        logger.error("Another instance is already running; exiting")
        print("remote-apt-dater: another instance is already running", file=sys.stderr)
        sys.exit(1)

    try:
        App().main()

    except KeyboardInterrupt:
        logger.info("Shutting down")
        notify.uninit()
        sys.exit(0)
