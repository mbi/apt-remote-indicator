# apt-remote-indicator

A single-file GNOME tray applet that polls remote Debian hosts over SSH and
surfaces pending package upgrades in the system tray.

Inspired by [arch-update](https://codeberg.org/RaphaelRochet/arch-update).

## What it does

On every poll (default interval, or "Check now" from the menu), the applet
connects to each configured host and runs:

- `sudo apt-get update -q -y`
- `sudo apt-get -q -y --ignore-hold --allow-change-held-packages -s dist-upgrade`

The `-s` flag **simulates** the dist-upgrade — remote packages are never
changed. It parses apt's `Inst <pkg> <version>` lines to build the list of
pending upgrades.

Hosts are polled in parallel (one thread per host); the tray is updated on
the GTK main thread once all results are in.

## Tray states

| Icon | Meaning |
|---|---|
| `sleeping.svg` | Up to date / poll succeeded |
| `updating.svg` | A poll is in flight |
| `locked.svg` | Every host failed (e.g. locked ssh-agent); offers an "Unlock SSH Agent" action |

The menu lists each pending package and its version, plus "Update now"
(runs the configured local upgrade command) and "Check now".

## Requirements

- Linux with an AppIndicator-compatible tray (AppIndicator3, libnotify, journald)
- Python 3.11 (deps: PyGObject, paramiko, systemd-python)
- ssh-agent (for key auth; `sudo` must be passwordless or cached on the hosts)

## Setup

```sh
uv sync                              # create/sync .venv from uv.lock
cp config.ini.sample config.ini      # then edit (config.ini is gitignored)
.venv/bin/python app.py              # run from the repo root
```

### Configuration (`config.ini`)

```ini
[ssh]
ssh_hosts = user@host1,user@host2   ; comma-separated user@host entries

[update]
update_interval = 900               ; seconds between polls
upgrade_command = kitty -e bash -c "sudo apt-get upgrade -y"   ; run locally
unlock_agent_command = ssh-add      ; offered when all hosts fail
ssh_agent_socket = /run/user/1000/keyring/ssh  ; optional, sets SSH_AUTH_SOCK
```

Hostnames are resolved through `~/.ssh/config` when a matching `Host` entry
exists. Per-host connect/auth timeouts are 5 seconds.

## Logging

Logs to journald under the identifier `remote-apt-dater`:

```sh
journalctl --identifier=remote-apt-dater -f
```

`--verbose` enables DEBUG (raw apt responses, SSH lifecycle).
