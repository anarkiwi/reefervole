"""Direct-mapped MAC address table in block RAM, with epoch aging.

Shape: 1024 buckets of ``{mac[48], port[1], valid[1], tick[8]}``. On an ECP5 a DP16KD is
x18 per port in true dual-port mode, so a memory costs ``ceil(width/18)`` blocks whatever
its depth: 58 bits is four blocks, and the same four blocks would hold 2048 buckets.

Hashing folds the 48-bit MAC by XOR into the bucket index rather than running a CRC. The
low 24 bits of a MAC are the station-specific half and carry the entropy; folding mixes
them with the OUI in one XOR tree, about two LUT4 per index bit and no state, where a CRC
would need a full 48-bit polynomial reduction for avalanche this design does not need. A
1024-bucket table holding the tens of MACs a two-port switch sees collides rarely, and the
full MAC is stored as a tag, so a collision evicts the previous occupant and its traffic is
then flooded. Flooding is the safe failure direction; the table cannot mis-forward.

Aging compares the entry's stored epoch against the current one, so no sweep walks the
table. A stale entry is invalidated by the access that finds it. ``age_limit`` must not
exceed half the epoch space, or an entry whose epoch wrapped fully around would read as
fresh; that needs a bucket untouched for a whole epoch cycle, which is what sizing the
epoch prescaler is for.

Throughput: at 1 Gb/s a port sees a minimum-size frame, preamble and interpacket gap
included, no more often than every 84 cycles of a 125 MHz clock. Two ports need two
lookups and two learns in that window, and this FSM completes each in three cycles, so a
single shared table on one memory port is not a bottleneck and needs no pipelining.
"""

from __future__ import annotations

import functools
import operator

from litex.soc.interconnect import stream
from migen import Cat, FSM, If, Memory, Module, NextState, NextValue, Signal, log2_int

MAC_BITS = 48
#: Indices into the table's event pulses, one per counter.
LEARNED, MOVED, AGED = range(3)


class Bucket:
    """The fields of one bucket, packed least significant first into a memory word."""

    def __init__(self, tick_bits: int):
        self.mac = Signal(MAC_BITS)
        self.port = Signal()
        self.valid = Signal()
        self.tick = Signal(tick_bits)

    def raw_bits(self):
        """The memory word this bucket occupies."""
        return Cat(self.mac, self.port, self.valid, self.tick)


def fold_hash(mac, bits: int):
    """XOR-fold a MAC-wide value down to ``bits``, least significant chunk first."""
    return functools.reduce(operator.xor, [mac[i : i + bits] for i in range(0, len(mac), bits)])


class MacTable(Module):
    """Learn and lookup ports onto one shared bucket array.

    ``learn`` and ``lookup`` are stream endpoints carrying a MAC (and, to learn, a port);
    ``result`` reports the lookup one to three cycles later. Learn wins arbitration, so a
    caller that presents a learn and then waits for its ready before presenting the lookup
    gets exactly the reference model's order: learn from the source, then look up the
    destination against the table the learn just updated.
    """

    def __init__(
        self, buckets: int = 1024, tick_bits: int = 8, age_limit: int = 128, counter_bits: int = 32
    ):
        if age_limit > 1 << (tick_bits - 1):
            raise ValueError("age_limit must not exceed half the epoch space")
        index_bits = log2_int(buckets)
        self.lookup = stream.Endpoint([("mac", MAC_BITS)])
        self.result = stream.Endpoint([("hit", 1), ("port", 1)])
        self.learn = stream.Endpoint([("mac", MAC_BITS), ("port", 1)])
        self.tick = Signal(tick_bits)
        self.learned = Signal(counter_bits)
        self.moved = Signal(counter_bits)
        self.aged = Signal(counter_bits)

        # One adder per counter, pulsed from the states, rather than a NextValue per state:
        # migen would otherwise duplicate the incrementer into every state that counts.
        events = [Signal() for _ in range(3)]
        self.sync += [
            If(event, counter.eq(counter + 1))
            for event, counter in zip(events, (self.learned, self.moved, self.aged))
        ]

        entry = Bucket(tick_bits)
        mem = Memory(len(entry.raw_bits()), buckets)
        mport = mem.get_port(write_capable=True)
        self.specials += mem, mport

        mac = Signal(MAC_BITS)
        port = Signal()
        stale = Signal()
        # Truncating to tick_bits makes the difference modular; migen would otherwise
        # widen it to a signed value and read a wrapped epoch as freshly stamped.
        age = Signal(tick_bits)
        self.comb += [
            entry.raw_bits().eq(mport.dat_r),
            age.eq(self.tick - entry.tick),
            stale.eq(entry.valid & ~(age < age_limit)),
        ]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act(
            "IDLE",
            If(
                self.learn.valid,
                mport.adr.eq(fold_hash(self.learn.mac, index_bits)),
                NextValue(mac, self.learn.mac),
                NextValue(port, self.learn.port),
                self.learn.ready.eq(1),
                NextState("LEARN"),
            ).Elif(
                self.lookup.valid,
                mport.adr.eq(fold_hash(self.lookup.mac, index_bits)),
                NextValue(mac, self.lookup.mac),
                self.lookup.ready.eq(1),
                NextState("LOOKUP"),
            ),
        )
        fsm.act(
            "LEARN",
            mport.adr.eq(fold_hash(mac, index_bits)),
            mport.dat_w.eq(Cat(mac, port, 1, self.tick)),
            mport.we.eq(1),
            If(stale, events[AGED].eq(1)),
            If(~entry.valid | stale | (entry.mac != mac), events[LEARNED].eq(1)).Elif(
                entry.port != port, events[MOVED].eq(1)
            ),
            NextState("IDLE"),
        )
        fsm.act(
            "LOOKUP",
            mport.adr.eq(fold_hash(mac, index_bits)),
            mport.we.eq(stale),
            If(stale, events[AGED].eq(1)),
            NextValue(self.result.valid, 1),
            NextValue(self.result.hit, entry.valid & ~stale & (entry.mac == mac)),
            NextValue(self.result.port, entry.port),
            NextState("RESULT"),
        )
        fsm.act(
            "RESULT",
            If(self.result.ready, NextValue(self.result.valid, 0), NextState("IDLE")),
        )
