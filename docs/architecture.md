# Architecture

## Overview

Kumatastic uses a **decoupled collector/pusher architecture**. Collectors observe the mesh and write sightings. Pushers read sightings and report status to Uptime Kuma. The two halves communicate through a shared state store and optional HTTP forwarding.

```
                        ┌─────────────────┐
                        │  NODE MANIFEST  │
                        │  (nodes.yaml)   │
                        └────────┬────────┘
                                 │ declares which nodes to track
               ┌─────────────────┼─────────────────┐
               ▼                 ▼                  ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   COLLECTOR(S)  │     │   STATE STORE   │     │   PUSHER(S)     │
│                 │     │                 │     │                 │
│ Meshtastic ─────┼────►│ JSON (local)    │────►│ Kuma instance   │
│ Gateway         │──┐  │                 │  ┌──│ (one or many)   │
└─────────────────┘  │  └─────────────────┘  │  └─────────────────┘
                     │  POST /sighting (HTTP) │
                     └───────────────────────►│
                       (fire-and-forget to    │
                        remote pushers)       │
```

## Components

### Manifest (`manifest.py`)

The manifest is the **single source of truth** for which nodes to track. It's a YAML file listing Meshtastic node IDs, names, and tags.

- **File paths** are read once at startup
- **URLs** (http/https) use `ReloadableManifest` — fetched at startup, auto-reloaded every 30 minutes in a background thread
- On reload failure, the previous manifest is kept (no disruption)
- Thread-safe for concurrent access

The manifest drives everything downstream: collectors filter incoming packets against it, pushers only report nodes listed in it, and `kumatastic sync` uses it to create/delete Kuma monitors.

### Collector (`collector.py`)

The collector connects to a Meshtastic device and writes sightings to the state store.

**Sighting sources:**
- **NodeDB updates** — firmware-reported node changes (the primary source)
- **NeighborInfo packets** — other nodes reporting their neighbors (~1/hour)
- **Position packets** — GPS broadcasts
- **Telemetry packets** — battery, voltage, environmental data
- **Any received packet** — even a text message proves the sender is alive

**Key behaviors:**
- Filters all sightings against the manifest — unlisted nodes are ignored
- Prunes stale NeighborInfo entries older than `neighbor_max_age` (default 4h)
- Auto-reconnects with exponential backoff (5s → 300s cap) on TCP connection loss. Detection relies on the meshtastic library's `isConnected` event, checked every 30 seconds.

**HTTP forwarding:** When `pusher_urls` is configured, the collector POSTs each sighting to remote pushers. This is fire-and-forget — failures are logged but never block packet processing.

### State Store (`state.py`)

The state store is the interface between collectors and pushers. It tracks per-node state:

- `NodeState` — last seen time, name, battery, voltage, position, hops, SNR, contributing collectors
- `NodeSighting` — a single observation from a specific collector

Two implementations:
- **`MemoryStore`** — in-memory, used for testing and the mmrelay plugin
- **`JSONFileStore`** — atomic JSON file writes via `mkstemp()`/`os.replace()`. Preserves file permissions and ownership across writes for multi-user access.

Multiple collectors can write to the same state file safely — each write is atomic and uses a unique temp file.

### Pusher (`pusher.py`)

The pusher reads node state and reports to Uptime Kuma.

**Push cycle** (every `push_interval`, default 10 min):
1. Read all manifest nodes from state
2. Compute UP/DOWN based on `offline_threshold` (default 6.5h)
3. Push status to each configured Kuma target

**Two push modes:**

| | Single-instance mode | Distributed mode |
|---|---|---|
| Config | No `push_secret` | `push_secret` set |
| Tokens | Discovered via Socket.io | Deterministic (HMAC-SHA256) |
| Pushes | Both UP and DOWN | UP only |
| DOWN detection | Explicit push | Kuma's dead-man-switch timer |
| Socket.io needed | Yes (for discovery) | No |
| Multi-pusher safe | No | Yes |

**HTTP sighting server:** When `listen` is configured, the pusher runs an HTTP server that accepts `POST /sighting` from remote collectors and writes them to local state. This enables cross-host visibility in many-to-many topologies.

**Status page sync:** The `sync_status_page()` method creates or updates a Kuma status page containing all monitors, sorted alphabetically. Called automatically by `kumatastic sync`.

### Config (`config.py`)

YAML configuration loading with environment variable support for secrets:
- `KUMATASTIC_SIGHTING_TOKEN` — bearer token for HTTP sighting auth
- `KUMATASTIC_SECRET` — shared secret for distributed push mode

Config files are searched in order: `./kumatastic.yaml`, `~/.config/kumatastic/kumatastic.yaml`, `/etc/kumatastic/kumatastic.yaml`.

### CLI (`cli.py`)

| Command | Description |
|---------|-------------|
| `collect` | Run collector daemon |
| `push` | Run pusher daemon (with optional `--once`, `--listen`) |
| `status` | Show current node state table |
| `init` | Create Kuma monitors for all manifest nodes |
| `sync` | Create missing monitors, delete orphans, update status page |

Global flags (`-v`, `--debug`) must come **before** the subcommand.

### mmrelay Plugin (`mmrelay_plugin.py`)

Adapter for running kumatastic as a [meshtastic-matrix-relay](https://github.com/geoffwhittington/meshtastic-matrix-relay) plugin. mmrelay owns the Meshtastic connection — the plugin just receives packets and writes sightings. Can optionally run a pusher in-process.

## Topologies

### Single Host

The simplest setup. One collector and one pusher share a state file on the same machine.

```
┌──────────────────────────────────────────┐
│              Raspberry Pi                │
│                                          │
│  ┌──────────┐  state.json  ┌──────────┐  │
│  │Collector │─────────────►│ Pusher   │  │
│  └────┬─────┘              └────┬─────┘  │
│       │                         │        │
└───────┼─────────────────────────┼────────┘
        │                         │
   Meshtastic                Uptime Kuma
   (USB/TCP)                 (Docker)
```

### Many-to-Many (Distributed Push)

Multiple collectors on different gateways forward sightings to multiple pushers, each pushing to multiple Kuma instances. All pushers use the same `push_secret` to derive identical tokens.

```
┌── Host A ──────────────────┐     ┌── Host B ──────────────────┐
│ Collector (gateway 1)      │     │ Pusher 2                   │
│   ↓ state.json             │────►│   listen: 0.0.0.0:9100     │
│ Pusher 1                   │     │   ↓ state.json             │
│   listen: 0.0.0.0:9100     │     │   → Kuma A (push)          │
│   → Kuma A (push)          │     │   → Kuma B (push)          │
│   → Kuma B (push)          │     └────────────────────────────┘
└────────────────────────────┘
         ▲
         │ POST /sighting
┌── Host C ──────────────────┐
│ Collector (gateway 2)      │
│   ↓ state.json             │
│   (no local pusher)        │
└────────────────────────────┘
```

**Why this works:**
- If ANY collector sees a node, its sighting reaches all pushers via HTTP forwarding
- If ANY pusher sends UP → Kuma stays UP
- If ALL pushers stop → Kuma's dead-man-switch timer marks the node DOWN
- No single point of failure for UP detection

## Data Flow

```
Meshtastic Radio
  → MeshCollector._on_receive()
    → filters against manifest
    → store.update_sighting(NodeSighting)
    → POST /sighting to pusher_urls (fire-and-forget)

Push cycle (every push_interval):
  → KumaPusher.push_cycle()
    → store.get_node(node_id) for each manifest node
    → _compute_status(node) → UP/DOWN based on offline_threshold
    → conn.push(token, "up"/"down", message)
```

## Key Design Decisions

1. **Manifest-driven filtering** — Only declared nodes are tracked. This prevents transient nodes (flyovers, visitors) from polluting the monitoring system.

2. **Conservative offline threshold** — 6.5 hours, based on analysis of 77,715 heartbeats from a production mesh. False DOWN is worse than false UP — it causes alert fatigue.

3. **Fire-and-forget forwarding** — HTTP POST failures never block packet handling. The collector's primary job is writing to local state; forwarding is best-effort.

4. **Deterministic tokens** — In distributed mode, `derive_push_token(secret, node_id)` produces the same token on every host. No coordination needed between pushers.

5. **Atomic state writes** — `mkstemp()` + `os.replace()` prevents corruption from concurrent writers. Permissions/ownership are preserved across writes for multi-user setups (e.g., root pusher + non-root mmrelay).

6. **Kuma monitor interval >> push interval** — The 6x multiplier (10min push → 60min Kuma interval) prevents processing jitter from causing false PENDING states.
