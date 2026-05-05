# SN21 Archive Server — Deployment

Three-tier archive endpoints store off-chain `AES_ct` ciphertexts that
miners' Layer 9.B prediction commits reference. The on-chain `Sha256(AES_ct)`
commit is the integrity anchor; archives just need to serve back bytes whose
SHA-256 matches.

| Tier | Operator | Auth model | Retention |
|---|---|---|---|
| Tier-2 (operator shadow) | Subnet operator | `require_signed_uploads=true` | ≥ 90 days |
| Tier-3 (miner self) | each miner | `require_signed_uploads=false` (typical) | best-effort |

## Run with Docker Compose

```bash
cd deploy/archive_server
docker compose up -d
curl -s http://localhost:8080/healthz
```

The compose file mounts a named volume (`sn21-archive-data`) onto
`/var/lib/sn21-archive` so uploads survive restarts. To change to a host
bind mount, edit `docker-compose.yml`.

## Run with systemd

1. Install the package on the host: `pip install -e .` (creates `/opt/sn21/.venv`).
2. Create the runtime user + dirs:
   ```bash
   sudo useradd -r -d /var/lib/sn21-archive -s /usr/sbin/nologin sn21
   sudo mkdir -p /var/lib/sn21-archive
   sudo chown sn21:sn21 /var/lib/sn21-archive
   ```
3. Drop the unit into `/etc/systemd/system/sn21-archive.service` and enable:
   ```bash
   sudo cp deploy/archive_server/sn21-archive.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now sn21-archive
   sudo systemctl status sn21-archive
   ```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SN21_ARCHIVE_HOST` | `0.0.0.0` | bind address |
| `SN21_ARCHIVE_PORT` | `8080` | port |
| `SN21_ARCHIVE_DIR` | `/var/lib/sn21-archive` | storage root |
| `SN21_ARCHIVE_REQUIRE_SIGNED` | `true` | enforce miner-hotkey signatures on uploads |
| `SN21_ARCHIVE_MAX_BODY_BYTES` | `1048576` | per-upload byte cap |

## Auth (Tier-2)

Uploaders must sign:

```
SHA-256( b"sn21-archive-v1:" + epoch_id_utf8 + b":" + sha256_hex_ascii )
```

with their Bittensor hotkey, send `X-Miner-Hotkey: <ss58>` and
`X-Miner-Signature: <hex>`, and use their own SS58 as the path
`miner_identity`. The server enforces `path == header SS58` so an attacker
cannot fill another miner's slot.

## Reverse proxy / TLS

Operators terminate TLS at a fronting proxy (nginx, caddy, cloud LB). The
container/service listens on plain HTTP. Sample nginx snippet:

```nginx
server {
    listen 443 ssl http2;
    server_name archive.example.io;
    ssl_certificate     /etc/ssl/archive/fullchain.pem;
    ssl_certificate_key /etc/ssl/archive/privkey.pem;

    client_max_body_size 4m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

## Retention pruning

The filesystem store does not auto-expire. Operators run a cron sweep that
deletes `epoch_id` directories older than the retention window:

```bash
# Retention: 90 days for Tier-2.
find /var/lib/sn21-archive -mindepth 1 -maxdepth 1 -type d -mtime +90 -exec rm -rf {} +
```

Phase E may add a built-in TTL store; for now operators control retention.

## Health + observability

- `GET /healthz` returns `{"ok": true}` for liveness probes.
- The app emits structured logs to stdout.
- Per-upload + per-fetch lines include tier, hotkey-prefix, status code,
  and elapsed milliseconds — pipe to whatever log aggregator the operator
  prefers.

## Verification

After deployment, run the verifier from a third party:

```bash
python scripts/verify_epoch.py \
    --epoch-id EPOCH-2026-... \
    --validator-hotkey 5GxVLdpRGZN... \
    --netuid 21 --network finney \
    --tier-2-base https://archive.example.io
```

Expected output: `OK: True` plus per-root match details.
