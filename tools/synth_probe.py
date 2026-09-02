"""Synthesise one gateware module for the ECP5 and report its real cell usage.

The design's fabric budget in docs/design.md is an estimate until each block is put
through yosys. Usage:

    python3 tools/synth_probe.py reefervole.gateware.buffer:PacketBuffer depth=2048
"""

from __future__ import annotations

import argparse
import ast
import importlib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

from migen import Record, Signal
from migen.fhdl.verilog import convert

#: ECP5 primitives worth reporting, in the order docs/design.md budgets them.
CELLS = ("LUT4", "CCU2C", "PFUMX", "L6MUX21", "TRELLIS_FF", "DP16KD", "TRELLIS_DPR16X4")

#: An ECP5 slice holds two LUT4s, and a CCU2C carry cell occupies both of them.
LUT4_EQUIVALENT = {"LUT4": 1, "CCU2C": 2}


def collect_ios(dut) -> set[Signal]:
    """Every Signal reachable from the module's public Records and status outputs.

    ``stream.Endpoint`` is a ``Record``, as is a flow key, and per-direction interfaces are
    plain lists of either. Anything missed here is pruned by yosys, silently
    under-reporting the block.
    """
    ios: set[Signal] = set()
    for name in dir(dut):
        if name.startswith("_"):
            continue
        for item in _flatten(getattr(dut, name, None)):
            if isinstance(item, Record):
                ios |= {sig for sig, _ in item.iter_flat()}
            elif isinstance(item, Signal):
                ios.add(item)
    return ios


def _flatten(attr):
    """An attribute, or the elements of a per-direction list of them."""
    return attr if isinstance(attr, (list, tuple)) else [attr]


def synthesise(verilog: pathlib.Path, top: str) -> dict[str, int]:
    """Run yosys synth_ecp5 and return a cell-name to count mapping."""
    script = f"read_verilog {verilog}; synth_ecp5 -top {top}; stat"
    result = subprocess.run(["yosys", "-p", script], capture_output=True, text=True, check=False)
    if result.returncode:
        raise SystemExit(f"yosys failed:\n{result.stderr or result.stdout}")
    # yosys stat prints "<count>   <CELL>", count first.
    return {
        name: int(count)
        for count, name in re.findall(r"^\s+(\d+)\s+(\S+)\s*$", result.stdout, re.M)
    }


def elaborate(target: str, params: list[str]):
    """Import ``module:Class`` and construct it from ``name=value`` arguments."""
    module_path, _, class_name = target.partition(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    kwargs = {}
    for item in params:
        name, _, value = item.partition("=")
        kwargs[name] = ast.literal_eval(value)
    return cls(**kwargs), class_name, kwargs


def main() -> int:
    """Elaborate, synthesise and print a one-line-per-cell report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="module.path:ClassName")
    parser.add_argument("params", nargs="*", help="name=value constructor arguments")
    args = parser.parse_args()

    dut, class_name, kwargs = elaborate(args.target, args.params)
    with tempfile.TemporaryDirectory() as tmp:
        verilog = pathlib.Path(tmp) / "top.v"
        verilog.write_text(str(convert(dut, ios=collect_ios(dut), name="top")))
        if shutil.which("yosys") is None:
            raise SystemExit("yosys not on PATH; run inside the reefervole image")
        counts = synthesise(verilog, "top")

    label = f"{class_name}({', '.join(f'{k}={v}' for k, v in kwargs.items())})"
    print(f"{label} on ECP5")
    for name in CELLS:
        if counts.get(name):
            print(f"  {name:<18} {counts[name]:>6}")
    equivalent = sum(counts.get(n, 0) * w for n, w in LUT4_EQUIVALENT.items())
    print(f"  {'= LUT4 equivalent':<18} {equivalent:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
