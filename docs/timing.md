# Timing closure on the ECP5

What closing a two-port gigabit datapath on the `-7` `LFE5U-25F` taught, stated as rules
with the measurement behind each. Tools: `tools/seed_sweep.py`, `tools/sweep_compare.py`,
`tools/fmax_probe.py`.

## 1. A single placer seed is not a measurement

nextpnr's Fmax moves several MHz with the seed — across one design's sweeps, 3.5 to 9.9 MHz
on a 58 MHz domain and up to 22 MHz on the CPU's. A change smaller than that spread is
noise until it is shown as a shift in the distribution.

* `tools/seed_sweep.py --dir build/<top>/gateware --seeds 16` runs the generated build
  script's yosys line once — the expensive, single-threaded half — and places every seed in
  parallel with its own `--textcfg`, so sixteen seeds cost about the wall time of one. It
  prints min/median/max per clock and exits non-zero if any seed missed a constraint,
  which the LiteX build itself does not (`--timing-allow-fail`). Pack a chosen seed with
  `ecppack seedN.config --bit seedN.bit`.
* nextpnr is seed-deterministic, so the same seed on two netlists is a matched pair.
  `tools/sweep_compare.py` pairs by seed and runs an exact sign-flip permutation test over
  the differences; report a change as "+4.9 MHz, p = 1.5e-4", not as a better maximum.
* **A fail count is not a measurement when the distribution sits on the constraint.** A
  domain with a median of 125.03 against 125.00 relocates half its seeds for a −0.7 MHz
  paired mean at p = 0.8. Read the paired statistic; count fails only for a domain that is
  well clear of its line.
* **An unmeetable constraint in one domain is paid for by the others.** The placer spends
  effort on the domain it cannot close; a second domain that was passing loses 7 MHz and
  starts failing, and recovers on its own once the first closes. Constrain what you can
  reach.
* A regression is only ever a control run in the same session with the same toolchain,
  never a number read back from a document.

## 2. Where the fabric is slow

`tools/fmax_probe.py module.path:Class` places one block alone on an empty device with
every port registered by a shift-register harness, so what it reports is the block's
register-to-register bound. It bounds a full build's figure; it never predicts it.

**Block RAM.** A `DP16KD` costs **5.05 ns clock-to-out** in `REGMODE=NOREG`, 29 % of a
57.7 MHz period before any logic runs, and 4.85 ns in `OUTREG` — the output register
barely helps. Yosys absorbs *one* flip-flop after a memory read into `OUTREG`, so adding a
pipeline register to a memory output gains ~3 MHz; a fabric flop after the block needs the
read to be **two** registers deep. Put the stage boundary near the memory: a cone of seven
LUT4 levels straight off the block output closes at ~80 MHz, the same cone behind a fabric
flop at 140.

A wide address fanning out to many blocks is a routing cost, not a logic one: 25 blocks'
address inputs driven from a mux through a transparency compare cost 7 ns of routing on a
9 ns path. Drive block addresses from a register.

Port modes are not free. migen's default `WRITE_FIRST` read port has its transparency
emulated in ~1 LUT + 1 FF per bit; `READ_FIRST` on either port of a true dual-port array
costs an extra block and roughly doubles the fabric around it; `NO_CHANGE` is the cheap
write port. Measure the pairing with `-m reefervole.synth` before choosing. Width is what
costs blocks ([`toolchain.md`](toolchain.md) §6): a 450-bit word is 25 `DP16KD` at any
depth to 1024.

**Late signals.** Do not let a late signal be a carry input, or the clock enable of a wide
register bank. A CRC verdict that was the carry-in of a pointer adder pinned the MAC's cone
to the bottom of a fixed `CCU2C` chain; precomputing `x + 1` from the register and
selecting with `Mux(late, x + 1, x)` moved the whole design's mean from 58.2 to 65.8 MHz
(16 seeds, p < 1e-5). The same fault as a 32-flop counter's enable on a 16-slice chain, or
a host request reaching 25 block write-enables, or a capture strobe gating a wide hold
register: in each case set one flip-flop from the late signal and let the wide thing act
the cycle after.

Register the late *cone*, not the data path behind it. Latching several drop conditions on
a frame's last beat and running the priority mux the cycle after cost one cycle of latency
and nothing at the commit point; registering a path the placer was free to rebalance
bought nothing, because it spent the slack elsewhere.

## 3. What LiteEth costs, and what it does not

A 32-bit `LiteEthMACCore` on the `-7` part binds its clock domain at **58–66 MHz**: every
seed's critical path is the CRC32 checker or its `last_be` pipeline into whatever follows,
3–6 ns of logic under 10–13 ns of routing. A 32-bit datapath at 57.7 MHz still carries
1.85 Gb/s per port, so the port is served; the cost is to anything else that shares the
domain. `board.md` §6's 125 MHz for `net` is a datapath figure, not a closable one.

Logic that needs a faster clock than the MAC allows — a rule walk, a hash — gets its own
domain from a spare PLL output and a seam (§5), not a faster `net`. The RGMII RX-derived
domains sit exactly on 125 MHz and are judged by §1's paired statistic, not by fail
counts.

## 4. Clocks

One `EHXPLLL` has four outputs and one VCO, so every domain is `VCO / n` for whole `n`. With
`sys` at 62.5 MHz (750/12) the rungs near it are 57.69 (750/13), 68.18 (750/11), 107.14
(750/7) and 125 (750/6); nothing in between exists. Asking LiteX for a value off the ladder
gets the nearest rung anyway, **but LiteX writes the *requested* frequency into the `.lpf`**,
so the design is then constrained against a clock the hardware does not run. Name the
achievable value.

Only `sys` is an exact timebase: 62.5 MHz makes a millisecond 62 500 cycles; 750/13 divides
no round unit. Put wall-clock counters in `sys`.

The 25 MHz reference is a PHY output measured at 24.94 MHz ([`board.md`](board.md) §3.1), so
the wire runs 0.2 % fast relative to every derived clock. A budget with no margin — a
walk that needs exactly one wire period — loses a few parts in 10⁵ to that alone.

## 5. Clock-domain seams

Between a slow datapath and a fast engine, the pattern that closed and stayed
work-conserving up to its budget:

* the datapath latches the request (a 362-bit key here) in its own domain and sends one
  `PulseSynchronizer` offer; the engine latches the held data on arrival, so nothing wide
  crosses as a synchronised vector — a vector synchronised bit by bit can be read in a
  state neither side ever held, and a configuration read that way fails open;
* results return through a `stream.AsyncFIFO`, depth 4, so the engine can start the next
  request while the last verdict is still crossing — the round trip is then latency, not
  throughput;
* one offer may be held behind the engine, which is what absorbs the round trip.

What did *not* work: blocking new offers on a count of refused ones until every earlier
verdict had been emitted. It kept frame order, and past the budget it also idled the engine
for a whole walk per refusal, so the device dropped twice what a busy engine would have.
Ordering belongs in a small FIFO of "walked or refused" per frame, so a refused frame's
verdict is emitted in its slot without stalling admission.

## 6. LiteX and migen pitfalls with a timing cost

* **The CSR bridge is unregistered unless the SoC has SDRAM**
  (`add_csr_bridge(with_register=hasattr(self, "sdram"))`). Without it one `sys` cycle
  carries the CPU's address through the slave decode, every bank's select and the read mux
  back. Override `add_csr_bridge` to register it: +16 MHz paired on `sys` here, one more bus
  cycle per CSR access.
* Two `LiteEthPHYRGMII` instances need
  `ClockDomainsRenamer({"eth_rx": "ethN_rx", "eth_tx": "ethN_tx"})` each; upstream only ever
  instantiates one.
* **Yosys deletes an uninitialised memory** (`removing const-x lane`), so a 16 KiB ROM with
  no firmware image costs no block RAM at placement and then costs 8 blocks the moment a
  real image is linked in. Budget the ROM as if it were there, or execute in place from
  flash (`litespi` MMAP) and never let it start.
* migen names a `Signal` after the Python variable only for a single assignment; a tuple
  assignment leaves every one unnamed in the Verilog, which matters to anything that finds
  signals by name ([`formal.md`](formal.md)).

## 7. Firmware on this part

VexRiscv `minimal` is RV32I with no multiplier; do not plan verification or hashing in
firmware. A 39 KB `-Os` image does not fit any ROM the die can spare next to a datapath, so
it executes in place from SPI flash; the part's block-protection bits must be cleared
before the CPU can program the flash it is fetching from. A 115 200-baud console drains
about 11 KB/s — 85 records of 136 bytes a second — which sets the ceiling on anything a
host learns through it.
