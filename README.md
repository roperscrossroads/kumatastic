# Kumatastic

**Meshtastic mesh network monitoring with [Uptime Kuma](https://github.com/louislam/uptime-kuma).**

Monitor your Meshtastic nodes from multiple collectors. Each collector connects to one Meshtastic radio; if any collector has heard a node recently, it's UP.

```
  Meshtastic              Kumatastic             Uptime Kuma
 ┌─────────┐          ┌────────────────┐       ┌───────────┐
 │ Radio A │──┐       │                │       │           │
 └─────────┘  ├──────►│  Collector(s)  │       │  Status   │
 ┌─────────┐  │       │       ↓        │       │  Page     │
 │ Radio B │──┘       │  State Store   │──────►│           │
 └─────────┘          │       ↓        │       │  !node1 ✓ │
 ┌─────────┐          │   Pusher(s)    │       │  !node2 ✓ │
 │ Radio C │─────────►│                │       │  !node3 ✗ │
 │(mmrelay)│          └────────────────┘       └───────────┘
 └─────────┘
```

**How it works:** Collectors connect to Meshtastic radios and record node sightings. Pushers read the sightings and report UP/DOWN status to Uptime Kuma. A shared node manifest (`nodes.yaml`) controls which nodes are tracked.

> **Status:** Kumatastic has been tested on a single production deployment (3 collectors, 2 pushers, ~20 nodes across 2 hosts). It works, but wider testing is needed. Documentation and deployment simplification are ongoing — contributions and feedback welcome.

**Key features:**
- Multiple collectors merge visibility — no single point of observation
- HTTP sighting forwarding between hosts for cross-collector awareness
- Push to multiple Kuma instances (internal dashboard, public status page, etc.)
- Auto-reconnects on connection loss with exponential backoff
- Scales from a single Raspberry Pi to a distributed multi-host setup
- Works standalone or as a [meshtastic-matrix-relay](https://github.com/geoffwhittington/meshtastic-matrix-relay) plugin

## Quick Start

The fast path is **Docker** — pull the prebuilt multi-arch image, no build:

```bash
git clone https://github.com/roperscrossroads/kumatastic
cd kumatastic/deploy/docker
./bootstrap.sh --with-kuma      # seeds config files, then (on re-run) pulls + starts
```

`bootstrap.sh` copies the example configs and tells you what to edit — your radio
and Kuma URL in `kumatastic.yaml`, the nodes to watch in `nodes.yaml`. Re-run it
to pull the image and start the stack, then create the monitors once:

```bash
docker compose run --rm pusher init --target kuma
```

`--with-kuma` even bundles a throwaway Uptime Kuma to point at while you try it.

**→ Full walkthrough, secrets, and options: [Docker deployment guide](deploy/docker/README.md).**

> **Uptime Kuma 2.x required** — kumatastic uses the 2.x monitor schema; a 1.x
> Kuma rejects monitors with `table monitor has no column named conditions`.

**Other ways to run:** [systemd / bare-metal](deploy/systemd/README.md) (e.g. a
USB-serial radio), or as an [mmrelay plugin](docs/configuration.md#mmrelay-plugin-configuration)
if you already run meshtastic-matrix-relay.

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
 │ Collector (radio 1)│─ POST ─►│ Pusher 2           │
 │ Collector (radio 2)│─ /sighting ─►│  → Kuma A     │
 │ Pusher 1           │         │  → Kuma B          │
 │   → Kuma A         │         └────────────────────┘
 │   → Kuma B         │
 └────────────────────┘
```

In **distributed mode** (`push_secret`), all pushers derive identical push tokens per node using HMAC-SHA256. Only UP is pushed — Kuma's dead-man-switch timer handles DOWN. No coordination needed between hosts.

A collector can also run **on its own**, forwarding sightings to a remote pusher over HTTP — contribute a radio to someone else's Uptime Kuma without running any pusher yourself. See [Docker guide → Collector-only](deploy/docker/README.md#collector-only-feed-a-remote-pusher).

See [Architecture](docs/architecture.md) for details on topologies, data flow, and design decisions.

## Documentation

| Doc | Description |
|-----|-------------|
| [Configuration Reference](docs/configuration.md) | All config options, env vars, manifest format |
| [Architecture](docs/architecture.md) | Components, topologies, data flow, design decisions |
| [Tuning Guide](docs/tuning.md) | Threshold tuning, common patterns, troubleshooting |
| [Docker Guide](deploy/docker/README.md) | Container images, compose, secrets |
| [systemd Guide](deploy/systemd/README.md) | Bare-metal install, secrets, multi-user permissions |

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
