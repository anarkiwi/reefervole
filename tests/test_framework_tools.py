"""Tests for the packaged framework tools: link checking and ECP5 cost reporting."""

from __future__ import annotations

import pathlib
import subprocess

from migen import Module, Record, Signal

from reefervole import checkdocs, synth


def test_link_pattern_ignores_absolute_and_anchor_links():
    """Only repository-relative links are the checker's business."""
    found = checkdocs.LINK.findall(
        "[a](docs/b.md) [b](https://x/y) [c](#anchor) [d](../up.md#frag) [e](http://z)"
    )
    assert found == ["docs/b.md", "../up.md"]


def test_tracked_markdown_lists_only_git_tracked_files(tmp_path: pathlib.Path):
    """Untracked and ignored markdown is not ours to validate."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.md").write_text("[x](tracked.md)", encoding="utf-8")
    (tmp_path / "untracked.md").write_text("[x](nowhere.md)", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    assert checkdocs.tracked_markdown(tmp_path) == [pathlib.Path("tracked.md")]


def test_main_reports_a_broken_relative_link(tmp_path, monkeypatch, capsys):
    """A link to a missing file is an error; a link that resolves is not."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.md").write_text("[x](missing.md)", encoding="utf-8")
    subprocess.run(["git", "add", "a.md"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    assert checkdocs.main() == 1
    assert "missing.md" in capsys.readouterr().err

    (tmp_path / "missing.md").write_text("here", encoding="utf-8")
    assert checkdocs.main() == 0


class _Dut(Module):
    """A module whose interface covers every shape collect_ios must walk."""

    def __init__(self):
        self.bare = Signal(4)
        self.record = Record([("a", 2), ("b", 3)])
        self.per_direction = [Signal(5), Signal(6)]
        self._private = Signal(7)


def test_collect_ios_walks_records_and_per_direction_lists():
    """Anything missed here is pruned by yosys, silently under-reporting the block."""
    ios = synth.collect_ios(_Dut())
    # bare, the record's two fields, and both per-direction signals; not the private one.
    assert len(ios) == 5
    assert all(isinstance(s, Signal) for s in ios)


def test_collect_ios_skips_underscored_attributes():
    """A leading underscore marks internal state, which is not an interface."""

    class _Internal(Module):
        def __init__(self):
            self._hidden = Signal()

    assert synth.collect_ios(_Internal()) == set()


def test_lut4_equivalent_counts_a_carry_cell_as_two():
    """A CCU2C occupies both LUT4s of an ECP5 slice."""
    assert synth.LUT4_EQUIVALENT["CCU2C"] == 2
    assert synth.LUT4_EQUIVALENT["LUT4"] == 1


def test_elaborate_constructs_from_string_arguments():
    """Arguments arrive from argv as strings and must reach the constructor as values."""
    dut, name, kwargs = synth.elaborate("reefervole.gateware.mactable:MacTable", ["buckets=256"])
    assert name == "MacTable"
    assert kwargs == {"buckets": 256}
    assert synth.collect_ios(dut)
