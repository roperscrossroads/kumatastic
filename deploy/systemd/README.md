# systemd / bare-metal deployment

The advanced path — for bare-metal installs, or a **USB-serial radio** where a
container adds friction. Most users want [Docker](../docker/README.md) instead.

Same collector/pusher model and the same
[config options](../../docs/configuration.md) as the Docker path; here you run
the CLI directly under systemd.

## Quick setup

```bash
# 1. Install into a venv and expose the CLI
sudo mkdir -p /opt/kumatastic
sudo python3 -m venv /opt/kumatastic/.venv
sudo /opt/kumatastic/.venv/bin/pip install .        # from a repo checkout
sudo ln -s /opt/kumatastic/.venv/bin/kumatastic /usr/local/bin/kumatastic

# 2. State directory
sudo mkdir -p /var/lib/kumatastic
sudo chmod 775 /var/lib/kumatastic

# 3. Configs (edit for your radio, manifest, and Kuma URL/creds)
sudo mkdir -p /etc/kumatastic
sudo cp deploy/systemd/collector.yaml /etc/kumatastic/kumatastic-collector.yaml
sudo cp deploy/systemd/pusher.yaml    /etc/kumatastic/kumatastic-pusher.yaml
sudo chmod 600 /etc/kumatastic/*.yaml               # configs contain passwords

# 4. systemd units
sudo cp deploy/systemd/kumatastic-collector.service /etc/systemd/system/
sudo cp deploy/systemd/kumatastic-pusher.service    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kumatastic-collector kumatastic-pusher

# 5. Create the monitors on Kuma (once)
kumatastic init --config /etc/kumatastic/kumatastic-pusher.yaml

# 6. Sync cron — reconcile monitors + status page every 30 min
sudo cp deploy/systemd/kumatastic-sync.cron /etc/cron.d/kumatastic-sync
```

> **Uptime Kuma must be 2.x.** kumatastic creates monitors with the Uptime Kuma
> 2.x schema; a 1.x Kuma rejects them with `table monitor has no column named
> conditions`. See the [Docker guide](../docker/README.md#throwaway-kuma-for-testing)
> for the version note and the fresh-2.x setup-wizard caveat.

## Generating secrets

```bash
# sighting_token — collector -> pusher HTTP auth (share between a collector
# and the pusher it forwards to)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# push_secret — distributed push (share with every pusher that should feed the
# SAME Kuma instance)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The two secrets do different jobs — see
[Configuration → Secrets: which one do I share?](../../docs/configuration.md#secrets-which-one-do-i-share).

## Multi-user state file access

If both a root service (pusher) and a non-root service (mmrelay plugin) write to
the same state file, set group permissions:

```bash
sudo chown root:your-group /var/lib/kumatastic/state.json
sudo chmod 664 /var/lib/kumatastic/state.json
```

Kumatastic preserves file permissions and ownership across atomic writes.

## Files here

| File | Description |
|------|-------------|
| `collector.yaml` | Collector config — connects to Meshtastic, forwards sightings |
| `pusher.yaml` | Pusher config — pushes to Kuma, runs the sighting server |
| `kumatastic-collector.service` | systemd unit for the collector |
| `kumatastic-pusher.service` | systemd unit for the pusher |
| `kumatastic-sync.cron` | Cron job to sync monitors every 30 minutes |
| `mmrelay-plugin-config.yaml` | mmrelay plugin config snippet (collector-as-plugin) |
