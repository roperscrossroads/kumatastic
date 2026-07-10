# Kumatastic

**Meshtastic mesh network monitoring with [Uptime Kuma](https://github.com/louislam/uptime-kuma).**

Monitor your Meshtastic nodes from multiple gateways. If any gateway sees a node, it's UP.

```
  Meshtastic              Kumatastic             Uptime Kuma
 ┌──────────┐         ┌────────────────┐       ┌───────────┐
 │ Gateway A │──┐      │                │       │           │
 └──────────┘  ├─────►│  Collector(s)  │       │  Status   │
 ┌──────────┐  │      │       ↓        │       │  Page     │
 │ Gateway B │──┘      │  State Store   │──────►│           │
 └──────────┘         │       ↓        │       │  !node1 ✓ │
 ┌──────────┐         │   Pusher(s)    │       │  !node2 ✓ │
 │ Gateway C │────────►│               │       │  !node3 ✗ │
 │ (mmrelay) │         └────────────────┘       └───────────┘
 └──────────┘
```

**How it works:** Collectors connect to Meshtastic radios and record node sightings. Pushers read the sightings and report UP/DOWN status to Uptime Kuma. A shared node manifest (`nodes.yaml`) controls which nodes are tracked.

> **Status:** Kumatastic has been tested on a single production deployment (3 collectors, 2 pushers, ~20 nodes across 2 hosts). It works, but wider testing is needed. Documentation and deployment simplification are ongoing — contributions and feedback welcome.

**Key features:**
- Multiple collectors merge visibility — no single point of observation
- HTTP sighting forwarding between hosts for cross-gateway awareness
- Push to multiple Kuma instances (internal dashboard, public status page, etc.)
- Auto-reconnects on connection loss with exponential backoff
- Scales from a single Raspberry Pi to a distributed multi-host setup
- Works standalone or as a [meshtastic-matrix-relay](https://github.com/geoffwhittington/meshtastic-matrix-relay) plugin

## Quick Start

### 1. Install

```bash
pip install -e ".[all]"
```

Or minimal: `pip install -e .` and add `meshtastic` or `python-socketio[client]` as needed.

### 2. Define your nodes

```yaml
# nodes.yaml — only these nodes will be monitored
nodes:
  "!aabbccdd":
    name: "Base Station Alpha"
    tags: ["core"]
  "!11223344":
    name: "Hilltop Repeater"
    tags: ["infra"]
```

### 3. Configure

```yaml
# kumatastic.yaml
collector:
  id: "gateway-1"
  meshtastic: "tcp:192.168.1.100:4403"
  state_path: "/var/lib/kumatastic/state.json"
  manifest_path: "nodes.yaml"

pusher:
  state_path: "/var/lib/kumatastic/state.json"
  manifest_path: "nodes.yaml"
  targets:
    - name: "kuma"
      url: "http://localhost:3001"
      username: "admin"
      password: "your-password"
```

See [Configuration Reference](docs/configuration.md) for all options.

### 4. Run

```bash
# Create monitors on Kuma (once)
kumatastic init --target kuma

# Start collector and pusher
kumatastic collect &
kumatastic push &

# Check status
kumatastic status
```

### 5. Keep in sync

```bash
kumatastic sync
```

Creates monitors for new nodes, deletes orphans, and updates the status page. Run from cron every 30 minutes:

```
*/30 * * * * root /usr/local/bin/kumatastic sync --config /etc/kumatastic/kumatastic.yaml
```

## CLI

| Command | Description |
|---------|-------------|
| `kumatastic collect` | Run collector daemon |
| `kumatastic push` | Run pusher daemon |
| `kumatastic push --once` | One-shot push (for testing) |
| `kumatastic status` | Show current node state |
| `kumatastic init --target NAME` | Create monitors for all manifest nodes |
| `kumatastic sync` | Sync monitors and status page |

`--config FILE` is optional — searches `./kumatastic.yaml`, `~/.config/kumatastic/kumatastic.yaml`, `/etc/kumatastic/kumatastic.yaml` in order.

## Scaling Up

The simplest setup is one collector and one pusher on the same host. For production, kumatastic supports a **many-to-many topology** where multiple collectors forward sightings to multiple pushers over HTTP:

```
  Host A                          Host B
 ┌────────────────────┐          ┌────────────────────┐
 │ Collector (gw 1)   │─ POST ─►│ Pusher 2           │
 │ Collector (gw 2)   │─ /sighting ─►│  → Kuma A     │
 │ Pusher 1           │         │  → Kuma B          │
 │   → Kuma A         │         └────────────────────┘
 │   → Kuma B         │
 └────────────────────┘
```

In **distributed mode** (`push_secret`), all pushers derive identical push tokens per node using HMAC-SHA256. Only UP is pushed — Kuma's dead-man-switch timer handles DOWN. No coordination needed between hosts.

See [Architecture](docs/architecture.md) for details on topologies, data flow, and design decisions.

## Deployment

**Docker (recommended):**

```bash
cd deploy/docker
cp .env.example .env                        # secrets (optional)
cp kumatastic.yaml.example kumatastic.yaml  # Kuma URL + credentials
cp ../../nodes.yaml.example nodes.yaml      # your nodes
docker compose up -d --build
docker compose run --rm pusher init --target kuma   # create monitors (once)
```

See the [Docker guide](deploy/docker/README.md) for details, or [`deploy/`](deploy/) for the **systemd** path (units, example configs, cron jobs).

The [mmrelay plugin](docs/configuration.md#mmrelay-plugin-configuration) can be used instead of a standalone collector if you're already running meshtastic-matrix-relay.

## Documentation

| Doc | Description |
|-----|-------------|
| [Configuration Reference](docs/configuration.md) | All config options, env vars, manifest format |
| [Architecture](docs/architecture.md) | Components, topologies, data flow, design decisions |
| [Tuning Guide](docs/tuning.md) | Threshold tuning, common patterns, troubleshooting |
| [Docker Guide](deploy/docker/README.md) | Container images, compose, secrets |
| [Deployment Guide](deploy/README.md) | systemd setup, secrets, multi-user permissions |

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Author

Designed and tested by [Adam Roper](https://github.com/roperscrossroads) for the [Georgia Statewide Mesh Coalition](https://github.com/georgia-statewide-mesh-coalition).

- GSMC repo: [georgia-statewide-mesh-coalition/kumatastic](https://github.com/georgia-statewide-mesh-coalition/kumatastic)
- Original repo: [roperscrossroads/kumatastic](https://github.com/roperscrossroads/kumatastic)

## License

MIT

## Acknowledgments

- [Meshtastic](https://meshtastic.org/) — the mesh networking project that makes this possible
- [Uptime Kuma](https://github.com/louislam/uptime-kuma) — the excellent self-hosted monitoring tool
- [meshtastic-matrix-relay](https://github.com/geoffwhittington/meshtastic-matrix-relay) — where the original plugin was developed
