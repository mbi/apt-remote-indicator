# Repository Guidelines

## Project Overview
Single-file Python 3 GNOME tray applet (`app.py`, AppIndicator id `remote-apt-dater`) that periodically SSHes into configured Debian hosts, runs `sudo apt-get update` plus a **simulated** `apt-get dist-upgrade` (`-s` — it never changes remote packages), parses apt's `Inst <pkg> [<installed>] (<candidate>)` lines, and surfaces pending upgrades in the tray: icon label with count, a menu listing each pending package, a desktop notification, and an "Update now" action that launches a locally configured upgrade command.

## Architecture & Data Flow
One class `App`, no module-level functions beyond the `__main__` guard. Event model is entirely the GLib/GTK main loop. SSH polls run off the main thread: `update()` spawns one daemon coordinator thread that fans out per-host `_poll_host` calls through a `ThreadPoolExecutor` (one worker per configured host), then hands results back with `GLib.idle_add(self._apply_results, results)`. All GTK/AppIndicator/Notify calls stay on the main thread (`_apply_results`); `_poll_host` must never touch them. A `self._polling` guard makes re-entrant `update()` calls (e.g. mashing "Check now") no-ops. Other blocking work (`subprocess` in `upgrade`/`unlock_agent`) still runs on the main thread.

```mermaid
flowchart LR
  A[config.ini] --> B["App.__init__"]
  T["GLib timeouts<br>(2s initial, then update_interval)"] --> U["App.update"]
  U -->|thread pool, paramiko per ssh_hosts entry| S["remote: apt-get update<br>+ -s dist-upgrade"]
  S -->|parse 'Inst ' lines| P[set of (pkg, old, new)]
  P -->|GLib.idle_add| UI["icon / label / menu / notification<br>(main thread, _apply_results)"]
  M["Update now / notification action"] --> C["Popen upgrade_command"] --> U
  L["Unlock SSH Agent"] --> X["Popen unlock_agent_command"] --> U
```

- **State machine via tray icons**: `sleeping.svg` = idle/up-to-date (default ACTIVE icon, restored after a successful poll); `updating.svg` = attention icon shown while a poll runs (`set_attention_icon_full` + `IndicatorStatus.ATTENTION`); `locked.svg` = `set_icon_full` when the SSH poll raises (connection/auth failure, e.g. locked ssh-agent) — sets `_ssh_agent_locked` and adds an "Unlock SSH Agent" menu item.
- **Callback chaining quirk**: `update()` returns `None` (a directly scheduled timeout fires once); `update_loop()` returns `True` so the interval timer recurs; code paths after Popen reschedule with `GLib.timeout_add_seconds(1, self.update)`. Any new timeout-driven caller MUST respect this single-fire convention. A poll in flight sets `_polling` until `_apply_results` runs; `update()` returns immediately if it is set.
- **Menu rebuilds from scratch** on every poll (`build_menu(set(available_updates))`); no incremental widget updates. Upgrade action comes from the notification callback (`notify` activate) or the menu.
- **Config consumed** (see `config.ini.sample`): `[ssh] ssh_hosts` (comma-separated `user@host`; split on `","` then exactly one `"@"` per entry), `[update] update_interval` (int seconds), `upgrade_command`, `unlock_agent_command`, optional `ssh_agent_socket` (exported as `SSH_AUTH_SOCK` before connecting).

## Key Directories
Flat repository, no package structure — everything lives in the root:
- `app.py` — entire application (~313 lines)
- `sleeping.svg`, `updating.svg`, `locked.svg` — tray state icons (resolved relative to the script directory via `Indicator.new_with_path`)
- `.venv/` — local Python 3.11.9 pyenv venv (gitignored)

## Development Commands
```sh
.venv/bin/python app.py                      # run the applet (icons + config resolved from script dir)
uv lock                                      # regenerate uv.lock (resolves from pyproject.toml)
uv sync                                      # sync .venv to uv.lock (also prunes extraneous packages)
cp config.ini.sample config.ini              # create local config, then edit (config.ini is gitignored)
```
No lint, test, or packaging commands exist.

## Code Conventions & Common Patterns
- **Config**: `configparser` reading `config.ini` from `os.path.dirname(__file__)`; never track or commit `config.ini` (contains real hostnames).
- **External commands from config**: `shlex.split(cmd, posix=False)` for `upgrade_command` (intentional — handles kitty-style quoting), `posix=True` for `unlock_agent_command`; both launched via `subprocess.Popen(...)` + `communicate()`. Handler methods on menu callbacks accept `*args, **kwargs` and MUST NOT return a truthy value or the timeout will re-fire.
- **Logging**: module logger `logging.getLogger(APPINDICATOR_ID)` with `systemd.journal.JournalHandler(SYSLOG_IDENTIFIER=remote-apt-dater)`, level INFO (DEBUG for raw apt responses). View with `journalctl --identifier=remote-apt-dater -f`.
- **Error handling**: the SSH poll uses one broad `except Exception` → locked state + warning log; `ssh.close()` sits in a `finally` guarded by a bare `except: pass`. Follow this pattern for new remote operations rather than introducing per-exception menus.
- **gi (PyGObject)**: `gi.require_version("AppIndicator3", "0.1")` / `("Notify", "0.7")` at import time; GTK menu built with `Gtk.Menu`/`Gtk.MenuItem`, callbacks via `item.connect("activate", self.<method>)`; state stored on `self._*` instance attributes (`_config`, `_indicator`, `_notification`, `_last_update`, `_ssh_agent_locked`).
- **Updates collection**: set of `(pkg, old, new)` tuples captured by module-level `INST_LINE_RE` from `Inst ` lines of the simulated dist-upgrade output (`old`/`installed` version is optional in apt's format); menu rows render `pkg old → new` (arch-update style, plain text only — AppIndicator/DBusMenu menus cannot carry styling or right-aligned columns); header reads `N updates pending`; the "Last checked" item formats `%-m/%-d/%Y %-I:%M %p`.

## Important Files
| File | Role |
|---|---|
|`app.py`|Entire source: `App.__init__` → `build_menu` → `main` (timers) → `update` (spawns thread pool) → `_poll_host` (per-host SSH, worker thread) → `_apply_results` (main-thread UI) → `upgrade`/`unlock_agent` (Popen)|
| `config.ini.sample` | Tracked config template; the contract for all config keys |
| `config.ini` | Live config — gitignored, per-deployment |
| `pyproject.toml` | Direct deps only (bare `>=` lower bounds, no hashes): PyGObject, paramiko, systemd-python |
| `uv.lock` | Exact-pinned lockfile incl. transitive deps (tracked in git) |
| `sleeping.svg` / `updating.svg` / `locked.svg` | Tray icon states |

## Runtime/Tooling Preferences
- **Python 3.11** (pyenv 3.11.9, venv at `.venv`); run from the repository root — the app resolves `config.ini` and icons relative to `app.py`'s directory, so it does not need to be installed and there is no packaging metadata (not installable).
- **uv** dependency workflow: deps are managed in `pyproject.toml` (direct deps as loose `>=` lower bounds) + `uv.lock` (exact pins, tracked in git). Changing a dep: edit `pyproject.toml`, then `uv lock` and `uv sync`. Never edit `uv.lock` by hand.
- **System prerequisites**: AppIndicator3/`libnotify` typelib files, ssh-agent, journald. Deps beyond PyGObject/paramiko/systemd-python are transitive only.
- Ruff format/check on save is an IDE-level preference (`.idea/`); no repo-level lint config exists.

## Testing & QA
No tests, test framework, CI, or QA dependencies exist anywhere (the `.venv/.../pudb/test/` directory is third-party pudb's bundled suite, not this project's). Changes are verified by running `python app.py` and exercising the tray; keep this single-file/no-tooling setup unless the user explicitly asks for tests or lint setup.
