"""litex_server --jtag with Nagle disabled on the OpenOCD jtagstream socket.

LiteX's pty2tcp thread sends one byte per ``send()``, so Nagle and the peer's delayed ACK
cost 40 ms on every CSR read -- 41.9 ms measured against 1.4 ms patched, and phy_probe
samples 17 bits per MDIO read frame, so an unpatched scan of all 32 addresses takes tens
of seconds. --jtag-config defaults to the config beside this file, which is the one that
sets _CHIPNAME; see its header for why the shipped config cannot be used.
"""

from __future__ import annotations

import os
import socket
import sys

from litex.tools import litex_term

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openocd_colorlight_5a_75b.cfg")

_open = litex_term.JTAGUART.open


def _open_nodelay(self):
    _open(self)
    self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


litex_term.JTAGUART.open = _open_nodelay

# Imported after the patch so litex_server binds the wrapped open().
from litex.tools.litex_server import main  # noqa: E402  pylint: disable=wrong-import-position


def argv(args):
    """Default to --jtag against our own config, overriding neither if already given."""
    args = list(args)
    if not any(a.startswith("--jtag") for a in args):
        args.append("--jtag")
    if not any(a.startswith("--jtag-config") for a in args):
        args.append(f"--jtag-config={_CONFIG}")
    return args


if __name__ == "__main__":
    sys.argv[1:] = argv(sys.argv[1:])
    sys.exit(main())
