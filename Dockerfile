# syntax=docker/dockerfile:1
#
# Multi-stage build for kumatastic.
#
# The runtime base (ghcr.io/roperscrossroads/python) is a Wolfi distroless
# image: Python 3.13, no shell, no pip, runs as uid/gid 1000. So we install
# everything in a pip-capable builder and copy the packages onto the runtime's
# sys.path. Both stages are glibc, so manylinux wheels copy across cleanly.

# Runtime base, declared before the first stage so it's usable in the final FROM.
ARG BASE_IMAGE=ghcr.io/roperscrossroads/python:latest

# ---- builder: install kumatastic + all deps into a relocatable prefix ----
FROM python:3.13-slim-bookworm AS builder

# Build tooling for any dependency without a prebuilt wheel. Discarded with
# this stage, so it never lands in the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

# Install into /install/... (prefix layout) so the packages can be dropped onto
# the runtime's sys.path. ".[all]" pulls meshtastic (collector) and
# python-socketio (pusher single-instance discovery).
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install ".[all]"

# Pre-create the state dir owned by the runtime's nonroot uid, so a fresh
# named volume mounted here inherits the right ownership on first use.
RUN mkdir -p /rootfs/var/lib/kumatastic

# ---- runtime: Wolfi distroless python 3.13 (uid/gid 1000) ----
FROM ${BASE_IMAGE}

# This base's python resolves imports from /usr/lib/python3.13/site-packages
# (verify with `python3.13 -c "import sysconfig; print(sysconfig.get_path('purelib'))"`).
COPY --from=builder /install/lib/python3.13/site-packages/ /usr/lib/python3.13/site-packages/
COPY --from=builder --chown=1000:1000 /rootfs/var/lib/kumatastic /var/lib/kumatastic

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Base image already runs as uid 1000. Override its `python3.13` entrypoint to
# launch the CLI module; `command:` in compose supplies the subcommand.
ENTRYPOINT ["/usr/bin/python3.13", "-m", "kumatastic"]
CMD ["--help"]
