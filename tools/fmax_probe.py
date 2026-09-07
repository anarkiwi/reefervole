"""Per-block Fmax on the ECP5: yosys then nextpnr-ecp5, one module at a time.

``reefervole.synth`` reports a block's area; this reports the clock it closes alone on an
empty device, which bounds -- and never predicts -- a full build's. Run it in the
reefervole image: ``python3 tools/fmax_probe.py reefervole.gateware.buffer:PacketBuffer``.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

from migen.fhdl.verilog import convert

from reefervole.synth import collect_ios, elaborate

#: Rev 8.2's part, docs/design.md section 3.
DEVICE = ("--25k", "--package", "CABGA256", "--speed", "7")

_FMAX = re.compile(r"Max frequency for clock\s+'(?P<clock>[^']+)':\s+(?P<mhz>[\d.]+) MHz")
_PORT = re.compile(r"^\s*(input|output)\s+(?:reg\s+)?(?:\[(\d+):0\]\s+)?(\w+)", re.M)
_CLOCKS = ("sys_clk", "sys_rst")


def _clock_name(net: str) -> str:
    """``$glbnet$sys_clk$TRELLIS_IO_IN`` back to ``sys_clk``."""
    return net.split("$")[2] if net.startswith("$glbnet$") else net


def harness(verilog: str) -> str:
    """A wrapper driving every input of ``top`` from a shift register and XORing its outputs.

    A block's ports outnumber the package's pins several times over, so it cannot be placed
    as a top level. Registering both sides costs two pins and leaves every path inside the
    block a register-to-register one, which is what its Fmax is.
    """
    ports = [(m[1], int(m[2] or 0) + 1, m[3]) for m in _PORT.finditer(verilog)]
    ins = [(width, name) for kind, width, name in ports if kind == "input" and name not in _CLOCKS]
    outs = [(width, name) for kind, width, name in ports if kind == "output"]
    depth, at = sum(width for width, _ in ins), 0
    lines = [f"wire [{width - 1}:0] {name};" for width, name in ins]
    for width, name in ins:
        lines.append(f"assign {name} = chain[{at + width - 1}:{at}];")
        at += width
    conn = ", ".join(f".{name}({name})" for _, name in ins + outs)
    return (
        f"module harness(input sys_clk, input sys_rst, input stim, output reg obs);\n"
        f"reg [{depth - 1}:0] chain;\n"
        + "\n".join(lines)
        + "\n"
        + "\n".join(f"wire [{width - 1}:0] {name};" for width, name in outs)
        + "\n"
        f"top dut(.sys_clk(sys_clk), .sys_rst(sys_rst), {conn});\n"
        f"always @(posedge sys_clk) begin\n"
        f"  chain <= {{chain[{depth - 2}:0], stim}};\n"
        f"  obs <= ^{{{', '.join(name for _, name in outs)}}};\n"
        f"end\nendmodule\n"
    )


def _run(command: list[str], what: str, check: bool = True) -> str:
    """Run a tool, returning its combined output; a failure ends the probe."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    if result.returncode and check:
        raise SystemExit(f"{what} failed:\n{output}")
    return output


def _netlist(dut, tmp: pathlib.Path) -> pathlib.Path:
    """Convert ``dut``, wrap it in the harness and synthesise; return the JSON netlist."""
    verilog, wrapper, netlist = tmp / "top.v", tmp / "harness.v", tmp / "top.json"
    verilog.write_text(str(convert(dut, ios=collect_ios(dut), name="top")))
    wrapper.write_text(harness(verilog.read_text()))
    script = f"read_verilog {verilog} {wrapper}; synth_ecp5 -top harness -json {netlist}"
    _run(["yosys", "-p", script], "yosys")
    return netlist


def probe(target: str, params: list[str], freq: float, seed: int) -> tuple[str, dict[str, float]]:
    """Elaborate, synthesise and place-and-route ``target``; return its clocks' Fmax."""
    for tool in ("yosys", "nextpnr-ecp5"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not on PATH; run inside the reefervole image")
    dut, class_name, kwargs = elaborate(target, params)
    with tempfile.TemporaryDirectory() as tmp:
        # Missing the target frequency is a nextpnr error exit, and is the usual result.
        report = _run(
            ["nextpnr-ecp5", "--json", str(_netlist(dut, pathlib.Path(tmp))), *DEVICE]
            + ["--lpf-allow-unconstrained", "--textcfg", "/dev/null"]
            + ["--freq", str(freq), "--seed", str(seed)],
            "nextpnr-ecp5",
            check=False,
        )
    clocks = {_clock_name(m["clock"]): float(m["mhz"]) for m in _FMAX.finditer(report)}
    if not clocks:
        raise SystemExit(f"nextpnr-ecp5 reported no clock:\n{report}")
    label = f"{class_name}({', '.join(f'{k}={v}' for k, v in kwargs.items())})"
    return label, clocks


def main() -> int:
    """Print one line per clock domain: its estimated maximum frequency."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="module.path:ClassName")
    parser.add_argument("params", nargs="*", help="name=value constructor arguments")
    parser.add_argument("--freq", type=float, default=125.0, help="target MHz for placement")
    parser.add_argument("--seed", type=int, default=1, help="nextpnr seed, fixed for repeatability")
    args = parser.parse_args()

    label, clocks = probe(args.target, args.params, args.freq, args.seed)
    print(f"{label} on ECP5 {' '.join(DEVICE)}")
    for clock, mhz in sorted(clocks.items()):
        print(f"  {clock:<18} {mhz:>8.2f} MHz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
