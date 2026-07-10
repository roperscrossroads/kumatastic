# Deployment

Two supported paths:

- **[Docker](docker/README.md)** — recommended for most setups. Prebuilt
  distroless image, `docker compose up`, secrets via `.env`.
- **systemd** (below) — for bare-metal installs or USB-serial radios where a
  container adds friction.

Both use the same collector/pusher model and the same
[config options](../docs/configuration.md).

## systemd Quick Setup

```bash
# 1. Install
sudo mkdir -p /opt/kumatastic
sudo python3 -m venv /opt/kumatastic/.venv
sudo /opt/kumatastic/.venv/bin/pip install /path/to/kumatastic
sudo ln -s /opt/kumatastic/.venv/bin/kumatastic /usr/local/bin/kumatastic

# 2. Create state directory
sudo mkdir -p /var/lib/kumatastic
sudo chmod 775 /var/lib/kumatastic

# 3. Copy and edit configs
sudo mkdir -p /etc/kumatastic
sudo cp deploy/examples/collector.yaml /etc/kumatastic/kumatastic-collector.yaml
sudo cp deploy/examples/pusher.yaml /etc/kumatastic/kumatastic-pusher.yaml
sudo chmod 600 /etc/kumatastic/*.yaml  # configs contain passwords

# 4. Install systemd units
sudo cp deploy/examples/kumatastic-collector.service /etc/systemd/system/
sudo cp deploy/examples/kumatastic-pusher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kumatastic-collector kumatastic-pusher

# 5. Initialize monitors on Kuma (once)
kumatastic init --config /etc/kumatastic/kumatastic-pusher.yaml

# 6. Set up sync cron
sudo cp deploy/examples/kumatastic-sync.cron /etc/cron.d/kumatastic-sync
```

## Generating Secrets

```bash
# sighting_token — collector -> pusher HTTP auth (share between a collector
# and the pusher it forwards to)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# push_secret — distributed push (share with every pusher that should feed the
# SAME Kuma instance)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The two secrets do different jobs — see
[Configuration → Secrets: which one do I share?](../docs/configuration.md#secrets-which-one-do-i-share).

## Multi-User State File Access

If both a root service (pusher) and a non-root service (mmrelay plugin) write to the same state file, set group permissions:

```bash
sudo chown root:your-group /var/lib/kumatastic/state.json
sudo chmod 664 /var/lib/kumatastic/state.json
```

Kumatastic preserves file permissions and ownership across atomic writes.

## Example Files

| File | Description |
|------|-------------|
| `collector.yaml` | Collector config — connects to Meshtastic, forwards sightings |
| `pusher.yaml` | Pusher config — pushes to Kuma, runs sighting server |
| `kumatastic-collector.service` | systemd unit for collector |
| `kumatastic-pusher.service` | systemd unit for pusher |
| `kumatastic-sync.cron` | Cron job to sync monitors every 30 minutes |
| `mmrelay-plugin-config.yaml` | mmrelay plugin config snippet |
