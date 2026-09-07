"""Placer-seed sweep and timing gate: one synthesis pass, many placements in parallel.

Fmax moves several MHz with the placer seed, so one run is an anecdote; synthesis is the
expensive single-threaded half, so N seeds cost the wall time of one. Exits non-zero if any
clock missed its constraint, since the LiteX build passes ``--timing-allow-fail``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shlex
import statistics
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

#: nextpnr prints an estimate before routing and the real figure after, so the last wins.
FMAX_RE = re.compile(
    r"Max frequency for clock\s+'([^']+)':\s+([\d.]+) MHz\s+\((PASS|FAIL) at ([\d.]+) MHz\)"
)


class Clock(NamedTuple):
    """One clock's post-route result and the constraint nextpnr judged it against."""

    mhz: float
    ok: bool
    target: float


#: LiteX writes ``build_<top>.sh``; the sweep drives that script's own yosys and nextpnr lines.
BUILD_SCRIPT = "build_*.sh"
TOOLS = ("yosys", "nextpnr-ecp5")


def split_build_script(text: str) -> dict[str, list[str]]:
    """Return the yosys and nextpnr argv from a generated LiteX build script."""
    cmds = {}
    for line in text.splitlines():
        stripped = line.strip()
        for tool in TOOLS:
            if stripped.startswith(tool):
                cmds[tool] = shlex.split(stripped)
    missing = sorted(set(TOOLS) - cmds.keys())
    if missing:
        raise ValueError(f"build script has no {', '.join(missing)} line")
    return cmds


def argv_option(argv: list[str], name: str) -> str | None:
    """Value of ``name`` in an argv, or None."""
    return argv[argv.index(name) + 1] if name in argv else None


def seed_argv(argv: list[str], seed: int) -> list[str]:
    """Rewrite a nextpnr argv for one seed, with its own textcfg so runs cannot race."""
    out: list[str] = []
    drop = False
    for tok in argv:
        if drop:
            drop = False
            continue
        if tok in ("--seed", "--textcfg"):
            drop = True
            continue
        out.append(tok)
    return out + ["--textcfg", f"seed{seed}.config", "--seed", str(seed)]


def parse_fmax(log: str) -> dict[str, Clock]:
    """Post-route result by clock name, stripped of nextpnr's ``$glbnet$`` prefix."""
    return {
        name.removeprefix("$glbnet$"): Clock(float(mhz), verdict == "PASS", float(target))
        for name, mhz, verdict, target in FMAX_RE.findall(log)
    }


def summarise(values: list[float]) -> str:
    """Min/median/max of a seed sweep; the spread is the point of running several."""
    if not values:
        return "no result"
    return (
        f"min {min(values):.2f}  median {statistics.median(values):.2f}  "
        f"max {max(values):.2f}  spread {max(values) - min(values):.2f} MHz"
    )


def run_seed(argv: list[str], seed: int, cwd: Path) -> tuple[int, dict[str, Clock], int]:
    """Place and route one seed, keeping its log so a failing seed can be diagnosed."""
    proc = subprocess.run(
        seed_argv(argv, seed), cwd=cwd, capture_output=True, text=True, check=False
    )
    log = proc.stdout + proc.stderr
    (cwd / f"seed{seed}.log").write_text(log, encoding="utf-8")
    return seed, parse_fmax(log), proc.returncode


def synthesise(cmds: dict[str, list[str]], cwd: Path) -> int:
    """Run yosys unless its netlist is already there; that pass dominates the wall time."""
    netlist = argv_option(cmds["nextpnr-ecp5"], "--json")
    if netlist is not None and (cwd / netlist).exists():
        print(f"reusing {netlist}", flush=True)
        return 0
    print(f"synthesising {netlist or '?'} ...", flush=True)
    return subprocess.run(cmds["yosys"], cwd=cwd, check=False).returncode


def sweep(argv: list[str], seeds: range, cwd: Path, workers: int, clock: str) -> dict:
    """Place and route every seed concurrently, reporting each as it lands."""
    results: dict[int, dict[str, Clock]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_seed, argv, seed, cwd) for seed in seeds]
        for future in concurrent.futures.as_completed(futures):
            seed, clocks, status = future.result()
            results[seed] = clocks
            got = clocks.get(clock)
            missed = sorted(name for name, c in clocks.items() if not c.ok)
            note = f"{got.mhz:.2f} MHz" if got else f"exit {status}, no {clock}"
            print(f"  seed {seed}: {note}{'  FAIL: ' + ', '.join(missed) if missed else ''}")
    return results


def report(results: dict[int, dict[str, Clock]], clock: str) -> list[str]:
    """Print the distribution for every clock, the one asked for first; name those that missed."""
    names = [clock] + sorted({k for c in results.values() for k in c} - {clock})
    failures: list[str] = []
    for name in names:
        seen = [c[name] for c in results.values() if name in c]
        bad = [c for c in seen if not c.ok]
        target = f" against {seen[0].target:.2f}" if seen else ""
        note = f"  {len(bad)}/{len(seen)} seeds FAIL" if bad else ""
        print(f"{name}: {summarise([c.mhz for c in seen])}{target}{note}")
        failures += [name] * bool(bad)
    return failures


def main(argv: list[str] | None = None) -> int:
    """Synthesise once, place and route every seed, print the distribution, gate on timing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=".", help="LiteX gateware output directory")
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=0, help="0 means one per seed")
    parser.add_argument("--clock", default="crg_clkout1", help="clock to summarise first")
    args = parser.parse_args(argv)

    cwd = Path(args.dir)
    scripts = sorted(cwd.glob(BUILD_SCRIPT))
    if len(scripts) != 1:
        print(f"expected one {BUILD_SCRIPT} in {cwd}, found {len(scripts)}", file=sys.stderr)
        return 1
    cmds = split_build_script(scripts[0].read_text(encoding="utf-8"))
    failed = synthesise(cmds, cwd)
    if failed:
        print("yosys failed", file=sys.stderr)
        return failed

    seeds = range(1, args.seeds + 1)
    workers = args.jobs or min(len(seeds), os.cpu_count() or 1)
    print(f"placing {args.seeds} seeds, {workers} at a time", flush=True)
    results = sweep(cmds["nextpnr-ecp5"], seeds, cwd, workers, args.clock)
    print()
    missed = report(results, args.clock)
    if not results or not any(results.values()):
        print("no timing reported by any seed", file=sys.stderr)
        return 1
    if missed:
        print(f"\nconstraint missed: {', '.join(missed)}; see seedN.log", file=sys.stderr)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
