#!/usr/bin/env python3
"""Probe a Reefervole bench rig and print one compact report.

Runs on the machine the board is wired to. Standard library only, so it works on a bare
host with no project virtualenv. See docs/bench.md.
"""

from __future__ import annotations

import argparse
import glob
import grp
import os
import pathlib
import re
import shutil
import subprocess
import sys

ECP5_25_IDCODE = "0x41111043"
#: USB vendor IDs of the serial and JTAG bridges this bench uses.
BRIDGE_VENDORS = {
    "0403:": "FTDI",
    "10c4:": "SiLabs",
    "1a86:": "WCH",
    "0483:": "ST",
    "2e8a:": "RPi",
    "1366:": "Segger J-Link",
}
NEEDED_GROUPS = ("dialout", "plugdev")


def run(*cmd: str, timeout: int = 20) -> str:
    """Return combined output of ``cmd``, or an empty string if it cannot run."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout + done.stderr


def probe_tools() -> tuple[bool, str]:
    """openFPGALoader is required; a terminal emulator is needed for the CLI."""
    required = {t: shutil.which(t) for t in ("openFPGALoader",)}
    terminals = [t for t in ("picocom", "minicom", "screen", "tio") if shutil.which(t)]
    missing = [t for t, path in required.items() if path is None]
    detail = f"terminals: {', '.join(terminals) or 'none'}"
    if missing:
        return False, f"missing {', '.join(missing)}; {detail}"
    return bool(terminals), detail


def probe_usb() -> tuple[bool, str]:
    """Look for USB serial and JTAG bridges.

    ``openFPGALoader --scan-usb`` is preferred over ``lsusb``: it opens each device and
    reads its string descriptors, so it distinguishes "plugged in" from "we can talk to
    it", which is the failure that permissions and a bound ``ftdi_sio`` actually cause.
    """
    if shutil.which("openFPGALoader"):
        scanned = re.findall(
            r"0x([0-9a-fA-F]{4}):0x([0-9a-fA-F]{4})\s+(\S+)", run("openFPGALoader", "--scan-usb")
        )
        usable = [
            f"{v}:{p} {probe}" for v, p, probe in scanned if f"{v.lower()}:" in BRIDGE_VENDORS
        ]
        if usable:
            return True, "openable via libusb: " + "; ".join(usable)
    found = []
    if shutil.which("lsusb"):
        found = [
            line.split(" ", 5)[-1]
            for line in run("lsusb").splitlines()
            if any(v in line for v in BRIDGE_VENDORS)
        ]
    else:
        for vid_path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            vid = pathlib.Path(vid_path).read_text(encoding="ascii").strip()
            pid_path = pathlib.Path(vid_path).with_name("idProduct")
            if f"{vid}:" in BRIDGE_VENDORS and pid_path.exists():
                found.append(f"{vid}:{pid_path.read_text(encoding='ascii').strip()}")
    expected = ", ".join(sorted(set(BRIDGE_VENDORS.values())))
    if found:
        return True, "present but not probed for libusb access: " + "; ".join(found)
    return False, f"none found; expected one of: {expected}"


def probe_serial() -> tuple[bool, str]:
    """Stable by-id serial paths; these survive replugging, /dev/ttyUSB0 does not."""
    ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if not ports:
        ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        return bool(ports), f"no by-id paths; raw: {', '.join(ports) or 'none'}"
    return True, "; ".join(ports)


def probe_permissions() -> tuple[bool, str]:
    """Group membership and udev rules are what allow non-root adapter access."""
    mine = {grp.getgrgid(gid).gr_name for gid in os.getgroups()}
    missing = [g for g in NEEDED_GROUPS if g not in mine and _group_exists(g)]
    rules = [
        path
        for directory in ("/etc/udev/rules.d", "/usr/lib/udev/rules.d", "/lib/udev/rules.d")
        for path in glob.glob(f"{directory}/*.rules")
        if "openfpgaloader" in os.path.basename(path).lower()
    ]
    detail = f"groups ok: {not missing}"
    if missing:
        detail = f"not in {', '.join(missing)} (log out and back in after usermod)"
    return not missing and bool(rules), f"{detail}; udev rules: {len(rules)}"


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def probe_jtag(cable: str) -> tuple[bool, str]:
    """Scan the JTAG chain and check the IDCODE is an ECP5-25."""
    if shutil.which("openFPGALoader") is None:
        return False, "openFPGALoader not installed"
    out = run("openFPGALoader", "-c", cable, "--detect", timeout=30)
    idcodes = re.findall(r"0x[0-9a-fA-F]{8}", out)
    if ECP5_25_IDCODE in (code.lower() for code in idcodes):
        return True, f"LFE5U-25 found, idcode {ECP5_25_IDCODE}"
    if idcodes:
        return False, f"unexpected idcode(s) {', '.join(idcodes)} — check TDI/TDO wiring"
    first = next((ln for ln in out.splitlines() if ln.strip()), "no output")
    return False, f"no device: {first.strip()[:90]}"


def probe_nics() -> tuple[bool, str]:
    """Candidate NIC pair: up-capable, carrying no addresses, not enslaved."""
    free, busy = [], []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if name == "lo" or (pathlib.Path(path) / "device").exists() is False:
            continue
        addrs = run("ip", "-o", "addr", "show", "dev", name)
        enslaved = (pathlib.Path(path) / "master").exists()
        scoped = [ln for ln in addrs.splitlines() if "scope global" in ln]
        (busy if scoped or enslaved else free).append(name)
    detail = f"free: {', '.join(free) or 'none'}"
    if busy:
        detail += f"; in use: {', '.join(busy)}"
    namespaces = {line.split()[0] for line in run("ip", "netns", "list").splitlines() if line}
    present = [ns for ns in ("bsw-a", "bsw-b") if ns in namespaces]
    if present:
        detail += f"; netns present: {', '.join(present)}"
    return len(free) >= 2, detail


def main() -> int:
    """Run every probe and print a report; return non-zero if any probe failed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cable", default="ft232", help="openFPGALoader cable name (--list-cables)"
    )
    args = parser.parse_args()

    # Phase 1a is JTAG and UART only: it settles the PHY straps, addresses and clock
    # source. Phase 1b needs the two Ethernet adapters. See docs/bench.md section 5b.
    probes = (
        ("toolchain", "1a", probe_tools),
        ("usb adapters", "1a", probe_usb),
        ("serial ports", "1a", probe_serial),
        ("permissions", "1a", probe_permissions),
        ("jtag chain", "1a", lambda: probe_jtag(args.cable)),
        ("nic pair", "1b", probe_nics),
    )
    print(f"reefervole bench check  ({os.uname().sysname} {os.uname().release}, cable={args.cable})")
    blocked = set()
    for name, phase, probe in probes:
        ok, detail = probe()
        if not ok:
            blocked.add(phase)
        print(f"  [{'PASS' if ok else 'FAIL'}] {phase}  {name:<13} {detail}")
    for phase, what in (("1a", "PHY interrogation over JTAG and UART"), ("1b", "traffic tests")):
        state = "BLOCKED" if phase in blocked else "ready"
        print(f"  phase {phase} {state:<8} {what}")
    return 1 if "1a" in blocked else 0


if __name__ == "__main__":
    sys.exit(main())
