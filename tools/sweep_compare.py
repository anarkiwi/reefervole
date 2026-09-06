"""Compare placer-seed sweeps of two builds, paired by seed.

Unpaired min/median/max hides most of the signal: nextpnr is seed-deterministic, so the
same seed on two netlists is a matched pair and the difference between them is the effect.
The test over those differences is an exact sign-flip permutation test, which assumes only
that the two builds are exchangeable under the null.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import numpy as np

from seed_sweep import Clock, parse_fmax

#: Above this many pairs the exact enumeration is 2**n rows, so sample instead.
EXACT_MAX = 20

#: Samples drawn when the pair count puts exact enumeration out of reach.
SAMPLES = 1 << 17


def read_sweep(directory: Path) -> dict[int, dict[str, Clock]]:
    """Every ``seedN.log`` in a build directory, parsed and keyed by seed."""
    out = {}
    for log in directory.glob("seed*.log"):
        stem = log.stem.removeprefix("seed")
        if stem.isdigit():
            clocks = parse_fmax(log.read_text(encoding="utf-8"))
            if clocks:
                out[int(stem)] = clocks
    return out


def sign_flip_p(diffs: np.ndarray, rng: np.random.Generator) -> float:
    """Two-sided p for ``mean(diffs) == 0`` under sign exchangeability."""
    n = len(diffs)
    if n == 0:
        return float("nan")
    if n <= EXACT_MAX:
        bits = (np.arange(1 << n)[:, None] >> np.arange(n)) & 1
        signs = bits.astype(np.int8) * 2 - 1
    else:
        signs = rng.integers(0, 2, size=(SAMPLES, n), dtype=np.int8) * 2 - 1
    totals = signs @ diffs
    # The identity arrangement is in its own null set, so p is never 0; the dot product
    # reassociates the same sum, so the comparison needs a tolerance to say so.
    observed = abs(diffs.sum())
    return float(np.mean(np.abs(totals) >= observed - 1e-9 * max(observed, 1.0)))


def describe(clocks: list[Clock]) -> str:
    """One sweep's distribution for one clock, with the seeds that missed."""
    mhz = [c.mhz for c in clocks]
    bad = sum(not c.ok for c in clocks)
    return (
        f"{min(mhz):6.2f} / {statistics.median(mhz):6.2f} / {max(mhz):6.2f}"
        f"  {bad}/{len(mhz)} fail"
    )


def compare(name: str, base: dict, head: dict, clock: str, rng) -> None:
    """Print both distributions for one clock and the paired difference between them."""
    seeds = sorted(set(base) & set(head))
    pairs = [
        (base[s][clock], head[s][clock]) for s in seeds if clock in base[s] and clock in head[s]
    ]
    if not pairs:
        print(f"{clock}: absent from one sweep")
        return
    diffs = np.array([h.mhz - b.mhz for b, h in pairs])
    print(f"{clock}  ({name}, {len(pairs)} paired seeds)")
    print(f"  base  {describe([b for b, _ in pairs])}")
    print(f"  head  {describe([h for _, h in pairs])}")
    print(
        f"  paired mean {diffs.mean():+6.2f} MHz  median {np.median(diffs):+6.2f}"
        f"  worse on {int((diffs < 0).sum())}/{len(diffs)}  p = {sign_flip_p(diffs, rng):.4g}"
    )


def main(argv: list[str] | None = None) -> int:
    """Report every clock both sweeps share, the one asked for first."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", help="gateware directory of the baseline sweep")
    parser.add_argument("head", help="gateware directory of the sweep under test")
    parser.add_argument("--clock", default="crg_clkout0", help="clock to report first")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for the sampled test")
    args = parser.parse_args(argv)

    base, head = read_sweep(Path(args.base)), read_sweep(Path(args.head))
    shared = sorted(set(base) & set(head))
    if not shared:
        print("no seed logs in common", file=sys.stderr)
        return 1
    names = {k for s in shared for k in base[s]} & {k for s in shared for k in head[s]}
    rng = np.random.default_rng(args.seed)
    for clock in [args.clock] + sorted(names - {args.clock}):
        compare(f"{args.base} -> {args.head}", base, head, clock, rng)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
