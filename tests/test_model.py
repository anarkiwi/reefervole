"""Behaviour of the reference switch model, which the gateware is tested against."""

import pytest

from reefervole.model.switch import (
    Decision,
    LearningSwitch,
    MacTable,
    Counters,
    Reason,
    fold_hash,
    is_group,
    mac_int,
)

BROADCAST = b"\xff\xff\xff\xff\xff\xff"
MULTICAST = b"\x01\x00\x5e\x00\x00\x01"
ETHERTYPE = b"\x08\x00"


def unicast(index: int) -> bytes:
    """A locally administered unicast MAC, distinct per index."""
    return (0x020000000000 | index).to_bytes(6, "big")


def frame(dst: bytes, src: bytes, payload: bytes = ETHERTYPE) -> bytes:
    """An Ethernet frame with the given addresses."""
    return dst + src + payload


def test_group_bit():
    """Only the low bit of the first octet marks a group address."""
    assert is_group(mac_int(BROADCAST))
    assert is_group(mac_int(MULTICAST))
    assert not is_group(mac_int(unicast(1)))


def test_short_frame_is_invalid():
    """A frame without a complete header is dropped and nothing is learned."""
    switch = LearningSwitch()
    assert switch.feed(0, frame(unicast(1), unicast(2))[:13], 0) == Decision(None, Reason.INVALID)
    assert switch.counters == Counters(invalid=1)


def test_unknown_destination_floods():
    """A miss floods to the only other port, and the source is learned."""
    switch = LearningSwitch()
    assert switch.feed(0, frame(unicast(1), unicast(2)), 0) == Decision(1, Reason.FLOODED)
    assert switch.counters.learned == 1
    assert switch.table.lookup(mac_int(unicast(2)), 0) == 0


@pytest.mark.parametrize("dst", [BROADCAST, MULTICAST])
def test_group_destination_floods_without_lookup(dst):
    """Broadcast and multicast flood even once every station is known."""
    switch = LearningSwitch()
    switch.feed(1, frame(unicast(1), unicast(2)), 0)
    assert switch.feed(0, frame(dst, unicast(3)), 0) == Decision(1, Reason.FLOODED)


def test_group_source_is_not_learned():
    """A group address is never a station, so it is never learned as a source."""
    switch = LearningSwitch()
    switch.feed(0, frame(unicast(1), BROADCAST), 0)
    assert switch.counters.learned == 0
    assert switch.table.lookup(mac_int(BROADCAST), 0) is None


def test_known_destination_forwards():
    """A hit on the other port forwards there."""
    switch = LearningSwitch()
    switch.feed(0, frame(BROADCAST, unicast(1)), 0)
    assert switch.feed(1, frame(unicast(1), unicast(2)), 0) == Decision(0, Reason.FORWARDED)
    assert switch.counters.forwarded == 1


def test_known_destination_on_ingress_is_filtered():
    """A hit on the ingress port means the destination is already behind it."""
    switch = LearningSwitch()
    switch.feed(0, frame(BROADCAST, unicast(1)), 0)
    assert switch.feed(0, frame(unicast(1), unicast(2)), 0) == Decision(None, Reason.FILTERED)
    assert switch.counters.filtered == 1


def test_move_relearns_on_the_new_port():
    """A MAC seen on another port moves, and traffic to it follows."""
    switch = LearningSwitch()
    switch.feed(0, frame(BROADCAST, unicast(1)), 0)
    switch.feed(1, frame(BROADCAST, unicast(1)), 0)
    assert switch.counters.moved == 1
    assert switch.counters.learned == 1
    assert switch.feed(0, frame(unicast(1), unicast(2)), 0) == Decision(1, Reason.FORWARDED)


def test_repeat_on_the_same_port_neither_learns_nor_moves():
    """Refreshing an entry is not a new station."""
    switch = LearningSwitch()
    switch.feed(0, frame(BROADCAST, unicast(1)), 0)
    switch.feed(0, frame(BROADCAST, unicast(1)), 1)
    assert switch.counters == Counters(learned=1, flooded=2)


def test_entry_ages_out():
    """An entry older than the age limit is discarded on the access that finds it."""
    switch = LearningSwitch(age_limit=8)
    switch.feed(0, frame(BROADCAST, unicast(1)), 0)
    assert switch.feed(1, frame(unicast(1), unicast(2)), 7) == Decision(0, Reason.FORWARDED)
    assert switch.feed(1, frame(unicast(1), unicast(3)), 8) == Decision(0, Reason.FLOODED)
    assert switch.counters.aged == 1
    assert switch.table.lookup(mac_int(unicast(1)), 8) is None


def test_learning_refreshes_the_epoch():
    """Traffic from a station keeps its entry alive."""
    switch = LearningSwitch(age_limit=8)
    for now in range(0, 24, 4):
        switch.feed(0, frame(BROADCAST, unicast(1)), now)
    assert switch.counters.aged == 0
    assert switch.table.lookup(mac_int(unicast(1)), 23) == 0


def test_collision_evicts_and_floods():
    """Two MACs in one bucket cost bandwidth, not correctness: the loser is flooded."""
    switch = LearningSwitch(buckets=1024)
    first, second = unicast(1), unicast(1 ^ (1 << 10) ^ 1)
    assert switch.table.index(mac_int(first)) == switch.table.index(mac_int(second))
    switch.feed(0, frame(BROADCAST, first), 0)
    switch.feed(0, frame(BROADCAST, second), 0)
    assert switch.counters.learned == 2
    assert switch.feed(1, frame(first, unicast(9)), 0) == Decision(0, Reason.FLOODED)
    assert switch.feed(1, frame(second, unicast(9)), 0) == Decision(0, Reason.FORWARDED)


def test_fold_hash_covers_every_chunk():
    """Folding mixes all 48 bits: chunks that cancel land in the same bucket."""
    assert fold_hash(0, 10) == 0
    assert fold_hash(1 << 10, 10) == 1
    assert fold_hash((1 << 10) | 1, 10) == 0
    # Four full chunks cancel in pairs, leaving the eight bits of the fifth.
    assert fold_hash(0xFFFFFFFFFFFF, 10) == 0xFF


@pytest.mark.parametrize(
    "kwargs", [{"buckets": 1000}, {"age_limit": 129}, {"tick_bits": 4, "age_limit": 16}]
)
def test_rejects_unbuildable_tables(kwargs):
    """Bucket counts must be a power of two and the age limit half the epoch space."""
    with pytest.raises(ValueError):
        MacTable(Counters(), **kwargs)
