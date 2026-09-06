# reefervole

A reusable development framework for the Colorlight 5A-75B, a ~US$15 LED receiving card
carrying a Lattice ECP5 `LFE5U-25F`, two Gigabit Ethernet PHYs, SDRAM and SPI flash. It
provides the board knowledge, the container toolchain, the bench tooling and a bring-up
reference design needed to treat that board as a serious FPGA target.

Target: Colorlight 5A-75B **rev 8.2** (rev 8.0 and 7.0 also supported; see
[`docs/board.md`](docs/board.md) for what changes per revision).

## Quickstart

```sh
docker build -t reefervole .                                  # ~1 GB toolchain, cached
docker run --rm -v "$PWD:/work" reefervole make check         # docs, lint, tests

# Bring-up bitstream: PLL, jtagbone, MDIO, a clk25 counter and a UART echo.
# Requests no Ethernet pads, so it cannot disturb the PHYs.
docker run --rm -v "$PWD:/work" reefervole \
  python3 -m reefervole.gateware.diagnostics --build

# On the machine the board is wired to, not in the container:
python3 tools/bench_check.py --cable ft232                    # probe the whole rig
openFPGALoader -b colorlight -c ft232 -m build/diagnostics/gateware/diagnostics.bit
python3 tools/phy_probe.py                                    # read the board's straps
```

**Before applying power, read [`docs/bench.md`](docs/bench.md) §3.** The board's input
limit is 5.5 V and a 12 V LED-panel supply destroys it immediately.

## Make targets

| Target | Does |
| --- | --- |
| `check` | `docs`, `lint`, `test` — what CI runs |
| `docs` | Validate relative links in git-tracked Markdown |
| `lint` | `black --check`, `pylint --recursive=y` |
| `test` | `pytest -n auto`, coverage floor 85 % |
| `update-litex` | Repin the LiteX-family git SHAs in `requirements-gateware.txt` to upstream `HEAD` |

## Layout

| Path | Contents |
| --- | --- |
| `reefervole/gateware/` | Migen gateware: reusable blocks and the reference application |
| `reefervole/model/` | Python reference models, authoritative for gateware behaviour |
| `reefervole.checkdocs` | Markdown link checker |
| `-m reefervole.synth` | Real ECP5 cell cost of one module, via yosys `synth_ecp5` |
| `tools/bench_check.py` | One-shot probe of a bench rig; standard library only |
| `tools/phy_probe.py` | Read-only MDIO scan, RGMII delay straps and `clk25`, over jtagbone |
| `tools/bench_netns.sh` | Two-namespace NIC test rig, setup and complete undo |
| `tools/seed_sweep.py` | One synthesis, N placer seeds in parallel; Fmax distribution per clock, exit 1 on a miss |
| `tools/sweep_compare.py` | Paired sign-flip permutation test between two sweeps |
| `tools/fmax_probe.py` | One module's register-to-register Fmax, alone on the device |
| `Dockerfile` | Multistage image: oss-cad-suite, RISC-V GCC, pinned Python deps |

## Docs

| Document | Covers |
| --- | --- |
| [`docs/board.md`](docs/board.md) | The board as a development target: revisions, pinouts, the rev 8.x clock and reset hazards, `LiteEthPHYRGMII` parameters, clocking |
| [`docs/bench.md`](docs/bench.md) | Power limits, FT232H JTAG wiring, the J19 console, the two-NIC test rig, loading bitstreams |
| [`docs/toolchain.md`](docs/toolchain.md) | yosys / nextpnr / prjtrellis / LiteX flow, container layout, measuring fabric cost, the `DP16KD` width trap |
| [`docs/timing.md`](docs/timing.md) | Closing timing on the ECP5: seed sweeps as measurement, block RAM and late-signal rules, what LiteEth costs, the PLL ladder, clock-domain seams, LiteX pitfalls |
| [`docs/formal.md`](docs/formal.md) | SymbiYosys over migen gateware: running it in the image, harness patterns, the signal-naming trap |

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
