# Toolchain

Entirely open source, pinned in `Dockerfile` and `requirements*.txt`. Nothing here needs a
vendor licence, an account, or a Windows VM.

## 1. What is in the flow

| Stage | Tool | Source |
| --- | --- | --- |
| Gateware description | Migen + LiteX (Python) | pip, pinned to git SHAs |
| Ethernet MAC and RGMII PHY | LiteEth (`liteeth.phy.ecp5rgmii`) | pip, pinned to git SHAs |
| Board pinout | `litex-boards` | pip, pinned to git SHAs |
| Soft CPU | VexRiscv (`pythondata-cpu-vexriscv`) | pip, pinned to git SHAs |
| SDRAM controller | LiteDRAM | pip, pinned to git SHAs |
| Synthesis | yosys `synth_ecp5` | oss-cad-suite |
| Place and route | nextpnr-ecp5 | oss-cad-suite |
| Bitstream pack | prjtrellis `ecppack` | oss-cad-suite |
| Simulation | Verilator + cocotb; migen's native simulator | oss-cad-suite / pip |
| Formal | SymbiYosys (`sby`) + Yices2 | oss-cad-suite |
| Firmware | `riscv-none-elf-gcc` (xPack), bare metal | xPack release tarball |
| Load / flash | openFPGALoader | host, not in the image ([`bench.md`](bench.md) §7) |

**Why LiteX rather than Amaranth.** LiteEth already ships a silicon-proven ECP5 RGMII PHY
with in-band link status and 10/100/1000 support, plus a MAC core that handles preamble,
FCS, padding, inter-frame gap, the clock-domain crossing and 8↔32-bit width conversion;
`litex-boards` already carries the 5A-75B pinout. Re-deriving any of that is pure re-work.
Everything upstream is used as-is — `LiteEthPHYRGMII`, `LiteEthMACCore`,
`litex.soc.interconnect.stream`, VexRiscv, the LiteX UART/timer/SPI-flash cores and
`litex.build`'s ECP5 DDR primitives — with board-specific parameters from
[`board.md`](board.md) §4.

**Two simulators, on purpose.** Migen's native simulator runs pure-Python generator
testbenches against migen modules with no compile step, which is what makes per-block unit
tests fast enough to run under `pytest -n auto`. Verilator plus cocotb is for whole-design
tests where the Verilog is what you want to exercise. `tests/gateware/simshim.py` carries
the one workaround the native simulator needs: `migen/fhdl/simplify.py` emits read
statements for a memory port without checking `port.dat_r is None`, while
`genlib.fifo.SyncFIFO` creates its write port with `read_capable=False`, so any design
holding a `SyncFIFO` raises `AttributeError` under `migen.sim`. The shim forces every
simulated port read-capable; the extra signal never reaches synthesis.

## 2. The container

```
 stage 1  fpga-tools    debian:bookworm-slim  + oss-cad-suite tarball   (~1 GB)
 stage 2  riscv-tools   debian:bookworm-slim  + xPack riscv-none-elf-gcc
 stage 3  runtime       python:3.12-slim-bookworm
                        COPY --from=fpga-tools  /opt/oss-cad-suite
                        COPY --from=riscv-tools /opt/riscv
                        + venv from requirements*.txt
```

Multistage is what keeps the loop short. The two toolchain downloads are the expensive
layers and they change only when `OSS_CAD_SUITE_DATE` or `RISCV_GCC_VERSION` moves, so
editing a requirement rebuilds only the last stage's pip layer. Both are build args, so a
toolchain bump is a one-line change and CI's registry cache does the rest.

Four things the Dockerfile does deliberately:

| Choice | Reason |
| --- | --- |
| Runtime is `python:3.12-slim-bookworm` | The oss-cad-suite binaries are built against bookworm's glibc, but bookworm ships Python 3.11 and the project needs 3.12 |
| `ENV PATH` **and** `/etc/profile.d/10-reefervole-toolchain.sh` | A login shell re-sources `/etc/profile` and would otherwise discard the `ENV` |
| `PYTHONPATH=/work` | The repo is bind-mounted at `/work`, so an editable install baked at image-build time would point at a path the image cannot know |
| `git config --system --add safe.directory /work` | The bind-mounted tree is owned by the host user, not by root in the container |
| Dateless tag built with `tr -d -` | `RUN` uses `/bin/sh`; a bash parameter substitution would not work |

Use it as a bind mount, never by copying the source in:

```sh
docker build -t reefervole .
docker run --rm -v "$PWD:/work" reefervole make check
```

## 3. Pins and updates

Two requirement files, both watched by Dependabot (`.github/dependabot.yml` covers pip,
docker and github-actions, weekly):

* `requirements.txt` — PyPI releases, exact `==` pins: numpy, cocotb, pytest and its xdist
  and cov plugins, hypothesis, scapy, black, pylint.
* `requirements-gateware.txt` — the LiteX family, pinned to **40-character git SHAs** rather
  than PyPI releases. The PyPI snapshots lag the tree by months and predate LiteEth features
  this board needs, notably RGMII in-band link status and dynamic 10/100/1000 handling in
  `liteeth.phy.ecp5rgmii`. `make update-litex` rewrites each SHA to the upstream `HEAD` and
  prints the diff; run the tests before committing it.

## 4. Checks

`make check` is `docs`, then `lint`, then `test` — the same three things CI runs, plus a
fourth CI job that builds the image.

| Target | Command | Gate |
| --- | --- | --- |
| `docs` | `python3 tools/check_docs.py` | Every relative Markdown link in a git-tracked `.md` resolves. Only tracked files are checked: cached upstream material under gitignored paths is not ours to validate |
| `lint` | `black --check .`; `pylint --recursive=y .` | Line length 100, Python 3.12 target. `--ignore` is set in `pyproject.toml`, not on the command line, where it would replace the list rather than extend it |
| `test` | `pytest -n auto --cov-fail-under=85` | Coverage measured on `reefervole` only. `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` are exported by the Makefile: numpy spawns a thread pool per xdist worker, which exhausts pthread limits on a many-core host |

## 5. Measuring a block's real cost

A fabric budget is an estimate until yosys has seen the block. `tools/synth_probe.py`
elaborates one migen module, converts it to Verilog, runs `synth_ecp5` and reports the
cells:

```sh
docker run --rm -v "$PWD:/work" reefervole \
  python3 tools/synth_probe.py reefervole.gateware.buffer:PacketBuffer depth=2048
```

`module.path:ClassName` plus `name=value` constructor arguments, parsed with
`ast.literal_eval`. It reports `LUT4`, `CCU2C`, `PFUMX`, `L6MUX21`, `TRELLIS_FF`, `DP16KD`
and `TRELLIS_DPR16X4`, then a LUT4-equivalent total that weights `CCU2C` as **2** — a
carry cell occupies both LUT4s of an ECP5 slice.

Two things to know before trusting a number:

* **The I/O set decides what survives.** `collect_ios()` walks the module's public
  attributes and takes every `Record` (a `stream.Endpoint` is one) and every `Signal`,
  including per-direction lists of them. Anything it misses is dangling, yosys prunes it,
  and the block is silently under-reported. Expose the interface as public attributes.
* **Hand estimates run low, consistently.** Pointer arithmetic and byte-offset comparators
  go carry-chain heavy in a way an estimate does not capture; measured blocks have come in
  30–75 % above their budget. Treat every unmeasured row as optimistic.

## 6. The block RAM width trap

The single most common sizing mistake on this part.

An ECP5 `DP16KD` is 18 Kb, which invites the arithmetic `blocks = ceil(depth × width /
18432)`. **That bound is unreachable for a true dual-port memory.** `DP16KD` is **×18 per
port** in true dual-port mode; ×36 exists only as `PDPW16KD`, which has a single read port.
So a memory that needs two independent read ports costs

```
blocks = ceil(width / 18)        regardless of depth, up to 1024 entries deep
```

A 512 × 504-bit table looks like 14 blocks by the capacity formula and is really 28. A
counter memory budgeted at 2 comes out at 4 the same way. On a 56-EBR part that error is
the difference between fitting and not.

Three ways out, in order of preference:

| Fix | Effect |
| --- | --- |
| Trim the word to a whole multiple of 18 | A 504-bit word chosen as a multiple of 36 wastes a block; 450 bits is 25 whole ×18 blocks, and widths this primitive cannot give you buy nothing |
| Drop a second read port you do not need | `PDPW16KD` at ×36 halves the block count |
| Time-multiplex one table between two readers | Halves the blocks again, at the cost of throughput |

Depth is nearly free until it crosses 1024; width is what costs. Check the real number with
`synth_probe.py` (§5) — the `DP16KD` row is exactly this count.

## 7. CI

`.github/workflows/ci.yml`, on push to `main` and on every pull request:

| Job | Runs |
| --- | --- |
| `docs` | `tools/check_docs.py`, no dependencies installed |
| `lint` | `black --check`, `pylint --recursive=y --ignore=build` |
| `test` | `pytest -n auto --cov-fail-under=85` with the thread-pool limits set |
| `toolchain` | `docker build` of the image, GitHub Actions layer cache in and out, no push |

The `toolchain` job is what keeps the multistage cache warm and catches an upstream
tarball that has moved or disappeared before it costs anyone a bench session.
