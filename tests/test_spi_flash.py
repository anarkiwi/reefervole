"""Gates the memory-mapped SPI flash capability the image has to supply.

Executing firmware in place from the board's SPI flash is the only way to run an image
larger than the ECP5's block RAM will hold, and it needs litespi: litex ships CSR-driven
flash masters only, and `SoC.add_spi_flash` imports litespi to build the memory-mapped
core. An `import litespi` proving nothing, these build the SoC and read the memory map.
"""

from __future__ import annotations

import pytest

from litex.soc.integration.soc_core import SoCCore
from litex_boards.platforms import colorlight_5a_75b
from litespi.modules import W25Q32JV
from litespi.opcodes import SpiNorFlashOpCodes

# The 5A-75B requests one-lane pads as "spiflash"; there is no "spiflash4x" to request.
FLASH_MODE = "1x"
# W25Q32JV, 32 Mbit.
FLASH_BYTES = 4 * 1024 * 1024


@pytest.fixture(name="soc", scope="module")
def _soc():
    """A minimal 5A-75B SoC with the flash mapped into the bus address space."""
    platform = colorlight_5a_75b.Platform(revision="8.2", toolchain="trellis")
    soc = SoCCore(
        platform,
        clk_freq=int(50e6),
        cpu_type=None,
        integrated_rom_size=0,
        integrated_sram_size=0x800,
        with_uart=False,
        with_timer=False,
        with_ctrl=False,
    )
    soc.add_spi_flash(mode=FLASH_MODE, module=W25Q32JV(SpiNorFlashOpCodes.READ_1_1_1))
    soc.finalize()
    return soc


def test_flash_is_a_memory_mapped_bus_region(soc):
    """A CSR-only master would leave no bus region, and firmware could not run from it."""
    assert hasattr(soc.spiflash, "mmap")
    region = soc.bus.regions["spiflash"]
    assert region.size == FLASH_BYTES
    assert region.origin is not None
    # Readable and executable: execute-in-place is the whole point of the region.
    assert set("rx") <= set(region.mode)


def test_flash_region_does_not_overlap_the_rest_of_the_map(soc):
    """An overlapping region silently aliases whatever the CPU fetches from it."""
    flash = soc.bus.regions["spiflash"]
    for name, region in soc.bus.regions.items():
        if name == "spiflash":
            continue
        assert flash.origin >= region.origin + region.size or region.origin >= (
            flash.origin + flash.size
        )


def test_w25q32jv_supports_the_single_lane_read_the_board_is_wired_for():
    """Only cs_n, mosi and miso are bonded out; a quad opcode has nowhere to go."""
    assert SpiNorFlashOpCodes.READ_1_1_1 in W25Q32JV.supported_opcodes
    assert W25Q32JV.total_size == FLASH_BYTES
