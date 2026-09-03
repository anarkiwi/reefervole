"""Frame construction and stream plumbing shared by the gateware tests."""

from __future__ import annotations

from collections.abc import Iterator

BROADCAST = b"\xff\xff\xff\xff\xff\xff"
MULTICAST = b"\x01\x00\x5e\x00\x00\x01"
ETHERTYPE = b"\x08\x00"
DW = 32


def unicast(index: int) -> bytes:
    """A locally administered unicast MAC, distinct per index."""
    return (0x020000000000 | index).to_bytes(6, "big")


def frame(dst: bytes, src: bytes, payload: bytes = ETHERTYPE) -> bytes:
    """An Ethernet frame with the given addresses."""
    return dst + src + payload


def words(data: bytes, dw: int = DW) -> Iterator[tuple[int, int, int]]:
    """``(data, last, last_be)`` for each word of a frame on a ``dw``-bit stream."""
    nbytes = dw // 8
    for offset in range(0, len(data), nbytes):
        chunk = data[offset : offset + nbytes]
        yield (
            int.from_bytes(chunk, "little"),
            int(offset + nbytes >= len(data)),
            1 << (len(chunk) - 1),
        )


def feed(sink, data: bytes, dw: int = DW):
    """Drive one frame into a sink that is never allowed to back-pressure."""
    for value, last, last_be in words(data, dw):
        yield sink.data.eq(value)
        yield sink.last.eq(last)
        yield sink.last_be.eq(last_be)
        yield sink.valid.eq(1)
        yield
    yield sink.valid.eq(0)
    yield sink.last.eq(0)


def collect(source, out: list[bytes], done: list, drain: int = 64, dw: int = DW):
    """Accept every word a source offers and reassemble whole frames into ``out``."""
    nbytes = dw // 8
    pending = bytearray()
    idle = 0
    yield source.ready.eq(1)
    while not done or idle < drain:
        idle += 1
        if (yield source.valid):
            idle = 0
            chunk = (yield source.data).to_bytes(nbytes, "little")
            if (yield source.last):
                out.append(bytes(pending + chunk[: (yield source.last_be).bit_length()]))
                pending = bytearray()
            else:
                pending += chunk
        yield


def watch_ready(signal, done: list, drain: int = 64):
    """Ethernet receive cannot be back-pressured: fail if the sink ever deasserts ready."""
    count = 0
    while not done or count < drain:
        assert (yield signal), "sink.ready deasserted"
        count += 1
        yield
