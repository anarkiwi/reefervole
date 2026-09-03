"""Two-port L2 learning switch datapath.

Each direction owns a MAC extractor, a request path into the shared :class:`MacTable`,
and a store-and-forward buffer. With two ports the egress of a frame is decided by
whether it survives at all: forwarding a frame back out of its own ingress port is what
filtering means, so every frame that leaves leaves on the other port and no crossbar is
needed. Ingress path 0 therefore drives egress port 1 and vice versa.

The verdict is available 12 bytes into the frame plus a bounded table access, so unlike a
design that must inspect the whole frame it is known before the end of frame arrives. That
keeps the buffer simple: no per-word gate and no second copy, just words written as they
arrive and, at end of frame, one pointer update that either publishes them by advancing the
commit pointer or discards them by rewinding the write pointer to where the frame started.

Ethernet receive cannot be back-pressured, so ``sink.ready`` is tied high and a frame that
does not fit is dropped whole by that same rewind, never truncated. ``stream.SyncFIFO``
and ``packet.PacketFIFO`` stall upstream instead, which on a receive path means dropping a
frame in the middle and emitting the fragment.
"""

from __future__ import annotations

from liteeth.common import eth_phy_description
from litex.soc.interconnect import stream
from migen import C, Case, Cat, FSM, If, Memory, Module, Mux, NextState, NextValue
from migen import Signal, log2_int

from reefervole.gateware.mactable import MAC_BITS, MacTable
from reefervole.model.switch import HEADER_BYTES, MAC_BYTES, MIN_FRAME_BYTES, PORTS

COUNTER_BITS = 32


class PacketBuffer(Module):
    """Store-and-forward frame buffer with a commit that may arrive before end of frame.

    Words are written as they arrive at a tentative write pointer. ``start`` pulses on the
    word that completes the shortest legal frame, at which point ``header`` holds the first
    twelve bytes and the owner may resolve a verdict onto ``verdict``. At end of frame the
    buffer advances the commit pointer if the verdict allows and the frame fit, or rewinds
    the write pointer if it does not. A verdict that has not arrived by end of frame parks
    the buffer until it does; a frame that starts during that window cannot be rewound
    around and is dropped whole, which at line rate cannot happen because the interpacket
    gap alone outlasts a table access.

    The read side spends two cycles per word, one to address the memory and one to present
    it. A 32-bit datapath at 125 MHz carries 1 Gb/s in one word every four cycles, so half
    rate is still twice what the port can offer.
    """

    def __init__(self, dw: int = 32, depth: int = 512):
        self.sink = stream.Endpoint(eth_phy_description(dw))
        self.source = stream.Endpoint(eth_phy_description(dw))
        self.verdict = stream.Endpoint([("commit", 1)])
        self.header = Signal(HEADER_BYTES * 8)
        self.start = Signal()
        self.dropped = Signal(2)

        self.dw = dw
        self.depth = depth
        self.be_bits = log2_int(dw // 8)
        self.adr_bits = log2_int(depth)
        self.mem = Memory(dw + 1 + self.be_bits, depth)
        self.specials += self.mem

        ptr_bits = self.adr_bits + 1
        self.write_ptr = Signal(ptr_bits)
        self.commit_ptr = Signal(ptr_bits)
        self.read_ptr = Signal(ptr_bits)
        self.frame_end = Signal(ptr_bits)
        self.words = Signal(max=HEADER_BYTES * 8 // dw + 2)
        self.bad = Signal()
        self.poison = Signal()
        self.issued = Signal()
        self.frame_bad = Signal()
        self.held = Signal()
        self.held_commit = Signal()
        self.consume = Signal()

        self.sync += [
            If(self.verdict.valid & self.verdict.ready, self.held.eq(1)),
            If(self.verdict.valid & self.verdict.ready, self.held_commit.eq(self.verdict.commit)),
            If(self.consume, self.held.eq(0)),
        ]
        self.comb += self.verdict.ready.eq(~self.held)
        self.submodules.rx = self._write_path()
        self.submodules.tx = self._read_path()

    def _rewind_or_commit(self, end, frame_bad, commit):
        """Publish the frame by advancing the commit pointer, or rewind over it."""
        return [
            self.consume.eq(1),
            If(commit & ~frame_bad, NextValue(self.commit_ptr, end)).Else(
                NextValue(self.write_ptr, self.commit_ptr)
            ),
        ]

    def _write_path(self):
        """Receive side: header capture, request trigger, commit or rewind at end of frame."""
        port = self.mem.get_port(write_capable=True)
        self.specials += port
        nbytes = self.dw // 8
        be_idx = Signal(self.be_bits)
        full = Signal()
        level = Signal(self.adr_bits + 1)
        seen = Signal(8)
        got = Signal()
        commit = Signal()
        eof = Signal()
        resolved = Signal()
        self.comb += [
            self.sink.ready.eq(1),
            level.eq(self.write_ptr - self.read_ptr),
            full.eq(level == self.depth),
            Case(self.sink.last_be, {1 << i: be_idx.eq(i) for i in range(nbytes)}),
            seen.eq(Cat(C(0, self.be_bits), self.words) + Mux(self.sink.last, be_idx + 1, nbytes)),
            got.eq(self.held | (self.verdict.valid & self.verdict.ready)),
            commit.eq(Mux(self.held, self.held_commit, self.verdict.commit)),
            eof.eq(self.sink.valid & self.sink.last),
            resolved.eq(eof & (self.issued | self.start)),
            port.adr.eq(self.write_ptr[: self.adr_bits]),
            port.dat_w.eq(Cat(self.sink.data, self.sink.last, be_idx)),
        ]

        header_words = HEADER_BYTES * 8 // self.dw
        fsm = FSM(reset_state="RX")
        fsm.act(
            "RX",
            self.start.eq(self.sink.valid & ~self.bad & ~self.issued & (seen >= MIN_FRAME_BYTES)),
            port.we.eq(self.sink.valid & ~self.bad & ~full),
            self.dropped.eq((eof & ~resolved) | (resolved & got & (self.bad | full))),
            If(
                self.sink.valid,
                Case(
                    self.words,
                    {
                        i: NextValue(self.header[i * self.dw : (i + 1) * self.dw], self.sink.data)
                        for i in range(header_words)
                    },
                ),
                If(self.words != header_words + 1, NextValue(self.words, self.words + 1)),
                If(self.start, NextValue(self.issued, 1)),
                If(full, NextValue(self.bad, 1)).Elif(
                    ~self.bad, NextValue(self.write_ptr, self.write_ptr + 1)
                ),
                If(
                    self.sink.last,
                    NextValue(self.words, 0),
                    NextValue(self.issued, 0),
                    NextValue(self.bad, 0),
                    NextValue(self.frame_end, self.write_ptr + 1),
                    NextValue(self.frame_bad, self.bad | full),
                    If(~resolved, NextValue(self.write_ptr, self.commit_ptr))
                    .Elif(got, *self._rewind_or_commit(self.write_ptr + 1, self.bad | full, commit))
                    .Else(NextState("WAIT")),
                ),
            ),
        )
        fsm.act(
            "WAIT",
            self.dropped.eq((self.sink.valid & self.sink.last) + (got & self.frame_bad)),
            If(self.sink.valid, NextValue(self.poison, ~self.sink.last)),
            If(
                got,
                *self._rewind_or_commit(self.frame_end, self.frame_bad, commit),
                NextValue(self.bad, self.poison | (self.sink.valid & ~self.sink.last)),
                NextValue(self.poison, 0),
                NextState("RX"),
            ),
        )
        return fsm

    def _read_path(self):
        """Transmit side: drain committed words, one memory address per two cycles."""
        port = self.mem.get_port()
        self.specials += port
        nbytes = self.dw // 8
        be_idx = Signal(self.be_bits)
        last = Signal()
        first = Signal(reset=1)
        self.comb += Cat(self.source.data, last, be_idx).eq(port.dat_r)

        fsm = FSM(reset_state="FETCH")
        fsm.act(
            "FETCH",
            port.adr.eq(self.read_ptr[: self.adr_bits]),
            If(self.read_ptr != self.commit_ptr, NextState("SEND")),
        )
        fsm.act(
            "SEND",
            self.source.valid.eq(1),
            self.source.first.eq(first),
            self.source.last.eq(last),
            Case(be_idx, {i: self.source.last_be.eq(1 << i) for i in range(nbytes)}),
            If(
                self.source.ready,
                NextValue(self.read_ptr, self.read_ptr + 1),
                NextValue(first, last),
                NextState("FETCH"),
            ),
        )
        return fsm


def mac_at(header, first_byte: int):
    """The MAC at ``first_byte`` of the header, first octet most significant."""
    return Cat(
        *[header[8 * k : 8 * k + 8] for k in reversed(range(first_byte, first_byte + MAC_BYTES))]
    )


class _Path(Module):
    """One ingress direction: extract, learn, look up, then commit or drop the frame.

    Learn is presented before lookup and waits for its ready, so the table applies the two
    in the reference model's order: a frame whose source and destination collide in the
    same bucket sees its own learn. Neither access is made for a group address, which is
    never learned as a source and never looked up as a destination.
    """

    def __init__(self, ingress: int, dw: int = 32, depth: int = 512):
        self.submodules.buffer = buffer = PacketBuffer(dw, depth)
        self.sink = buffer.sink
        self.source = buffer.source
        self.dropped = buffer.dropped
        self.learn = stream.Endpoint([("mac", MAC_BITS), ("port", 1)])
        self.lookup = stream.Endpoint([("mac", MAC_BITS)])
        self.result = stream.Endpoint([("hit", 1), ("port", 1)])
        self.forwarded = Signal()
        self.flooded = Signal()
        self.filtered = Signal()

        dst = Signal(MAC_BITS)
        src = Signal(MAC_BITS)
        commit = Signal()
        self.sync += If(
            buffer.start,
            dst.eq(mac_at(buffer.header, 0)),
            src.eq(mac_at(buffer.header, MAC_BYTES)),
        )

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE", If(buffer.start, NextState("LEARN")))
        fsm.act(
            "LEARN",
            If(src[MAC_BITS - 8], NextState("LOOKUP")).Else(
                self.learn.valid.eq(1),
                self.learn.mac.eq(src),
                self.learn.port.eq(ingress),
                If(self.learn.ready, NextState("LOOKUP")),
            ),
        )
        fsm.act(
            "LOOKUP",
            If(dst[MAC_BITS - 8], self.flooded.eq(1), NextValue(commit, 1), NextState("DONE")).Else(
                self.lookup.valid.eq(1),
                self.lookup.mac.eq(dst),
                If(self.lookup.ready, NextState("RESULT")),
            ),
        )
        fsm.act(
            "RESULT",
            self.result.ready.eq(1),
            If(
                self.result.valid,
                If(~self.result.hit, self.flooded.eq(1), NextValue(commit, 1))
                .Elif(self.result.port == ingress, self.filtered.eq(1), NextValue(commit, 0))
                .Else(self.forwarded.eq(1), NextValue(commit, 1)),
                NextState("DONE"),
            ),
        )
        fsm.act(
            "DONE",
            buffer.verdict.valid.eq(1),
            buffer.verdict.commit.eq(commit),
            If(buffer.verdict.ready, NextState("IDLE")),
        )


class LearningSwitch(Module):
    """Two ports, two ingress paths, one shared MAC table.

    ``sink[p]`` is port ``p``'s receive stream and ``source[p]`` its transmit stream; the
    path that receives on port ``p`` drives ``source[p ^ 1]``. ``tick`` is the aging epoch,
    driven by a prescaled counter in the containing SoC so that the age limit lands where
    the deployment wants it. Table access is granted to path 0 first: each path holds at
    most one outstanding request and issues one per frame, so a port that is congested for
    a whole 84-cycle minimum frame time cannot starve the other.
    """

    def __init__(
        self,
        dw: int = 32,
        depth: int = 512,
        buckets: int = 1024,
        tick_bits: int = 8,
        age_limit: int = 128,
    ):
        self.submodules.table = table = MacTable(buckets, tick_bits, age_limit, COUNTER_BITS)
        self.paths = [_Path(port, dw, depth) for port in range(PORTS)]
        self.submodules += self.paths
        self.sink = [path.sink for path in self.paths]
        self.source = [self.paths[port ^ 1].source for port in range(PORTS)]
        self.tick = table.tick
        self.learned = table.learned
        self.moved = table.moved
        self.aged = table.aged
        self.forwarded = Signal(COUNTER_BITS)
        self.flooded = Signal(COUNTER_BITS)
        self.filtered = Signal(COUNTER_BITS)
        self.dropped = Signal(COUNTER_BITS)
        self.sync += [
            counter.eq(counter + sum(getattr(path, name) for path in self.paths))
            for name, counter in (
                ("forwarded", self.forwarded),
                ("flooded", self.flooded),
                ("filtered", self.filtered),
                ("dropped", self.dropped),
            )
        ]
        self._arbitrate(table)

    def _arbitrate(self, table):
        """Grant the shared table to one path at a time, path 0 first."""
        learn_sel = Signal()
        lookup_sel = Signal()
        owner = Signal()
        learn, lookup = [path.learn for path in self.paths], [path.lookup for path in self.paths]
        self.comb += [
            learn_sel.eq(~learn[0].valid),
            table.learn.valid.eq(learn[0].valid | learn[1].valid),
            table.learn.mac.eq(Mux(learn_sel, learn[1].mac, learn[0].mac)),
            table.learn.port.eq(Mux(learn_sel, learn[1].port, learn[0].port)),
            lookup_sel.eq(~lookup[0].valid),
            table.lookup.valid.eq(lookup[0].valid | lookup[1].valid),
            table.lookup.mac.eq(Mux(lookup_sel, lookup[1].mac, lookup[0].mac)),
            table.result.ready.eq(
                Mux(owner, self.paths[1].result.ready, self.paths[0].result.ready)
            ),
        ]
        self.sync += If(table.lookup.valid & table.lookup.ready, owner.eq(lookup_sel))
        for index, path in enumerate(self.paths):
            self.comb += [
                path.learn.ready.eq(table.learn.ready & (learn_sel == index)),
                path.lookup.ready.eq(table.lookup.ready & (lookup_sel == index)),
                path.result.valid.eq(table.result.valid & (owner == index)),
                path.result.hit.eq(table.result.hit),
                path.result.port.eq(table.result.port),
            ]
