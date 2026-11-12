"""App._poll_host: per-host SSH polling, parsing and failure isolation."""

import threading

import pytest

import app as app_module


class FakeChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status

    def recv_exit_status(self):
        return self._exit_status


class FakeStdout:
    def __init__(self, lines):
        self.channel = FakeChannel()
        self._lines = lines

    def readlines(self):
        return [line + "\n" for line in self._lines]


class FakeSSHClient:
    """Stands in for paramiko.SSHClient; records how it was used."""

    def __init__(self, lines=(), error=None):
        self.lines = list(lines)
        self.error = error
        self.connect_args = None
        self.exec_command_arg = None
        self.closed = False

    def load_system_host_keys(self):
        pass

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, host, username=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.connect_args = (host, username, kwargs)

    def exec_command(self, command):
        self.exec_command_arg = command
        return None, FakeStdout(self.lines), None

    def close(self):
        self.closed = True


@pytest.fixture
def fake_ssh(monkeypatch):
    """Install a fake paramiko client; returns the instance handed to app.py."""
    client = FakeSSHClient()

    def factory():
        return client

    monkeypatch.setattr(app_module, "SSHClient", factory)
    return client


class FakeSSHConfig:
    """Mirrors paramiko's SSHConfig.lookup(): a dict with a 'hostname' key."""

    def __init__(self, hostnames):
        self.hostnames = hostnames

    def lookup(self, host):
        return {"hostname": self.hostnames.get(host)}


def test_parses_inst_lines_into_updates(make_app, fake_ssh):
    fake_ssh.lines = [
        "Reading package lists...",
        "Inst libc6 [2.36-8] (2.36-9 Debian-Security:12 [amd64])",
        "Conf libc6 (2.36-9 Debian-Security:12 [amd64])",
        "Inst curl [8.0.1-1] (8.1.0-1 Debian:12 [amd64])",
    ]
    updates, failure = make_app()._poll_host("mbi@host1")

    assert failure is None
    assert updates == {
        ("libc6", "2.36-8", "2.36-9"),
        ("curl", "8.0.1-1", "8.1.0-1"),
    }


def test_uses_bounded_connect_timeouts(make_app, fake_ssh):
    make_app()._poll_host("mbi@host1")

    host, username, kwargs = fake_ssh.connect_args
    assert (host, username) == ("host1", "mbi")
    assert kwargs == {"timeout": 5, "banner_timeout": 5, "auth_timeout": 5}


def test_runs_update_plus_simulated_dist_upgrade(make_app, fake_ssh):
    make_app()._poll_host("mbi@host1")

    command = fake_ssh.exec_command_arg
    assert "sudo apt-get update" in command
    # The `-s` flag keeps the upgrade simulated: packages are never changed.
    assert "-s dist-upgrade" in command


def test_ssh_config_hostname_replaces_host(make_app, fake_ssh):
    obj = make_app()
    obj._ssh_config = FakeSSHConfig({"home.mbi.me": "188.154.141.88"})

    obj._poll_host("mbi@home.mbi.me")

    assert fake_ssh.connect_args[0] == "188.154.141.88"


def test_unmatched_host_used_as_given(make_app, fake_ssh):
    obj = make_app()
    obj._ssh_config = FakeSSHConfig({})

    obj._poll_host("mbi@canary.cruncher.ch")

    assert fake_ssh.connect_args[0] == "canary.cruncher.ch"


def test_no_ssh_config_uses_host_as_given(make_app, fake_ssh):
    obj = make_app()
    obj._ssh_config = None

    obj._poll_host("mbi@host1")

    assert fake_ssh.connect_args[0] == "host1"


def test_connection_failure_is_reported_and_not_raised(make_app, fake_ssh):
    fake_ssh.error = OSError("timed out")

    updates, failure = make_app()._poll_host("mbi@host1")

    assert failure == "other"
    assert updates == set()


def test_authentication_failure_is_distinguished(make_app, fake_ssh):
    """A locked agent surfaces as auth rejection, not as "unreachable"."""
    fake_ssh.error = app_module.AuthenticationException("Authentication failed.")

    updates, failure = make_app()._poll_host("mbi@host1")

    assert failure == "auth"
    assert updates == set()


def test_connection_is_closed_on_success_and_failure(make_app, fake_ssh):
    obj = make_app()
    obj._poll_host("mbi@host1")
    assert fake_ssh.closed is True

    fake_ssh.closed = False
    fake_ssh.error = OSError("boom")
    obj._poll_host("mbi@host1")
    assert fake_ssh.closed is True


def test_first_connect_is_gated_until_a_login_succeeds(make_app, monkeypatch):
    """A locked keyring must prompt once, not once per parallel host."""
    obj = make_app()
    gated = []

    class RecordingClient(FakeSSHClient):
        def connect(self, host, username=None, **kwargs):
            gated.append(obj._auth_lock.locked())
            super().connect(host, username=username, **kwargs)

    monkeypatch.setattr(app_module, "SSHClient", RecordingClient)

    obj._poll_host("mbi@host1")
    assert gated == [True]  # first login: serialized, so one prompt
    assert obj._auth_ready.is_set()

    obj._poll_host("mbi@host2")
    assert gated == [True, False]  # agent proven usable: parallel again


def test_parallel_first_connects_reach_the_agent_one_at_a_time(make_app, monkeypatch):
    """Hosts must not open one keyring dialog each while the agent is locked."""
    obj = make_app()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    entered = []
    lock = threading.Lock()

    class BlockingClient(FakeSSHClient):
        def connect(self, host, username=None, **kwargs):
            with lock:
                entered.append(host)
                index = len(entered)
            (first_entered if index == 1 else second_entered).set()
            if index == 1:
                # Stands in for the user typing the passphrase.
                assert release_first.wait(5)
            super().connect(host, username=username, **kwargs)

    monkeypatch.setattr(app_module, "SSHClient", BlockingClient)

    first = threading.Thread(target=obj._poll_host, args=("mbi@host1",), daemon=True)
    second = threading.Thread(target=obj._poll_host, args=("mbi@host2",), daemon=True)
    first.start()
    assert first_entered.wait(5)
    second.start()
    try:
        # While the first login is pending, the second host must wait in
        # front of the gate instead of issuing its own signing request.
        assert second_entered.wait(0.5) is False
    finally:
        release_first.set()
    first.join(5)
    second.join(5)

    assert entered == ["host1", "host2"]
    assert obj._auth_ready.is_set()


def test_failed_first_login_leaves_the_gate_closed(make_app, fake_ssh):
    """A cancelled prompt must not be mistaken for a working agent."""
    obj = make_app()
    fake_ssh.error = OSError("timed out")

    obj._poll_host("mbi@host1")
    assert not obj._auth_ready.is_set()

    fake_ssh.error = None
    obj._poll_host("mbi@host1")
    assert obj._auth_ready.is_set()
