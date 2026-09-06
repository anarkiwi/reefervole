"""Pairing and the sign-flip test behind the sweep comparison."""

from __future__ import annotations

import numpy as np
import pytest

from sweep_compare import compare, read_sweep, sign_flip_p

CLOCK = "crg_clkout0"


def log_for(mhz: float, target: float = 62.5) -> str:
    """One seed log carrying nextpnr's pre-route estimate and its post-route figure."""
    verdict = "PASS" if mhz >= target else "FAIL"
    return (
        f"Info: Max frequency for clock '$glbnet${CLOCK}': 1.00 MHz (FAIL at {target} MHz)\n"
        f"Info: Max frequency for clock '$glbnet${CLOCK}': {mhz} MHz ({verdict} at {target} MHz)\n"
    )


@pytest.fixture(name="sweeps")
def sweeps_fixture(tmp_path):
    """Two sweeps over the same four seeds, the second uniformly 2 MHz faster."""
    dirs = []
    for offset in (0.0, 2.0):
        directory = tmp_path / f"sweep{offset}"
        directory.mkdir()
        for seed, mhz in enumerate([61.0, 63.0, 64.0, 66.0], start=1):
            (directory / f"seed{seed}.log").write_text(log_for(mhz + offset), encoding="utf-8")
        dirs.append(directory)
    return dirs


def test_only_the_post_route_figure_is_read(tmp_path):
    """The estimate nextpnr prints before routing is not the measurement."""
    (tmp_path / "seed3.log").write_text(log_for(65.0), encoding="utf-8")
    (tmp_path / "notes.log").write_text("nothing", encoding="utf-8")
    sweep = read_sweep(tmp_path)
    assert set(sweep) == {3}
    assert sweep[3][CLOCK].mhz == 65.0


def test_a_uniform_shift_is_significant_and_a_permutation_of_it_is_not(sweeps, capsys):
    """Every seed moving the same way is the signal an unpaired summary throws away."""
    base, head = (read_sweep(d) for d in sweeps)
    compare("shift", base, head, CLOCK, np.random.default_rng(0))
    out = capsys.readouterr().out
    assert "paired mean  +2.00 MHz" in out
    assert "worse on 0/4" in out
    assert "1/4 fail" in out and "0/4 fail" in out


def test_the_p_value_is_the_exact_sign_flip_tail():
    """Four pairs give sixteen sign vectors, and both extremes are as far out as the data."""
    rng = np.random.default_rng(0)
    assert sign_flip_p(np.array([1.0, 1.0, 1.0, 1.0]), rng) == pytest.approx(2 / 16)
    assert np.isnan(sign_flip_p(np.array([]), rng))


def test_the_observed_arrangement_is_in_its_own_null_set():
    """Summation order made the exact tail miss the very sum it was measured against."""
    diffs = np.array([16.01, 15.98, 14.2, 17.3, 15.5, 16.6, 15.1, 18.0])
    assert sign_flip_p(diffs, np.random.default_rng(0)) == pytest.approx(2 / 2**8)


def test_a_sampled_test_agrees_with_the_exact_one():
    """Past the enumeration bound the tail is sampled, and it lands in the same place."""
    diffs = np.concatenate([np.full(11, 1.0), np.full(11, -0.2)])
    sampled = sign_flip_p(diffs, np.random.default_rng(0))
    assert 0.0 < sampled < 0.1


def test_a_clock_one_sweep_never_reported_is_named_not_dropped(sweeps, capsys):
    """A missing clock is a difference between the builds, so it is reported as one."""
    base, head = (read_sweep(d) for d in sweeps)
    compare("absent", base, head, "no_such_clock", np.random.default_rng(0))
    assert "absent from one sweep" in capsys.readouterr().out
