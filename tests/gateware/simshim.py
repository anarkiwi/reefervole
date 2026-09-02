"""Simulation-only workaround for a migen defect.

Not a conftest: pytest's default import mode registers every ``conftest.py`` under the
bare name ``conftest``, so a second one here would shadow ``tests/conftest.py`` and make
the model suite uncollectable. Gateware tests call :func:`patch_write_only_memory_ports`
explicitly instead, which also keeps migen out of the model tests' import path.
"""

from __future__ import annotations

from migen import Memory


def patch_write_only_memory_ports() -> None:
    """Give every simulated memory port a read side.

    ``migen/fhdl/simplify.py`` emits the read statements for a memory port without
    guarding ``port.dat_r is None``, while ``genlib.fifo.SyncFIFO`` creates its write port
    with ``read_capable=False``. Any design holding a ``SyncFIFO`` therefore raises
    ``AttributeError: 'NoneType' object has no attribute 'eq'`` under ``migen.sim`` —
    migen's own ``test_fifo`` fails the same way, and upstream master still does. The
    extra read side costs one unused signal and never reaches synthesis, which converts
    the module normally.
    """
    original = Memory.get_port

    def get_port(self, *args, **kwargs):
        args = (args[0], True, *args[2:]) if len(args) >= 2 else args
        kwargs["read_capable"] = True
        return original(self, *args, **kwargs)

    Memory.get_port = get_port
