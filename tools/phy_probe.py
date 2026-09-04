"""Host-side board probe: MDIO scan, RGMII delay straps and clk25, over jtagbone.

Talks to the gateware built by ``reefervole.gateware.diagnostics`` through
``litex_server --jtag``. This probe itself only reads, because on this board PHY1 clocks
the whole FPGA; see _FORBIDDEN_BITS and _BRINGUP, which --help prints along with the exact
litex_server invocation and OpenOCD config the bridge needs.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from litex.tools.litex_client import RemoteClient

_BRINGUP = """\
Bridge. From the repository root, with the diagnostics bitstream in SRAM:

  openFPGALoader -b colorlight -c ft232 -m build/diagnostics/gateware/diagnostics.bit
  python3 tools/jtagbone_server.py &
  python3 tools/phy_probe.py --csr-csv build/diagnostics/csr.csv

tools/jtagbone_server.py is `litex_server --jtag` with two fixes it will not run without.
Its OpenOCD config, tools/openocd_colorlight_5a_75b.cfg, sets _CHIPNAME: LiteX's jtagstream
helper builds the tap name as the literal "$_CHIPNAME.tap", and the config litex_boards
ships names its tap directly, so litex_server dies dereferencing an unset variable after the
TAP has already been found. And it disables Nagle on the socket to OpenOCD, which LiteX
feeds one byte per send(); with the peer's delayed ACK that costs 40 ms on every CSR read.
openFPGALoader and the bridge both want the FT232H exclusively, so run them in that order.

MDIO timing. MDC is bit-banged one CSR write per half cycle, so the JTAG round trip sets
its rate, three orders of magnitude below the 2.5 MHz clause-22 ceiling. Clause 22 permits
MDC to be stopped indefinitely, so no minimum rate applies and no host-side delay is
needed. Driven bits change while MDC is low and are latched by the PHY on the rising edge;
sampled bits are read while MDC is high, long after the PHY's 300 ns max output delay. A
read samples ONE turnaround bit: the master has released MDIO for the first of the two, so
the first level it can sample is already the PHY's zero. Sampling two shifts every value one
bit left, which only shows on a register whose top bit is set -- here PHYID2, which then
reads 0xffff and makes a live bus look empty.

Safety. Rev 8.x shares PHYRSTB across both PHYs and takes the FPGA's 25 MHz from PHY1, so
a reset, power-down, CLKOUT gate or ALDPS write stops the design until the board is power
cycled. Those bits live in registers 0, 24 and 25, so the guard lives inside
MDIOBus.write(), before any write frame, rather than at call sites where a later edit would
forget it. The guard is per bit, not per register: BMCR bit 10 (isolate) is measured to
leave CLKOUT running (docs/rtl8211f.md section 8.7) and is writable, while bits 15, 11 and
the unmeasured 9 of that same register are not. This probe itself writes only register 31,
the page select, and always restores it.\
"""

#: Clock-critical bits, per register; BMCR 10 (isolate) is absent by measurement.
_FORBIDDEN_BITS = {0: (0x8A00, "BMCR"), 24: (0x1006, "PHYCR1"), 25: (0x0801, "PHYCR2")}

_PAGE_SELECT = 31
_PAGE_RGMII = 0xD08
_TXDLY = (0x11, 8)
_RXDLY = (0x15, 3)
_PHYCR2 = 25

#: ECP5 DELAYG resolution, and the fixed delay each RTL8211F strap inserts.
_TAP_S = 25e-12
_STRAP_S = 2e-9

#: PHYAD[1] is carried by RXC per the datasheet and by RXCTL per chubby75; only these two
#: addresses distinguish the readings, and nothing here computes an address from a strap.
_ADDR_SOURCE = {
    3: "chubby75 (RXCTL = PHYAD[1], RXC = PHYAD[2])",
    5: "the datasheet (RXC = PHYAD[1], RXCTL = PHYAD[2])",
}

#: PHYID1:PHYID2 the datasheet gives for the RTL8211F; the parts fitted here report a
#: different model and revision under the same Realtek OUI, so identify by OUI.
_DS_PHYID = 0x001CC916
_REALTEK_OUI = "00:e0:4c"

_CLK_CANDIDATES = (25e6, 125e6)
_CLK_TOLERANCE = 0.01


class MDIOWriteRefused(RuntimeError):
    """A write was attempted to a register that can stop the FPGA's clock."""


class MDIOBus:
    """IEEE 802.3 clause-22 MDIO bit-banged over the mdio_w/mdio_r CSRs."""

    def __init__(self, regs):
        self._w = regs.mdio_w
        self._r = regs.mdio_r

    def _edge(self, oe, bit):
        self._w.write((bit << 2) | (oe << 1))
        self._w.write((bit << 2) | (oe << 1) | 1)

    def _out(self, value, nbits):
        for i in reversed(range(nbits)):
            self._edge(1, (value >> i) & 1)

    def _in(self, nbits):
        value = 0
        for _ in range(nbits):
            self._edge(0, 0)
            value = (value << 1) | (self._r.read() & 1)
        return value

    def _frame(self, op, phy, reg):
        self._out((1 << 32) - 1, 32)
        self._out(0b01, 2)
        self._out(op, 2)
        self._out(phy, 5)
        self._out(reg, 5)

    def read(self, phy, reg):
        """Clause-22 read; 0xffff if no PHY drove the turnaround bit low.

        One sampled turnaround bit, not two. The STA releases MDIO during the first
        turnaround bit time, so the first level it can sample is already the PHY's zero;
        the sixteen data bits follow immediately and the trailing released cycle below is
        what keeps the frame 64 bits long. Sampling two shifts every value one place left,
        which is invisible on any register whose top bit is 0 -- BMCR, BMSR and PHYID1 all
        still look plausible -- and turns a PHYID2 with bit 15 set into 0xffff, i.e. into
        "no PHY here". A whole bus of healthy PHYs then scans as empty. Linux
        ``mdiobb.c`` samples one bit here for the same reason.
        """
        self._frame(0b10, phy, reg)
        turnaround = self._in(1)
        value = self._in(16)
        self._edge(0, 0)
        self._w.write(0)
        return 0xFFFF if turnaround else value

    def write(self, phy, reg, value):
        """Clause-22 write, refused structurally if it would disturb a clock-critical bit.

        The guard compares the proposed value against what the register currently holds
        rather than demanding zeroes, because the safe value is not the same for every
        protected bit: BMCR's reset and power-down are safe only as 0, while PHYCR2's
        CLKOUT enable is safe only as the 1 the board came up with. "Leave these bits
        exactly as they are" is the rule that is right for both, and the extra read is a
        cheap price for a guard that a plausible-looking constant cannot defeat.
        """
        mask, name = _FORBIDDEN_BITS.get(reg, (0, ""))
        if mask and (value ^ self.read(phy, reg)) & mask:
            raise MDIOWriteRefused(
                f"refusing to write 0x{value:04x} to PHY {phy} register {reg} ({name}): it "
                f"changes bits 0x{mask:04x}, which reset, power down or gate the CLKOUT that "
                "clocks this FPGA"
            )
        self._frame(0b01, phy, reg)
        self._out(0b10, 2)
        self._out(value, 16)
        self._w.write(0)

    def read_paged(self, phy, page, reg):
        """Read reg on a Realtek extension page, always restoring page 0."""
        self.write(phy, _PAGE_SELECT, page)
        try:
            return self.read(phy, reg)
        finally:
            self.write(phy, _PAGE_SELECT, 0)


@dataclass
class Phy:
    """One responding MDIO address and the raw registers read from it."""

    addr: int
    id1: int
    id2: int
    txreg: int = 0xFFFF
    rxreg: int = 0xFFFF
    phycr2: int = 0xFFFF


@dataclass
class Clk25:
    """One clk25 gate measurement."""

    alive: bool
    done: bool
    count: int
    freq: float


def _bit(reg, pos):
    return None if reg == 0xFFFF else (reg >> pos) & 1


def _oui(id1, id2):
    """IEEE OUI from PHYID1/2: 22 bits, bit-reversed per octet (clause 22.2.4.3.1)."""
    seq = ((id1 << 6) | (id2 >> 10)) & 0x3FFFFF
    octets = (int(f"{(seq >> (16 - 8 * i)) & 0xFF:08b}"[::-1], 2) for i in range(3))
    return ":".join(f"{o:02x}" for o in octets)


def _ident(phy):
    """OUI, model and revision, and whether the part is the one the datasheet describes."""
    oui = _oui(phy.id1, phy.id2)
    if (phy.id1 << 16) | phy.id2 == _DS_PHYID:
        part = "RTL8211F"
    elif oui == _REALTEK_OUI:
        part = f"Realtek, but not the datasheet's 0x{_DS_PHYID:08x}"
    else:
        part = "NOT A REALTEK PART"
    return f"OUI {oui} model 0x{(phy.id2 >> 4) & 0x3F:02x} rev {phy.id2 & 0xF} {part}"


def _delay_plan(strap):
    """FPGA-side delay complementing the PHY's own: 2 ns when the strap is off, else none."""
    seconds = _STRAP_S if strap == 0 else 0.0
    return seconds, round(seconds / _TAP_S)


def scan(bus):
    """Read ID registers 2 and 3 at every address; keep the ones that answer both."""
    found = []
    for addr in range(32):
        id1 = bus.read(addr, 2)
        if id1 in (0x0000, 0xFFFF):
            continue
        id2 = bus.read(addr, 3)
        if id2 not in (0x0000, 0xFFFF):
            found.append(Phy(addr, id1, id2))
    return found


def read_registers(bus, phys):
    """Fill in the two RGMII delay strap registers and PHYCR2 for each responder.

    Address 0 is skipped: it is the clause-22 broadcast, and a page select written there
    would land on every PHY on the bus at once.
    """
    for phy in (p for p in phys if p.addr):
        phy.txreg = bus.read_paged(phy.addr, _PAGE_RGMII, _TXDLY[0])
        phy.rxreg = bus.read_paged(phy.addr, _PAGE_RGMII, _RXDLY[0])
        phy.phycr2 = bus.read(phy.addr, _PHYCR2)


def measure_clk25(client, timeout):
    """Pulse the gate, poll done, and scale the count by the bitstream's own constants."""
    regs, consts = client.regs, client.constants
    alive = bool(regs.clk25_alive.read() & 1)
    regs.clk25_start.write(1)
    deadline = time.monotonic() + timeout
    done = False
    while not done and time.monotonic() < deadline:
        done = bool(regs.clk25_done.read() & 1)
        if not done:
            time.sleep(0.005)
    count = regs.clk25_count.read() if done else 0
    freq = count * consts.diag_sys_clk_freq / consts.diag_gate_cycles
    return Clk25(alive, done, count, freq)


def _addr_verdict(phys):
    """Two non-broadcast responders is the whole requirement; the strap mapping is an aside."""
    strapped = sorted(p.addr for p in phys if p.addr)
    if len(strapped) != 2:
        return False, (
            f"PHY addresses: expected exactly two non-broadcast responders, got "
            f"{len(strapped)} {strapped} -- STOP, THE BOARD IS NOT WHAT WE ASSUMED"
        )
    vendors = {_oui(p.id1, p.id2) for p in phys if p.addr}
    if vendors != {_REALTEK_OUI}:
        return (
            False,
            f"PHY addresses: {strapped} answered, but OUIs {sorted(vendors)} are not Realtek",
        )
    source = next((_ADDR_SOURCE[a] for a in strapped if a in _ADDR_SOURCE), None)
    aside = (
        f", and vindicates {source} for PHYAD[1]"
        if source
        else "; neither 3 nor 5 is among them, so the RXC-versus-RXCTL strap mapping stays "
        "undecided, which nothing here depends on -- addresses are scanned, never computed"
    )
    return True, f"PHY addresses -> {strapped}{aside}"


def _delay_verdict(phys):
    parts = []
    for phy in (p for p in phys if p.addr):
        txdly, rxdly = _bit(phy.txreg, _TXDLY[1]), _bit(phy.rxreg, _RXDLY[1])
        if txdly is None or rxdly is None:
            return False, f"RGMII delay straps: PHY {phy.addr} page 0xd08 read back all ones"
        tx_s, tx_taps = _delay_plan(txdly)
        rx_s, rx_taps = _delay_plan(rxdly)
        parts.append(
            f"PHY {phy.addr} TXDLY={txdly} RXDLY={rxdly} => tx_delay={tx_s * 1e9:.1f}e-9 "
            f"({tx_taps} taps), rx_delay={rx_s * 1e9:.1f}e-9 ({rx_taps} taps)"
        )
    if not parts:
        return False, "RGMII delay straps: no PHY answered"
    return True, "RGMII delay straps -> " + "; ".join(parts)


def _clk_verdict(clk, phys):
    if not clk.alive:
        return False, "clk25: no edge ever seen on P6 -- the clock is absent, not merely wrong"
    if not clk.done:
        return False, "clk25: the gate never reported done within the timeout"
    nearest = min(_CLK_CANDIDATES, key=lambda f: abs(clk.freq - f))
    error = (clk.freq - nearest) / nearest
    bits = {_bit(p.phycr2, 11) for p in phys if p.addr}
    says = {frozenset({0}): "25 MHz", frozenset({1}): "125 MHz"}.get(
        frozenset(bits), "nothing usable"
    )
    if abs(error) > _CLK_TOLERANCE:
        return False, (
            f"clk25: {clk.freq / 1e6:.4f} MHz is within {_CLK_TOLERANCE:.0%} of neither 25 nor "
            f"125 MHz; PHYCR2[11] says {says}"
        )
    agrees = "AGREES" if (nearest == 125e6) == (bits == {1}) else "CONTRADICTS THE MEASUREMENT"
    return True, (
        f"clk25 on P6 -> {clk.freq / 1e6:.4f} MHz, i.e. {nearest / 1e6:.0f} MHz {error:+.2%}; "
        f"PHYCR2[11] reads {says}, which {agrees}"
    )


def report(phys, clk, out=sys.stdout):
    """Print the readings and one verdict line per open question; return the failure count."""

    def line(text=""):
        print(text, file=out)

    line("== MDIO scan, addresses 0-31 ==")
    for phy in phys:
        tag = " broadcast" if phy.addr == 0 else "          "
        line(f"  addr {phy.addr:2d}{tag}  id 0x{phy.id1:04x}.{phy.id2:04x}  {_ident(phy)}")
    if not phys:
        line("  nothing answered")

    line()
    line("== RGMII delay straps, page 0xd08 (the FPGA's delays complement the PHY's) ==")
    for phy in (p for p in phys if p.addr):
        line(
            f"  addr {phy.addr:2d}  0x11=0x{phy.txreg:04x} bit8={_bit(phy.txreg, _TXDLY[1])}  "
            f"0x15=0x{phy.rxreg:04x} bit3={_bit(phy.rxreg, _RXDLY[1])}  "
            f"PHYCR2=0x{phy.phycr2:04x} bit11={_bit(phy.phycr2, 11)}"
        )
    line("  legend: strap 0 -> the FPGA adds 2 ns (80 taps); strap 1 -> it adds none")

    line()
    line("== clk25, FPGA pin P6 from PHY1 CLKOUT ==")
    line(
        f"  alive={'yes' if clk.alive else 'NO'} done={'yes' if clk.done else 'NO'} "
        f"count={clk.count} -> {clk.freq / 1e6:.4f} MHz"
    )

    line()
    verdicts = [_addr_verdict(phys), _delay_verdict(phys), _clk_verdict(clk, phys)]
    for ok, text in verdicts:
        line(f"{'RESOLVED' if ok else 'UNRESOLVED'}: {text}")
    return sum(not ok for ok, _ in verdicts)


def _dry_run():
    """Canned readings in the shape a healthy rev 8.x board returns, for exercising --help."""
    phys = [Phy(a, 0x001C, 0xC858, 0x0D09, 0x0819, 0x0841) for a in (1, 2)]
    count = 843577
    return phys, Clk25(True, True, count, count * 31000000 / (1 << 20))


def main():
    """Probe the board over jtagbone, or format a canned report with --dry-run."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=_BRINGUP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csr-csv", default="build/diagnostics/csr.csv", help="Bitstream CSR map.")
    parser.add_argument("--host", default="localhost", help="litex_server bind address.")
    parser.add_argument("--port", type=int, default=1234, help="litex_server bind port.")
    parser.add_argument("--timeout", type=float, default=2.0, help="clk25 done poll timeout, s.")
    parser.add_argument("--dry-run", action="store_true", help="Canned report, no connection.")
    args = parser.parse_args()

    if args.dry_run:
        print("(dry run: canned values, no board and no litex_server)")
        return report(*_dry_run())

    client = RemoteClient(host=args.host, port=args.port, csr_csv=args.csr_csv)
    client.open()
    try:
        bus = MDIOBus(client.regs)
        phys = scan(bus)
        read_registers(bus, phys)
        return report(phys, measure_clk25(client, args.timeout))
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
