"""Two-port L2 learning switch: the reference model the gateware is tested against.

The model is bucket-accurate, not idealised: it holds the same direct-mapped hash table
the gateware holds, so a hash collision produces the same decision in both. A collision
evicts the previous occupant of the bucket, and because the full 48-bit MAC is stored as
a tag and compared on lookup, the evicted MAC then misses and is flooded. Collisions
therefore cost bandwidth, never correctness: the table can fail to forward, but it cannot
forward to the wrong port.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

PORTS = 2
MAC_BYTES = 6
#: Destination MAC, source MAC and EtherType: the shortest frame with a complete header.
MIN_FRAME_BYTES = 14
HEADER_BYTES = 2 * MAC_BYTES


class Reason(Enum):
    """Why a frame left on the port it did, mirroring the datapath's own report."""

    INVALID = "invalid"
    FORWARDED = "forwarded"
    FLOODED = "flooded"
    FILTERED = "filtered"


@dataclass(frozen=True)
class Decision:
    """Egress port for a frame, or ``None`` if it was dropped, and the reason."""

    egress: int | None
    reason: Reason


@dataclass
class Counters:
    """Event counts, shared by the table and the switch that owns it."""

    learned: int = 0
    moved: int = 0
    forwarded: int = 0
    flooded: int = 0
    filtered: int = 0
    aged: int = 0
    invalid: int = 0


@dataclass
class Entry:
    """One direct-mapped bucket: the tag, the port it was seen on, and its epoch."""

    mac: int
    port: int
    tick: int


def mac_int(raw: bytes) -> int:
    """The 48 bits of a MAC address, first octet most significant."""
    return int.from_bytes(raw, "big")


def is_group(mac: int) -> bool:
    """True for broadcast and multicast: bit 0 of the first octet, bit 40 of the MAC."""
    return bool(mac >> 40 & 1)


def fold_hash(mac: int, bits: int) -> int:
    """XOR-fold a 48-bit MAC down to ``bits``, least significant chunk first."""
    folded = 0
    mask = (1 << bits) - 1
    while mac:
        folded ^= mac & mask
        mac >>= bits
    return folded


class MacTable:
    """Direct-mapped MAC table with epoch aging.

    Aging compares a stored epoch against the caller's current epoch, so nothing ever
    walks the table. ``tick_bits`` narrows the stored epoch to the width the gateware
    keeps in block RAM; ``age_limit`` must not exceed half that space, or an entry that
    wrapped fully around would read as fresh. A stale entry is discarded the moment any
    access touches its bucket, so the wrap case needs a bucket left untouched for a full
    epoch cycle.
    """

    def __init__(
        self, counters: Counters, buckets: int = 1024, tick_bits: int = 8, age_limit: int = 128
    ) -> None:
        if buckets & (buckets - 1):
            raise ValueError("buckets must be a power of two")
        if age_limit > 1 << (tick_bits - 1):
            raise ValueError("age_limit must not exceed half the epoch space")
        self.counters = counters
        self.index_bits = buckets.bit_length() - 1
        self.tick_mask = (1 << tick_bits) - 1
        self.age_limit = age_limit
        self.entries: list[Entry | None] = [None] * buckets

    def index(self, mac: int) -> int:
        """The bucket a MAC maps to."""
        return fold_hash(mac, self.index_bits)

    def _fresh(self, entry: Entry, now: int) -> bool:
        """Whether an entry is still within the age limit at epoch ``now``."""
        return (now - entry.tick) & self.tick_mask < self.age_limit

    def _touch(self, slot: int, now: int) -> Entry | None:
        """The bucket's entry, discarding and counting it first if it has aged out."""
        entry = self.entries[slot]
        if entry is not None and not self._fresh(entry, now):
            self.entries[slot] = None
            self.counters.aged += 1
            return None
        return entry

    def learn(self, mac: int, port: int, now: int) -> None:
        """Associate ``mac`` with ``port``, replacing whatever the bucket held."""
        slot = self.index(mac)
        entry = self._touch(slot, now)
        if entry is None or entry.mac != mac:
            self.counters.learned += 1
        elif entry.port != port:
            self.counters.moved += 1
        self.entries[slot] = Entry(mac, port, now & self.tick_mask)

    def lookup(self, mac: int, now: int) -> int | None:
        """The port ``mac`` was last seen on, or ``None`` on a miss."""
        entry = self._touch(self.index(mac), now)
        return entry.port if entry is not None and entry.mac == mac else None


class LearningSwitch:
    """Two ports, one shared MAC table, one decision per frame.

    With two ports the egress of a frame is fully determined by whether it is dropped:
    forwarding to the ingress port is what filtering means, so every frame that leaves
    leaves on the other port. Flooding and forwarding differ only in the reason reported.
    """

    def __init__(self, buckets: int = 1024, tick_bits: int = 8, age_limit: int = 128) -> None:
        self.counters = Counters()
        self.table = MacTable(self.counters, buckets, tick_bits, age_limit)

    def feed(self, ingress: int, frame: bytes, now: int) -> Decision:
        """Learn from and forward one frame arriving on ``ingress`` at epoch ``now``."""
        if len(frame) < MIN_FRAME_BYTES:
            self.counters.invalid += 1
            return Decision(None, Reason.INVALID)
        dst = mac_int(frame[:MAC_BYTES])
        src = mac_int(frame[MAC_BYTES:HEADER_BYTES])
        # A group address is never a real station, so it is never a valid source.
        if not is_group(src):
            self.table.learn(src, ingress, now)
        port = None if is_group(dst) else self.table.lookup(dst, now)
        if port is None:
            self.counters.flooded += 1
            return Decision(ingress ^ 1, Reason.FLOODED)
        if port == ingress:
            self.counters.filtered += 1
            return Decision(None, Reason.FILTERED)
        self.counters.forwarded += 1
        return Decision(port, Reason.FORWARDED)
