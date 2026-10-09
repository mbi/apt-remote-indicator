"""INST_LINE_RE: parsing of apt's simulated dist-upgrade output."""

import pytest

from app import INST_LINE_RE


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Normal dist-upgrade line: installed version in [], candidate in ().
        (
            "Inst libc6 [2.36-8] (2.36-9 Debian-Security:12 [amd64])",
            ("libc6", "2.36-8", "2.36-9"),
        ),
        # Candidate followed by a comma (apt appends origin/size info).
        (
            "Inst curl [8.0.1-1] (8.1.0-1, Debian:12 [amd64])",
            ("curl", "8.0.1-1", "8.1.0-1"),
        ),
        # No installed version available (new package / not installed).
        (
            "Inst python3-minimal (3.11.2-1)",
            ("python3-minimal", None, "3.11.2-1"),
        ),
        # Tabs / extra whitespace between columns.
        ("Inst\tfoo\t[1.0]\t(2.0)", ("foo", "1.0", "2.0")),
        ("Inst   foo  [1.0]  (2.0)", ("foo", "1.0", "2.0")),
        # Epoch, tilde, backport versions and odd package names.
        (
            "Inst libstdc++6 [1:12.2.0-14] (1:13.2.0-1 Debian:12 [amd64])",
            ("libstdc++6", "1:12.2.0-14", "1:13.2.0-1"),
        ),
        (
            "Inst udev [257.7-1~bpo12+1] (257.8-1~bpo12+1 Debian-backports [amd64])",
            ("udev", "257.7-1~bpo12+1", "257.8-1~bpo12+1"),
        ),
        (
            "Inst docker.io [26.1.3-1] (28.3.3-1 Debian:12/stable [amd64])",
            ("docker.io", "26.1.3-1", "28.3.3-1"),
        ),
    ],
)
def test_matches_real_inst_lines(line, expected):
    match = INST_LINE_RE.match(line)
    assert match is not None
    assert match.groups() == expected


@pytest.mark.parametrize(
    "line",
    [
        "Conf libc6 (2.36-9 Debian-Security:12 [amd64])",
        "Remv linux-image-6.1.0-13-amd64 [6.1.140-1]",
        "Reading package lists...",
        "Installing foo (1.0)",  # prefix collision: not an "Inst " line
        "Instfoo [1.0] (2.0)",  # no whitespace after "Inst"
        "  Inst libc6 [2.36-8] (2.36-9)",  # leading whitespace: not anchored
    ],
)
def test_rejects_non_inst_lines(line):
    assert INST_LINE_RE.match(line) is None
