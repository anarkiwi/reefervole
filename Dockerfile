# syntax=docker/dockerfile:1

ARG OSS_CAD_SUITE_DATE=2026-09-01
ARG RISCV_GCC_VERSION=14.2.0-3

# --- Stage 1: FPGA toolchain (yosys, nextpnr-ecp5, prjtrellis, verilator, sby) ---------
FROM debian:bookworm-slim AS fpga-tools
ARG OSS_CAD_SUITE_DATE
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# RUN uses /bin/sh, so the dateless tag is built with tr rather than a bash substitution.
RUN set -eux; stamp="$(echo "${OSS_CAD_SUITE_DATE}" | tr -d -)"; \
    curl -fsSL -o /tmp/oss.tgz \
      "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${OSS_CAD_SUITE_DATE}/oss-cad-suite-linux-x64-${stamp}.tgz"; \
    mkdir -p /opt; tar -xzf /tmp/oss.tgz -C /opt; rm /tmp/oss.tgz

# --- Stage 2: RISC-V bare-metal toolchain ---------------------------------------------
FROM debian:bookworm-slim AS riscv-tools
ARG RISCV_GCC_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/rv.tar.gz \
      "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v${RISCV_GCC_VERSION}/xpack-riscv-none-elf-gcc-${RISCV_GCC_VERSION}-linux-x64.tar.gz" \
    && mkdir -p /opt/riscv && tar -xzf /tmp/rv.tar.gz -C /opt/riscv --strip-components=1 && rm /tmp/rv.tar.gz

# --- Stage 3: runtime ------------------------------------------------------------------
# Bookworm-based so the oss-cad-suite binaries find the glibc they were built against,
# but pinned to the Python the project requires rather than the 3.11 bookworm ships.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      git make libtinfo6 libffi8 libreadline8 libgomp1 zlib1g ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=fpga-tools  /opt/oss-cad-suite /opt/oss-cad-suite
COPY --from=riscv-tools /opt/riscv         /opt/riscv
ENV PATH=/opt/oss-cad-suite/bin:/opt/riscv/bin:/opt/venv/bin:$PATH
# Login shells re-source /etc/profile and would otherwise discard the ENV above.
RUN printf 'PATH=/opt/oss-cad-suite/bin:/opt/riscv/bin:/opt/venv/bin:$PATH\n' \
      > /etc/profile.d/10-reefervole-toolchain.sh

RUN python3 -m venv /opt/venv
COPY requirements.txt requirements-gateware.txt /tmp/
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-gateware.txt

# The repo is bind-mounted at /work, so the package is importable without an
# editable install baked against a path the image cannot know at build time.
ENV PYTHONPATH=/work
# The bind-mounted repo is owned by the host user, not by root in this container.
RUN git config --system --add safe.directory /work
WORKDIR /work
