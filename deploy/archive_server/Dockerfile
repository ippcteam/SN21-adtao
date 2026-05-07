FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Copy only what's needed to install. The archive server depends on
# fastapi + cryptography + cbor2 + bittensor_drand (transitively); the rest
# of the project is unused server-side. The image carries the full package
# for now (simpler) — Phase E may slim this down via a sub-package install.
COPY pyproject.toml README.md ./
COPY hope ./hope
RUN pip install --upgrade pip wheel \
 && pip install --prefix=/install ".[server]" 2>/dev/null \
 || pip install --prefix=/install fastapi uvicorn cryptography cbor2 bittensor-drand httpx pydantic

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SN21_ARCHIVE_DIR=/var/lib/sn21-archive \
    SN21_ARCHIVE_HOST=0.0.0.0 \
    SN21_ARCHIVE_PORT=8080 \
    SN21_ARCHIVE_REQUIRE_SIGNED=true

# Non-root user; storage owned by it.
RUN useradd -r -u 1000 -m -d /home/sn21 sn21 \
 && mkdir -p /var/lib/sn21-archive \
 && chown -R sn21:sn21 /var/lib/sn21-archive

COPY --from=builder /install /usr/local
COPY --from=builder /build/hope /opt/hope
ENV PYTHONPATH=/opt

USER sn21
WORKDIR /home/sn21
EXPOSE 8080

# Healthcheck hits /healthz directly so the platform sees a clear up signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status == 200 else 1)"

# Entrypoint launches uvicorn with our app factory and a filesystem store.
COPY deploy/archive_server/entrypoint.py /opt/entrypoint.py
CMD ["python", "/opt/entrypoint.py"]
