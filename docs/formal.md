# Formal proofs over migen gateware

SymbiYosys runs against the Verilog a block is built from — elaborated through the same
`migen` → `reefervole.synth` path the synthesis and Fmax tools use — so a proof is about the
netlist, not a hand-written model of it. The pattern here is the one a downstream project
uses to gate CI with two proofs; the framework carries the tooling and the traps.

## Running it

`sby`, `yosys`, `bitwuzla` and `yices` exist only in the image (`/opt/oss-cad-suite/bin`),
never on a host install, and the image does not carry a project's Python dependencies. A
read-only mount defeats `pip install -e`, so give pip a writable target:

```sh
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/w" -w /w \
  -e PYTHONPATH=/w:/tmp/pylibs -e HOME=/tmp -e PIP_TARGET=/tmp/pylibs \
  reefervole bash -c "pip install -q <deps>; python3 formal/run.py --require-sby"
```

Pass `-u`, or the build directory ends up root-owned and the next host-side run fails with
`PermissionError`; repair one with `docker run --rm -v "$PWD/formal:/f" alpine:3 chown -R
"$(id -u):$(id -g)" /f/build`. CI downloads the same oss-cad-suite tarball the image is built
from, so a proof that passes in the image gates there too. Make a missing `sby` a silent
no-op for developers and an error (`--require-sby`) for CI.

## Writing the harness

* **Keep the DUT scaled.** Prove a 16-entry buffer with 4 descriptors, not the board's; the
  properties are structural and the solver time is not. Expose depths as constructor
  parameters so the same class builds both.
* **Scope the solver per task.** `bitwuzla` finished an inductive `prove` step in fifty
  seconds that `yices` had not finished in half an hour; `yices` is the faster on `bmc`.
  Set `engines` per task in the `.sby`.
* **Count frames past every stage.** A pairing proof — nothing creates or destroys a frame
  between the parser and the buffer — is stated as "each queue's level equals the difference
  of the counts either side of it", with a shadow array carrying serials so the *order* is
  asserted and not only the counts. State every recovery path as an equality too, and
  assert that nothing is forwarded while it runs.
* **`cover` carries the non-vacuity checks.** A property that holds because a state is
  unreachable is worth nothing; make the solver reach it.

## The naming trap

A harness that finds a DUT signal by name (`signal.backtrace[-1][0]`) relies on migen
recording the Python variable name, and migen does that only for a plain single
assignment. A tuple assignment — `a, b = Signal(), Signal()` — leaves every signal unnamed
in the Verilog, and the probe reports "0 signals match".

Do not rename the RTL to suit the harness. Recompute the value in the SystemVerilog from
ports that do exist; that turns the DUT's internal signal into a *check* rather than an
input, which is strictly stronger.
