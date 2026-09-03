"""Structural checks on the bring-up diagnostics SoC.

The safety properties of this design are all absences -- no PHYRstB, no eth pads -- and an
absence cannot be reviewed reliably by eye. Elaborating the SoC and reading the pin
constraints back is the only check that stays true after an edit.
"""

from __future__ import annotations

import pytest

from litex.soc.integration.builder import Builder

from reefervole.gateware.diagnostics import (
    GATE_CYCLES,
    OSC_DIV,
    OSC_NOMINAL_HZ,
    DiagnosticsSoC,
)

#: MDC, MDIO, the measured clock input and the two console pins. Nothing else.
EXPECTED_SITES = {"P6", "R5", "T4", "T6", "R7"}

#: PHYRstB, shared by both PHYs. Requesting it at all would let something drive it.
PHYRSTB_SITE = "R6"


@pytest.fixture(name="built", scope="module")
def built_fixture(tmp_path_factory):
    """Elaborate the SoC once; return its constraint file and its CSR map as text."""
    output_dir = tmp_path_factory.mktemp("diagnostics")
    csr_csv = output_dir / "csr.csv"
    Builder(
        DiagnosticsSoC(), output_dir=str(output_dir), csr_csv=str(csr_csv), compile_software=False
    ).build(build_name="diagnostics", run=False)
    return (
        (output_dir / "gateware" / "diagnostics.lpf").read_text(encoding="utf-8"),
        csr_csv.read_text(encoding="utf-8"),
    )


@pytest.fixture(name="lpf")
def lpf_fixture(built):
    """The generated Lattice constraint file."""
    return built[0]


@pytest.fixture(name="csr")
def csr_fixture(built):
    """The generated CSR map, as phy_probe's RemoteClient reads it."""
    return built[1]


def _sites(lpf):
    return {line.split('SITE "')[1].split('"')[0] for line in lpf.splitlines() if "SITE " in line}


def test_only_the_five_expected_pins_are_constrained(lpf):
    """Every requested pad reaches the constraint file, so this is the whole pin list."""
    assert _sites(lpf) == EXPECTED_SITES


def test_phyrstb_is_never_requested(lpf):
    """Driving the shared reset stops the PHY that clocks the FPGA."""
    assert PHYRSTB_SITE not in _sites(lpf)


def test_both_clock_domains_are_constrained(lpf):
    """clk25 is measured, not used, but an unconstrained input clock skews the CDC paths."""
    assert 'FREQUENCY PORT "clk25" 25.0 MHz;' in lpf
    assert 'FREQUENCY NET "sys_clk"' in lpf


def test_the_bitstream_publishes_what_the_host_scales_the_count_by(csr):
    """phy_probe divides by these rather than by a number copied into two places."""
    assert f"constant,diag_sys_clk_freq,{int(OSC_NOMINAL_HZ / OSC_DIV)}," in csr
    assert f"constant,diag_gate_cycles,{GATE_CYCLES}," in csr


@pytest.mark.parametrize(
    "register", ["clk25_start", "clk25_done", "clk25_count", "clk25_alive", "mdio_w", "mdio_r"]
)
def test_the_csrs_the_probe_drives_exist_under_those_names(csr, register):
    """phy_probe looks these up by name on RemoteClient.regs; a rename breaks it silently."""
    assert f"csr_register,{register}," in csr
