# Repository Guidelines

## Project Overview
Single-file Python 3 GNOME tray applet (`app.py`, AppIndicator id `remote-apt-dater`) that periodically SSHes into configured Debian hosts, runs `sudo apt-get update` plus a **simulated** `apt-get dist-upgrade` (`-s` — it never changes remote packages), parses apt's `Inst <pkg> [<installed>] (<candidate>)` lines, and surfaces pending upgrades in the tray: icon label with count, a menu listing each pending package, a desktop notification, and an "Update now" action that launches a locally configured upgrade command.

## Architecture & Data Flow
One class `App` (plus `acquire_instance_lock()` for single-instance startup: a non-blocking `fcntl.flock` on `$XDG_RUNTIME_DIR/remote-apt-dater.lock`, fallback `/tmp`; a second launch logs and exits 1, and the kernel releases the lock on process exit — no stale locks). Event model is entirely the GLib/GTK main loop. SSH polls run off the main thread: `update()` spawns one daemon coordinator thread that fans out per-host `_poll_host` calls through a `ThreadPoolExecutor` (one worker per configured host), then hands results back with `GLib.idle_add(self._apply_results, results)`. All GTK/AppIndicator/Notify calls stay on the main thread (`_apply_results`); `_poll_host` must never touch them. A `self._polling` guard makes re-entrant `update()` calls (e.g. mashing "Check now") no-ops. Other blocking work (`subprocess` in `upgrade`/`unlock_agent`) still runs on the main thread.

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
Flat repository, no package structure:
- `app.py` — entire application (~310 lines)
- `sleeping.svg`, `updating.svg`, `locked.svg` — tray state icons (resolved relative to the script directory via `Indicator.new_with_path`)
- `tests/` — pytest suite (headless: stubs GTK/Notify, no network/display)
- `.venv/` — local uv-managed Python venv (gitignored)

## Development Commands
```sh
.venv/bin/python app.py                      # run the applet (icons + config resolved from script dir)
uv run pytest                                # run the test suite (dev deps in [dependency-groups])
uv lock                                      # regenerate uv.lock (resolves from pyproject.toml)
uv sync                                      # sync .venv to uv.lock (also prunes extraneous packages)
cp config.ini.sample config.ini              # create local config, then edit (config.ini is gitignored)
```
No lint or packaging commands exist. `ruff` is an IDE-level preference only.

## Code Conventions & Common Patterns
- **Config**: `configparser` reading `config.ini` from `os.path.dirname(__file__)`; never track or commit `config.ini` (contains real hostnames).
- **External commands from config**: `shlex.split(cmd, posix=False)` for `upgrade_command` (intentional — handles kitty-style quoting), `posix=True` for `unlock_agent_command`; both launched via `subprocess.Popen(...)` + `communicate()`. Handler methods on menu callbacks accept `*args, **kwargs` and MUST NOT return a truthy value or the timeout will re-fire.
- **Logging**: module logger `logging.getLogger(APPINDICATOR_ID)` with `systemd.journal.JournalHandler(SYSLOG_IDENTIFIER=remote-apt-dater)`, level INFO (DEBUG for raw apt responses). View with `journalctl --identifier=remote-apt-dater -f`.
- **Error handling**: the SSH poll uses one broad `except Exception` → locked state + warning log; `ssh.close()` sits in a `finally` guarded by a bare `except: pass`. Follow this pattern for new remote operations rather than introducing per-exception menus.
- **gi (PyGObject)**: `gi.require_version("Notify", "0.7")` / `("AppIndicator3", "0.1")` MUST stay ahead of the `gi.repository` imports — requesting a version after the typelib is loaded is a no-op and makes PyGObject emit `PyGIWarning` and pick a default version; GTK menu built with `Gtk.Menu`/`Gtk.MenuItem`, callbacks via `item.connect("activate", self.<method>)`; state stored on `self._*` instance attributes (`_config`, `_indicator`, `_notification`, `_last_update`, `_ssh_agent_locked`).
- **Updates collection**: set of `(pkg, old, new)` tuples captured by module-level `INST_LINE_RE` from `Inst ` lines of the simulated dist-upgrade output (`old`/`installed` version is optional in apt's format); menu rows render `pkg old → new` (arch-update style, plain text only — AppIndicator/DBusMenu menus cannot carry styling or right-aligned columns); header reads `N updates pending`; the "Last checked" item formats `%-m/%-d/%Y %-I:%M %p`.

## Important Files
| File | Role |
|---|---|
|`app.py`|Entire source: `App.__init__` → `build_menu` → `main` (timers) → `update` (spawns thread pool) → `_poll_host` (per-host SSH, worker thread) → `_apply_results` (main-thread UI) → `upgrade`/`unlock_agent` (Popen)|
| `config.ini.sample` | Tracked config template; the contract for all config keys |
| `config.ini` | Live config — gitignored, per-deployment |
| `pyproject.toml` | Direct deps only (bare `>=` lower bounds, no hashes): PyGObject, paramiko, systemd-python |
| `uv.lock` | Exact-pinned lockfile incl. transitive deps (tracked in git) |
| `tests/` | Pytest suite + `conftest.py` stubs (headless; run with `uv run pytest`) |
| `sleeping.svg` / `updating.svg` / `locked.svg` | Tray icon states |

## Runtime/Tooling Preferences
- **Python 3.14** (`requires-python = ">=3.14"` in `pyproject.toml`; venv at `.venv`, managed by uv); run from the repository root — the app resolves `config.ini` and icons relative to `app.py`'s directory, so it does not need to be installed and there is no packaging metadata (not installable).
- **uv** dependency workflow: deps are managed in `pyproject.toml` (direct deps as loose `>=` lower bounds) + `uv.lock` (exact pins, tracked in git). Changing a dep: edit `pyproject.toml`, then `uv lock` and `uv sync`. Never edit `uv.lock` by hand.
- **System prerequisites**: AppIndicator3/`libnotify` typelib files, ssh-agent, journald. Deps beyond PyGObject/paramiko/systemd-python are transitive only.
- Ruff format/check on save is an IDE-level preference (`.idea/`); no repo-level lint config exists.

## Testing & QA
Pytest suite in `tests/` (dev dependency via `[dependency-groups]` in `pyproject.toml`; run with `uv run pytest` or `.venv/bin/python -m pytest`). It is **headless**: `tests/conftest.py` stubs `gtk` and `notify` on the `app` module and builds `App` instances through `App.__new__` + hand-set attributes, so `__init__` (AppIndicator/notify/config.ini) is never exercised. No display, session bus, SSH, or network access is required.

Coverage: `INST_LINE_RE` parsing; `_poll_host` (update parsing, `~/.ssh/config` hostname substitution, connect timeouts, failure isolation, client always closed); `_apply_results` (all-hosts-failed → locked state, partial failure → success, dedupe/merge across hosts, notification, `_polling` reset); `build_menu` (row text `pkg old → new`, pluralized header, `Last checked M/D/Y H:MM AM|PM`, conditional items, action wiring); `update()`/`update_loop()` (daemon-thread handoff, `GLib.idle_add` application, `ThreadPoolExecutor` failure fallback, timer recurrence contract).

New behavior belongs in a test here when it is consumer-visible (tray state, menu text, notification, config contract). Don't add tests that pin incidental strings or wiring-for-wiring's-sake; prefer one focused test per decision (see the mutation-checked date/row-format tests).
