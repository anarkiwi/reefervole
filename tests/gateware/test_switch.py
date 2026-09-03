"""Differential test of the LearningSwitch gateware against the reference model.

Every case drives real frames in and compares the frames that come out, byte for byte and
port for port, with what the model decides for the same input at the same epoch.
"""

import random

from migen.sim import run_simulation

from ethframes import BROADCAST, ETHERTYPE, MULTICAST, collect, feed, frame, unicast, watch_ready
from reefervole.sim import patch_write_only_memory_ports
from reefervole.model.switch import LearningSwitch as SwitchModel

patch_write_only_memory_ports()

# pylint: disable=wrong-import-position
from reefervole.gateware.switch import LearningSwitch  # noqa: E402

#: Gateware counter to model counter. A runt is the only frame the buffer itself drops.
COUNTERS = {
    "forwarded": "forwarded",
    "flooded": "flooded",
    "filtered": "filtered",
    "learned": "learned",
    "moved": "moved",
    "aged": "aged",
    "dropped": "invalid",
}
TABLE = {"buckets": 16, "tick_bits": 4, "age_limit": 8}
GAP = 16
#: Cycles after the last frame for verdicts and buffers to settle before reading counters.
SETTLE = 32


def feeder(dut, script, done, counters, gap):
    """Drive the script one frame at a time, leaving a gap for the verdict to resolve."""
    for port, data, tick in script:
        yield dut.tick.eq(tick)
        yield from feed(dut.sink[port], data)
        for _ in range(gap):
            yield
    for _ in range(SETTLE):
        yield
    for name in COUNTERS:
        counters[name] = yield getattr(dut, name)
    done.append(True)


def simulate(script, gap=GAP, **table):
    """Run the gateware over a script of ``(port, frame, tick)``."""
    dut = LearningSwitch(**table)
    out, done, counters = ([], []), [], {}
    run_simulation(
        dut,
        [
            feeder(dut, script, done, counters, gap),
            collect(dut.source[0], out[0], done),
            collect(dut.source[1], out[1], done),
            watch_ready(dut.sink[0].ready, done),
            watch_ready(dut.sink[1].ready, done),
        ],
    )
    return out, counters


def reference(script, **table):
    """What the model emits on each port, and the counters it reaches."""
    model = SwitchModel(**table)
    out = ([], [])
    for port, data, tick in script:
        decision = model.feed(port, data, tick)
        if decision.egress is not None:
            out[decision.egress].append(data)
    return out, {name: getattr(model.counters, field) for name, field in COUNTERS.items()}


def check(script, gap=GAP, **table):
    """Frames out, ports out and counters must all match the model."""
    table = table or TABLE
    assert simulate(script, gap, **table) == reference(script, **table)


def body(length: int) -> bytes:
    """An EtherType and enough payload to reach ``length`` bytes past the header."""
    return (ETHERTYPE + bytes(index % 251 for index in range(length)))[:length]


def test_unknown_destination_floods_and_source_is_learned():
    """The first frame floods; the reply to its source is then forwarded."""
    check(
        [
            (0, frame(BROADCAST, unicast(1)), 0),
            (1, frame(unicast(1), unicast(2)), 0),
        ]
    )


def test_known_destination_on_ingress_is_filtered():
    """A destination already behind the ingress port is dropped, not looped back."""
    check(
        [
            (0, frame(BROADCAST, unicast(1)), 0),
            (0, frame(unicast(1), unicast(2)), 0),
        ]
    )


def test_move_follows_the_station():
    """A MAC that reappears on the other port is relearned there."""
    check(
        [
            (0, frame(BROADCAST, unicast(1)), 0),
            (1, frame(BROADCAST, unicast(1)), 0),
            (0, frame(unicast(1), unicast(2)), 0),
        ]
    )


def test_group_destinations_flood():
    """Broadcast and multicast flood even when every station is known."""
    check(
        [
            (0, frame(BROADCAST, unicast(1)), 0),
            (1, frame(BROADCAST, unicast(2)), 0),
            (0, frame(BROADCAST, unicast(1)), 0),
            (1, frame(MULTICAST, unicast(2)), 0),
        ]
    )


def test_group_source_is_never_learned():
    """A frame sourced from a group address updates nothing; the learned count proves it."""
    check(
        [
            (0, frame(unicast(1), BROADCAST), 0),
            (0, frame(unicast(2), MULTICAST), 0),
        ]
    )


def test_entry_ages_out():
    """Past the age limit the entry is gone and traffic to it floods again."""
    check(
        [
            (0, frame(BROADCAST, unicast(1)), 0),
            (1, frame(unicast(1), unicast(2)), TABLE["age_limit"] - 1),
            (1, frame(unicast(1), unicast(3)), TABLE["age_limit"]),
        ]
    )


def test_hash_collision_floods_the_evicted_mac():
    """The evicted occupant of a bucket is flooded, never sent to the wrong port."""
    other = unicast(1 ^ (1 << 4) ^ 1)
    check(
        [
            (0, frame(BROADCAST, unicast(1)), 0),
            (0, frame(BROADCAST, other), 0),
            (1, frame(unicast(1), unicast(9)), 0),
            (1, frame(other, unicast(9)), 0),
        ]
    )


def test_runt_is_dropped():
    """A frame without a full header is dropped whole and teaches the table nothing."""
    check(
        [
            (0, frame(BROADCAST, unicast(1))[:13], 0),
            (1, frame(unicast(1), unicast(2)), 0),
        ]
    )


def test_frame_lengths_are_preserved():
    """Frames of every length modulo the datapath width come out byte identical."""
    check([(0, frame(BROADCAST, unicast(index), body(index)), 0) for index in range(2, 12)])


def test_oversized_frame_is_dropped_whole():
    """A frame that does not fit rewinds to its start; the next frame is unharmed."""
    frames = [frame(BROADCAST, unicast(1), body(80)), frame(BROADCAST, unicast(2))]
    out, counters = simulate([(0, data, 0) for data in frames], depth=16, **TABLE)
    assert out[1] == [frames[1]]
    assert (counters["dropped"], counters["flooded"]) == (1, 2)


def test_back_to_back_frames_are_never_truncated():
    """With no interpacket gap at all, frames are lost whole or not at all."""
    frames = [frame(BROADCAST, unicast(index), body(2)) for index in range(20)]
    out, counters = simulate([(0, data, 0) for data in frames], gap=0, **TABLE)
    assert not out[0]
    assert all(data in frames for data in out[1])
    assert len(out[1]) + counters["dropped"] == len(frames)


def test_random_soak():
    """A seeded mix of ports, lengths, group addresses and epochs."""
    rng = random.Random(20260903)
    macs = [unicast(index) for index in range(6)]
    script = [
        (
            rng.randrange(2),
            frame(
                rng.choice(macs + [BROADCAST, MULTICAST]),
                rng.choice(macs + [MULTICAST]),
                body(rng.randrange(2, 40)),
            ),
            index // 5 % (1 << TABLE["tick_bits"]),
        )
        for index in range(80)
    ]
    check(script)
