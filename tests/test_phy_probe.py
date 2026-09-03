"""Host-side PHY probe against emulated clause-22 slaves.

The slave answers a read with a single turnaround zero, as the board's PHYs do and Linux
``mdiobb.c`` expects. Sampling two shifts every value one bit left, which only shows on a
register whose top bit is set, so the pair below carries the values measured on the bench.
"""

from __future__ import annotations

import io

import pytest

from phy_probe import (
    Clk25,
    MDIOBus,
    MDIOWriteRefused,
    Phy,
    _addr_verdict,
    _clk_verdict,
    _delay_verdict,
    _ident,
    _oui,
    main,
    measure_clk25,
    read_registers,
    report,
    scan,
)

PULLUP = 1
FRAME_EDGES = 64

#: Page 0 and page 0xd08 registers as read from both PHYs of the rev 8.x board.
MEASURED = {
    (0, 0x00): 0x1040,
    (0, 0x01): 0x7989,
    (0, 0x02): 0x001C,
    (0, 0x03): 0xC858,
    (0, 0x18): 0x0118,
    (0, 0x19): 0x0841,
    (0xD08, 0x11): 0x0D09,
    (0xD08, 0x15): 0x0819,
}


class Slave:
    """One clause-22 slave: a ``{(page, reg): value}`` map answering only its own address."""

    def __init__(self, addr, registers=None):
        self.addr = addr
        self.registers = dict(MEASURED if registers is None else registers)
        self.page = 0
        self.written = []
        self._ones = 0
        self._cmd = None
        self._payload = None
        self._out = []

    def _read(self, reg):
        if reg == 31:
            return self.page
        return self.registers.get((self.page, reg), 0x0000)

    def _decode(self):
        bits = self._cmd
        self._cmd = None
        opcode = bits[2] * 2 + bits[3]
        addr = int("".join(map(str, bits[4:9])), 2)
        reg = int("".join(map(str, bits[9:14])), 2)
        if bits[0] or not bits[1] or addr != self.addr:
            return
        if opcode == 0b10:
            value = self._read(reg)
            self._out = [0] + [(value >> i) & 1 for i in reversed(range(16))]
        elif opcode == 0b01:
            self._payload = [reg]

    def _commit(self):
        reg, bits = self._payload[0], self._payload[3:]
        self._payload = None
        value = int("".join(map(str, bits)), 2)
        self.written.append((self.page, reg, value))
        if reg == 31:
            self.page = value
        else:
            self.registers[(self.page, reg)] = value

    def edge(self, line):
        """Take one MDC rising edge; return the level driven next, or None if released."""
        if self._out:
            return self._out.pop(0)
        if self._payload is not None:
            self._payload.append(line)
            if len(self._payload) == 19:
                self._commit()
        elif self._cmd is not None:
            self._cmd.append(line)
            if len(self._cmd) == 14:
                self._decode()
        elif line:
            self._ones += 1
        elif self._ones >= 32:
            self._cmd, self._ones = [0], 0
        else:
            self._ones = 0
        return None


class Csr:
    """One CSR, or a namespace of them: ``write`` and ``read`` bound by whoever owns it."""

    def __init__(self, write=None, read=None):
        self.write = write
        self.read = read


class Bus:
    """The shared MDIO net: a pull-up, the bit-bang CSRs and any number of slaves.

    Doubles as the ``regs`` object ``MDIOBus`` expects, so the probe drives it unchanged.
    """

    def __init__(self, *slaves):
        self.slaves = list(slaves)
        self.line = PULLUP
        self.edges = 0
        self._mdc = 0
        self.mdio_w = Csr(write=self._drive)
        self.mdio_r = Csr(read=lambda: self.line)

    def _drive(self, value):
        mdc, output_enable, bit = value & 1, (value >> 1) & 1, (value >> 2) & 1
        if mdc and not self._mdc:
            self.edges += 1
            sampled = bit if output_enable else self.line
            levels = [lvl for lvl in (s.edge(sampled) for s in self.slaves) if lvl is not None]
            self.line = min(levels) if levels else PULLUP
        self._mdc = mdc


class Client:
    """Enough of ``RemoteClient`` for ``measure_clk25``: a gate that ends after N polls."""

    def __init__(self, count=843577, alive=1, polls=2, sys_clk=31000000, gate=1 << 20):
        self.polls = polls
        self.constants = Csr()
        self.constants.diag_sys_clk_freq = sys_clk
        self.constants.diag_gate_cycles = gate
        self.regs = Csr()
        self.regs.clk25_alive = Csr(read=lambda: alive)
        self.regs.clk25_start = Csr(write=lambda _: None)
        self.regs.clk25_count = Csr(read=lambda: count)
        self.regs.clk25_done = Csr(read=self._done)

    def _done(self):
        self.polls -= 1
        return int(self.polls <= 0)


@pytest.fixture(name="bus")
def bus_fixture():
    """The board's pair: two slaves at the strapped addresses, no broadcast responder."""
    return Bus(Slave(1), Slave(2))


def test_read_recovers_a_register_whose_top_bit_is_set(bus):
    """PHYID2 is the register the two-turnaround-bit reading destroyed."""
    assert MDIOBus(bus).read(1, 3) == 0xC858


def test_read_costs_exactly_one_frame(bus):
    """A read is 64 MDC cycles: preamble, command, turnaround, data, release."""
    MDIOBus(bus).read(1, 2)
    assert bus.edges == FRAME_EDGES


def test_write_costs_exactly_one_frame(bus):
    """A write is the same 64 cycles, with two driven turnaround bits."""
    MDIOBus(bus).write(1, 31, 0xD08)
    assert bus.edges == FRAME_EDGES


def test_read_returns_all_ones_when_nothing_answers(bus):
    """An unstrapped address leaves the pull-up to answer."""
    assert MDIOBus(bus).read(7, 2) == 0xFFFF


def test_a_slave_ignores_another_slaves_address():
    """Address decoding is per slave, not per bus."""
    other = Bus(Slave(2))
    assert MDIOBus(other).read(1, 2) == 0xFFFF
    assert MDIOBus(other).read(2, 2) == 0x001C


def test_paged_read_returns_the_page_and_restores_page_zero(bus):
    """Every extension-page read leaves both PHYs back on page 0."""
    assert MDIOBus(bus).read_paged(1, 0xD08, 0x11) == 0x0D09
    assert [s.page for s in bus.slaves] == [0, 0]


def test_page_select_write_round_trips(bus):
    """Register 31 is the one permitted write, and it takes effect."""
    mdio = MDIOBus(bus)
    mdio.write(1, 31, 0xD08)
    assert mdio.read(1, 31) == 0xD08


@pytest.mark.parametrize("reg, name", [(0, "BMCR"), (24, "PHYCR1"), (25, "PHYCR2")])
def test_write_is_refused_before_any_edge(bus, reg, name):
    """The clock-critical registers are refused inside the write primitive."""
    with pytest.raises(MDIOWriteRefused, match=name):
        MDIOBus(bus).write(1, reg, 0)
    assert bus.edges == 0


def test_scan_finds_exactly_the_strapped_pair(bus):
    """The scan keeps the addresses that answer both ID registers."""
    assert [(p.addr, p.id1, p.id2) for p in scan(MDIOBus(bus))] == [
        (1, 0x001C, 0xC858),
        (2, 0x001C, 0xC858),
    ]


def test_scan_skips_an_address_that_answers_with_zeros():
    """Zeros are as good as no answer: reserved registers read that way."""
    quiet = Bus(Slave(1, {(0, 0x02): 0x0000, (0, 0x03): 0x0000}))
    assert not scan(MDIOBus(quiet))


def test_read_registers_fills_both_straps_and_phycr2(bus):
    """Each responder gets its two delay straps and PHYCR2."""
    phys = scan(MDIOBus(bus))
    read_registers(MDIOBus(bus), phys)
    assert [(p.txreg, p.rxreg, p.phycr2) for p in phys] == [(0x0D09, 0x0819, 0x0841)] * 2


def test_read_registers_leaves_the_broadcast_row_alone(bus):
    """Address 0 is the broadcast row and is never re-read."""
    read_registers(MDIOBus(bus), [Phy(0, 0x001C, 0xC858)])
    assert bus.edges == 0


def test_measure_clk25_scales_the_count_by_the_bitstream_constants():
    """Frequency comes from the gate the bitstream published, not a copied constant."""
    clk = measure_clk25(Client(), timeout=1.0)
    assert clk.alive and clk.done
    assert clk.freq == pytest.approx(24.939e6, rel=1e-4)


def test_measure_clk25_gives_up_when_done_never_arrives():
    """A clk25 that never ticks ends the poll at the timeout with no count."""
    clk = measure_clk25(Client(polls=10**9), timeout=0.02)
    assert not clk.done
    assert clk.count == 0


def test_oui_decodes_realtek():
    """PHYID1:PHYID2 bit-reverse into the printed OUI."""
    assert _oui(0x001C, 0xC858) == "00:e0:4c"


@pytest.mark.parametrize(
    "id2, expected",
    [(0xC916, "RTL8211F"), (0xC858, "not the datasheet's"), (0x0000, "NOT A REALTEK PART")],
)
def test_ident_names_the_part_or_says_it_cannot(id2, expected):
    """Only the datasheet's exact ID earns the RTL8211F name."""
    assert expected in _ident(Phy(1, 0x001C, id2))


@pytest.mark.parametrize(
    "addrs, ok, expected",
    [
        ([1, 2], True, "undecided"),
        ([1, 3], True, "chubby75"),
        ([1, 5], True, "datasheet"),
        ([1], False, "STOP"),
        ([1, 2, 4], False, "STOP"),
    ],
)
def test_addr_verdict(addrs, ok, expected):
    """Two responders resolve the question; 3 or 5 also resolve the strap mapping."""
    got_ok, text = _addr_verdict([Phy(0, 0, 0)] + [Phy(a, 0x001C, 0xC858) for a in addrs])
    assert got_ok is ok
    assert expected in text


@pytest.mark.parametrize(
    "txreg, rxreg, ok, expected",
    [
        (0x0D09, 0x0819, True, "tx_delay=0.0e-9 (0 taps), rx_delay=0.0e-9 (0 taps)"),
        (0x0000, 0x0000, True, "tx_delay=2.0e-9 (80 taps), rx_delay=2.0e-9 (80 taps)"),
        (0xFFFF, 0x0819, False, "read back all ones"),
        (0x0D09, 0xFFFF, False, "read back all ones"),
    ],
)
def test_delay_verdict(txreg, rxreg, ok, expected):
    """Each strap maps to the complementary FPGA-side delay and tap count."""
    got_ok, text = _delay_verdict([Phy(1, 0x001C, 0xC858, txreg, rxreg, 0x0841)])
    assert got_ok is ok
    assert expected in text


def test_delay_verdict_needs_a_responder():
    """A bus with only the broadcast row settles nothing."""
    ok, text = _delay_verdict([Phy(0, 0x001C, 0xC858)])
    assert not ok
    assert "no PHY answered" in text


@pytest.mark.parametrize(
    "clk, phycr2, ok, expected",
    [
        (Clk25(False, False, 0, 0.0), 0x0841, False, "no edge ever seen"),
        (Clk25(True, False, 0, 0.0), 0x0841, False, "never reported done"),
        (Clk25(True, True, 0, 70e6), 0x0841, False, "within 1% of neither"),
        (Clk25(True, True, 0, 24.94e6), 0x0841, True, "CONTRADICTS THE MEASUREMENT"),
        (Clk25(True, True, 0, 24.94e6), 0x0041, True, "AGREES"),
        (Clk25(True, True, 0, 125.1e6), 0x0841, True, "AGREES"),
        (Clk25(True, True, 0, 24.94e6), 0xFFFF, True, "nothing usable"),
    ],
)
def test_clk_verdict(clk, phycr2, ok, expected):
    """The measurement decides, and the verdict says whether PHYCR2[11] agrees."""
    got_ok, text = _clk_verdict(clk, [Phy(1, 0x001C, 0xC858, 0x0D09, 0x0819, phycr2)])
    assert got_ok is ok
    assert expected in text


def test_report_counts_unresolved_verdicts_and_prints_every_row(bus):
    """The report prints each responder and returns the failure count."""
    phys = scan(MDIOBus(bus))
    read_registers(MDIOBus(bus), phys)
    out = io.StringIO()
    unresolved = report(phys, Clk25(True, True, 843577, 24.94e6), out=out)
    text = out.getvalue()
    assert unresolved == 0
    assert "addr  1" in text and "addr  2" in text
    assert "0x11=0x0d09" in text and "0x15=0x0819" in text


def test_report_says_so_when_the_bus_is_silent():
    """A silent bus leaves all three questions open."""
    out = io.StringIO()
    assert report([], Clk25(False, False, 0, 0.0), out=out) == 3
    assert "nothing answered" in out.getvalue()


def test_dry_run_resolves_every_question(monkeypatch, capsys):
    """The canned report exercises the formatting without a board."""
    monkeypatch.setattr("sys.argv", ["phy_probe", "--dry-run"])
    assert main() == 0
    assert "UNRESOLVED" not in capsys.readouterr().out
