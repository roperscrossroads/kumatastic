# Configuration Reference

## Config File Locations

Kumatastic searches for config files in this order:

1. `./kumatastic.yaml` (current directory)
2. `~/.config/kumatastic/kumatastic.yaml` (user config)
3. `/etc/kumatastic/kumatastic.yaml` (system config)

Use `--config FILE` to specify a path directly.

See `kumatastic.yaml.example` for a fully annotated template.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `KUMATASTIC_SIGHTING_TOKEN` | Bearer token for HTTP sighting auth (overrides config file) |
| `KUMATASTIC_SECRET` | Shared secret for distributed push mode (overrides config file) |

## Secrets: which one do I share?

Kumatastic has two shared secrets. They sound similar but do completely
different jobs, so it's worth being clear about which is which.

| | `sighting_token` / `KUMATASTIC_SIGHTING_TOKEN` | `push_secret` / `KUMATASTIC_SECRET` |
|---|---|---|
| **What it is** | Bearer token on the `POST /sighting` HTTP endpoint | Shared secret used to derive Kuma push tokens (HMAC-SHA256) |
| **What it protects** | Collector → pusher sighting forwarding | Which Kuma monitors a pusher writes to |
| **Who shares it** | A collector and the pusher it forwards to | Every pusher that should feed the **same Kuma instance** |
| **Enables** | Authenticated cross-host sighting ingest | Distributed push mode (deterministic tokens, UP-only push) |
| **If blank** | Sighting endpoint accepts unauthenticated POSTs | Single-instance mode (tokens discovered via Socket.io) |

**If someone wants to feed the same Uptime Kuma instance, share
`push_secret` (`KUMATASTIC_SECRET`).** Because each node's Kuma push token is
`HMAC-SHA256(push_secret, node_id)`, every pusher holding the same secret
derives the same token per node and therefore writes to the *same monitors* —
no coordination needed. If instead they only run a *collector* that forwards
sightings to your pusher over HTTP, they need your `sighting_token`, and your
pusher does the actual Kuma pushing.

> **Security:** anyone with `push_secret` can push arbitrary UP/DOWN status for
> any node in your manifest — share it only with operators you trust to
> co-report the mesh. A blank `sighting_token` leaves the sighting endpoint
> open, so set one whenever `listen` is exposed beyond localhost.

See [Architecture → Push modes](architecture.md#pusher-pusherpy) for how
distributed mode uses these tokens.

## Manifest (`nodes.yaml`)

The manifest declares which nodes to monitor. It can be a local file path or a URL.

- **File paths** are read once at startup
- **URLs** (http/https) are auto-reloaded every 30 minutes

```yaml
nodes:
  "!aabbccdd":
    name: "Base Station Alpha"
    tags: ["core"]
  "!11223344":
    name: "Hilltop Repeater"
    tags: ["infra"]
  "!deadbeef":
    name: "Mobile Unit 7"
    # tags defaults to ["auto"]
```

## Collector Configuration

```yaml
collector:
  id: "collector-north"                  # Unique collector identifier
  meshtastic: "tcp:192.168.1.100:4403"   # TCP or serial connection
  state_path: "/var/lib/kumatastic/state.json"
  manifest_path: "nodes.yaml"            # File path or URL
  neighbor_max_age: 14400                # 4 hours (NeighborInfo retention)

  # HTTP sighting forwarding (optional)
  pusher_urls:                           # POST sightings to these pushers
    - "http://localhost:9100"
    - "https://remote-host:9100"
  sighting_token: "token"               # Bearer auth (or env: KUMATASTIC_SIGHTING_TOKEN)
```

### Collector Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `id` | (required) | Unique identifier for this collector |
| `meshtastic` | (required) | Connection string: `tcp:host:port` or serial path |
| `state_path` | `./state.json` | Path to shared state file |
| `manifest_path` | `./nodes.yaml` | Path or URL to node manifest |
| `neighbor_max_age` | 14400 (4h) | NeighborInfo sighting retention in seconds |
| `pusher_urls` | (none) | List of pusher URLs to forward sightings to |
| `sighting_token` | (none) | Bearer token for sighting POST auth |

## Pusher Configuration

```yaml
pusher:
  state_path: "/var/lib/kumatastic/state.json"
  manifest_path: "nodes.yaml"            # File path or URL
  offline_threshold: 23400               # 6.5h — time before node is DOWN
  push_interval: 600                     # 10 min — push cycle frequency
  request_timeout: 10                    # HTTP/Socket.io timeout

  # HTTP sighting server (optional)
  listen: "0.0.0.0:9100"                # Accept POSTs from remote collectors
  sighting_token: "token"               # Bearer auth (or env: KUMATASTIC_SIGHTING_TOKEN)

  # Distributed mode (optional)
  push_secret: "shared-secret"           # or env: KUMATASTIC_SECRET

  # Kuma monitor tuning
  maxretries: 6                          # Beats before Kuma marks DOWN
  monitor_interval_multiplier: 6         # Kuma interval = push_interval × 6
  monitor_retry_multiplier: 3            # Kuma retry = push_interval × 3

  targets:
    - name: "internal"
      url: "http://kuma-internal:3001"
      username: "admin"
      password: "secret"
      default_tag: "auto"
    - name: "public"
      url: "http://kuma-public:3001"
      username: "admin"
      password: "secret"
      default_tag: "stable"
```

### Pusher Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `state_path` | `./state.json` | Path to shared state file |
| `manifest_path` | `./nodes.yaml` | Path or URL to node manifest |
| `offline_threshold` | 23400 (6.5h) | Seconds since last sighting before node is DOWN |
| `push_interval` | 600 (10min) | Seconds between push cycles |
| `request_timeout` | 10 | HTTP/Socket.io timeout in seconds |
| `listen` | (none) | `host:port` to run HTTP sighting server |
| `sighting_token` | (none) | Bearer token for sighting auth |
| `push_secret` | (none) | Shared secret to enable distributed mode |
| `maxretries` | 6 | Heartbeats in PENDING before Kuma confirms DOWN |
| `monitor_interval_multiplier` | 6 | Kuma monitor interval = push_interval x this |
| `monitor_retry_multiplier` | 3 | Kuma retry interval = push_interval x this |

### Kuma Target Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `name` | (required) | Target name (used in CLI: `--target NAME`) |
| `url` | (required) | Kuma instance URL |
| `username` | (required) | Kuma login username |
| `password` | (required) | Kuma login password |
| `default_tag` | `"auto"` | Tag applied to newly created monitors |

## mmrelay Plugin Configuration

When running as a [meshtastic-matrix-relay](https://github.com/geoffwhittington/meshtastic-matrix-relay) plugin, configuration goes in mmrelay's `config.yaml`:

```yaml
custom-plugins:
  kumatastic:
    active: true
    state_path: "/var/lib/kumatastic/state.json"
    collector_id: "mmrelay-1"
    manifest_path: "https://raw.githubusercontent.com/you/repo/main/nodes.yaml"

    # Forward sightings to remote pushers (optional)
    pusher_urls:
      - "https://remote-host:9100"
    sighting_token: "your-bearer-token"

    # Optional: run pusher in-process
    pusher:
      enabled: true
      offline_threshold: 23400
      push_interval: 600
      listen: "0.0.0.0:9100"
      sighting_token: "your-bearer-token"
      push_secret: "your-shared-secret"
      targets:
        - name: "internal"
          url: "http://kuma:3001"
          username: "admin"
          password: "secret"
```

### Plugin Installation

```bash
# Install kumatastic in the mmrelay environment
pipx inject mmrelay /path/to/kumatastic

# Copy plugin (must be named plugin.py)
mkdir -p ~/.mmrelay/plugins/custom/kumatastic
cp /path/to/kumatastic/kumatastic/mmrelay_plugin.py \
   ~/.mmrelay/plugins/custom/kumatastic/plugin.py
```
