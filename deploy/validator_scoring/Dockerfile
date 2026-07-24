FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install the package + its full runtime dependency set. The scoring runner
# needs bittensor (chain reads + commit-reveal), cryptography (ed25519),
# cbor2 (canonical CBOR), httpx (archive fetch), and bittensor_drand (TLE).
COPY pyproject.toml README.md ./
COPY hope ./hope
COPY scripts ./scripts
RUN pip install --upgrade pip wheel \
 && pip install --prefix=/install .

# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:${PATH}"

# Runtime needs jq for release discovery + curl for the backend probe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl jq ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Bittensor wallets live under $HOME/.bittensor by
# default — keep that path stable across runs by mounting it from a
# persistent volume in the scheduler config.
RUN useradd -r -u 1000 -m -d /home/sn21 sn21 \
 && mkdir -p /home/sn21/.bittensor /etc/sn21/keys \
 && chown -R sn21:sn21 /home/sn21 /etc/sn21

COPY --from=builder /install /usr/local
COPY deploy/validator_scoring/run.sh /opt/sn21/run.sh
RUN chmod +x /opt/sn21/run.sh

USER sn21
WORKDIR /home/sn21
ENV HOME=/home/sn21

# This image runs `hope-validator` once per invocation and exits. Schedule
# it via cron / systemd timer / Render Cron Job / Kubernetes CronJob.
ENTRYPOINT ["/opt/sn21/run.sh"]
