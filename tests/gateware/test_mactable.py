"""Differential test of the MacTable gateware against the reference model's table."""

import random

import pytest
from migen.sim import run_simulation

from ethframes import unicast
from simshim import patch_write_only_memory_ports
from reefervole.model.switch import Counters, MacTable as ModelTable, mac_int

patch_write_only_memory_ports()

# pylint: disable=wrong-import-position
from reefervole.gateware.mactable import MacTable  # noqa: E402

BUCKETS = 16
TICK_BITS = 4
AGE_LIMIT = 8
COUNTERS = ("learned", "moved", "aged")


def learn(mac, port, tick=0):
    """A learn operation."""
    return ("learn", mac, port, tick)


def lookup(mac, tick=0):
    """A lookup operation."""
    return ("lookup", mac, 0, tick)


def drive(dut, ops, results, counters):
    """Run the operations against the gateware, recording answers and final counters."""
    yield dut.result.ready.eq(1)
    for kind, mac, port, tick in ops:
        yield dut.tick.eq(tick)
        endpoint = dut.learn if kind == "learn" else dut.lookup
        yield endpoint.mac.eq(mac_int(mac))
        if kind == "learn":
            yield endpoint.port.eq(port)
        yield endpoint.valid.eq(1)
        yield
        while not (yield endpoint.ready):
            yield
        yield endpoint.valid.eq(0)
        if kind == "lookup":
            yield
            while not (yield dut.result.valid):
                yield
            results.append((yield dut.result.port) if (yield dut.result.hit) else None)
        yield
    for name in COUNTERS:
        counters[name] = yield getattr(dut, name)


def reference(ops):
    """The model's answers and counters for the same operations."""
    model = ModelTable(Counters(), BUCKETS, TICK_BITS, AGE_LIMIT)
    results = []
    for kind, mac, port, tick in ops:
        if kind == "learn":
            model.learn(mac_int(mac), port, tick)
        else:
            results.append(model.lookup(mac_int(mac), tick))
    return results, {name: getattr(model.counters, name) for name in COUNTERS}


def check(ops):
    """Every lookup answer and every counter must match the model."""
    dut = MacTable(buckets=BUCKETS, tick_bits=TICK_BITS, age_limit=AGE_LIMIT)
    results, counters = [], {}
    run_simulation(dut, drive(dut, ops, results, counters))
    assert (results, counters) == reference(ops)


def test_learn_then_hit():
    """A learned MAC is found on the port it was learned on."""
    check([learn(unicast(1), 0), learn(unicast(2), 1), lookup(unicast(1)), lookup(unicast(2))])


def test_miss_on_unknown():
    """An unlearned MAC misses."""
    check([learn(unicast(1), 0), lookup(unicast(2))])


def test_move():
    """Relearning on the other port moves the entry and counts the move."""
    check([learn(unicast(1), 0), learn(unicast(1), 1), lookup(unicast(1))])


def test_aging():
    """An entry past the age limit misses, counts as aged, and is invalidated."""
    check([learn(unicast(1), 1, 0), lookup(unicast(1), AGE_LIMIT - 1), lookup(unicast(1), 8)])


def test_collision_evicts_the_previous_tag():
    """Two MACs in one bucket: the evicted one misses rather than answering wrongly."""
    other = unicast(1 ^ (1 << 4) ^ 1)
    check([learn(unicast(1), 0), learn(other, 1), lookup(unicast(1)), lookup(other)])


def test_random_soak():
    """A seeded mix of learns and lookups over a table small enough to collide."""
    rng = random.Random(20260902)
    macs = [unicast(index) for index in range(24)]
    ops = []
    for _ in range(200):
        mac, tick = rng.choice(macs), rng.randrange(1 << TICK_BITS)
        ops.append(learn(mac, rng.randrange(2), tick) if rng.random() < 0.5 else lookup(mac, tick))
    check(ops)


@pytest.mark.parametrize("kwargs", [{"tick_bits": 4, "age_limit": 16}, {"age_limit": 200}])
def test_rejects_an_age_limit_the_epoch_cannot_express(kwargs):
    """Half the epoch space is the most an unambiguous wrapping comparison can cover."""
    with pytest.raises(ValueError):
        MacTable(**kwargs)
