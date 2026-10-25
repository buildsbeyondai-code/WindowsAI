# SysPerf dashboard -- containerised web/API tier.
#
# The image contains the application and nothing host-specific. Everything that
# has to touch the Windows host (the PowerShell sample, remediation commands,
# version probes) leaves through the host-exec broker over a unix socket, so
# this container runs with no added capabilities, default seccomp, a read-only
# root filesystem and as a non-root user.
#
# Pinned by digest, not by tag: python:3.12-slim moves, and an image that moves
# under a running service is a build you cannot reproduce. build.sh resolves the
# digest and writes Dockerfile.resolved; build that file, not this one.
FROM python@sha256:PYTHON_DIGEST_PLACEHOLDER

LABEL org.opencontainers.image.title="SysPerf Dashboard" \
      org.opencontainers.image.description="System performance dashboard for a Windows host" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Pinned to the versions declared in requirements.txt, plus the two dev
# dependencies the integrated test suite needs. pytest is what the "unit" and
# "coverage" gates shell out to (`sys.executable -m pytest`), and markdown is
# imported by the docs gate -- without them the Auto-Improve tab's test card can
# report a stale result but never run a new one.
RUN pip install --no-cache-dir \
      "Flask==3.1.3" \
      "waitress==3.0.2" \
      "pytest==9.1.1" \
      "markdown==3.7"

WORKDIR /app

# --- host-exec shims -------------------------------------------------------
# One client, installed under the names the application already looks for, so
# no application code knows the broker exists. python3 is deliberately NOT
# shimmed: it is this container's own interpreter. uname is left alone too --
# the container shares the host kernel, so the answer is already correct.
COPY shims/hostexec_client.py /usr/local/lib/hostexec_client.py
RUN set -eux; \
    printf '#!/bin/sh\nexec /usr/local/bin/python3 /usr/local/lib/hostexec_client.py "$0" "$@"\n' \
        > /usr/local/lib/hostexec-wrapper; \
    for name in hostexec powershell.exe pwsh.exe wslpath wsl.exe docker \
                node npm nginx gunicorn git kiro-cli lsb_release; do \
        cp /usr/local/lib/hostexec-wrapper "/usr/local/bin/$name"; \
        chmod 0755 "/usr/local/bin/$name"; \
    done; \
    rm /usr/local/lib/hostexec-wrapper

# --- application -----------------------------------------------------------
# The package is installed as /app/sysperf and run as `python -m sysperf`, the
# same entry point a pip install uses.
COPY app/ /app/

# Three files live next to the code rather than in the data directory, and the
# app resolves them relative to its own package dir. Two are user-maintained
# content the dashboard reads (the component catalog and the hardware
# known-issue watchlist) and one it appends to. On a read-only image layer the
# first two would be absent -- the Components panel and the hardware watchlist
# would render empty with no error -- and the third would fail to open. They are
# symlinked to the writable state directory; install-container.sh seeds it.
RUN set -eux; \
    for f in components.json hw_watchlist.json refresh_components.log; do \
        ln -sf "/data/$f" "/app/sysperf/$f"; \
    done

# The uid should match the host user that owns the state directory and the
# broker socket, so the bind mounts need no permission fiddling. build.sh passes
# the invoking user's uid.
ARG SYSPERF_UID=1000
RUN useradd --uid "${SYSPERF_UID}" --create-home --shell /usr/sbin/nologin sysperf \
 && chown -R "${SYSPERF_UID}:${SYSPERF_UID}" /app
USER ${SYSPERF_UID}:${SYSPERF_UID}

# Serving on all interfaces *inside* the container is fine: compose publishes
# it to 127.0.0.1 only. PERFDASH_SELFTEST_ROOT points the integrated test suite
# at the bind-mounted, writable distribution (/dist) rather than the read-only
# image layer, which has no tests/ and nowhere to write a pytest cache.
ENV PYTHONPATH=/app \
    PERFDASH_HOST=0.0.0.0 \
    PERFDASH_PORT=5056 \
    SYSPERF_DATA_DIR=/data \
    SYSPERF_PS_EXE=/usr/local/bin/powershell.exe \
    SYSPERF_HOSTEXEC_SOCK=/run/sysperf-hostexec/sock \
    PERFDASH_SELFTEST_ROOT=/dist

EXPOSE 5056

# /api/health reports collector state, so a failing sample pipeline shows up as
# an unhealthy container instead of a dashboard that silently serves no data.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["/usr/local/bin/python3", "-c", \
         "import json,urllib.request as u; json.load(u.urlopen('http://127.0.0.1:5056/api/health', timeout=8))"]

CMD ["/usr/local/bin/python3", "-m", "sysperf"]
