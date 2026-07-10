# Docker Deployment

Run kumatastic as containers — the fast path for most setups. The image is a
prebuilt, multi-arch (amd64 + arm64) Wolfi **distroless** build published to
`ghcr.io/roperscrossroads/kumatastic`, so you **pull, don't build**. Collector
and pusher run as separate containers sharing one state volume. For bare-metal /
systemd see [`../systemd/README.md`](../systemd/README.md).

## Quick start (single host)

```bash
cd deploy/docker
./bootstrap.sh          # seeds config files (first run), then pulls + starts the stack
```

`bootstrap.sh` copies the three example configs, tells you what to edit, and on
the next run does `docker compose pull` + `up -d`. Add `--with-kuma` to also spin
up a throwaway Uptime Kuma for testing. To do it by hand instead:

```bash
cp .env.example .env                        # secrets (optional)
cp kumatastic.yaml.example kumatastic.yaml  # set Kuma URL + credentials + radio
cp ../../nodes.yaml.example nodes.yaml      # which nodes to monitor
# ...edit those three...

docker compose pull                         # fetch the published image (no build)
docker compose up -d                        # start collector + pusher

docker compose run --rm pusher init --target kuma   # create the monitors (once)
```

Check it's working:

```bash
docker compose logs -f
docker compose run --rm pusher status
```

### Throwaway Kuma for testing

Don't have a Kuma to point at? Bring up a bundled one (Uptime Kuma **2.4.0**,
pinned) and target it from `kumatastic.yaml` (`url: http://uptime-kuma:3001`):

```bash
docker compose --profile kuma up -d         # adds uptime-kuma on :3001
```

The compose pre-seeds `db-config.json` so it skips the Kuma 2.x "Setup Database"
wizard, but you still **create the admin account once** — open
<http://localhost:3001> and set a username/password that match the `username`/
`password` in your `kumatastic.yaml`. Only then can the pusher log in:

```bash
docker compose run --rm pusher init --target kuma   # now succeeds
```

> **Kuma version matters.** kumatastic uses the Uptime Kuma **2.x** monitor
> schema, so the bundled Kuma is pinned to a 2.x tag. A **1.x** Kuma rejects
> every monitor with `table monitor has no column named conditions`. Point at a
> 2.x instance (2.0.x–2.4.x all work).

## Collector-only (feed a remote pusher)

The collector runs independently of the pusher. Set `pusher_urls` and it reads its radio,
filters against the manifest, and forwards each sighting over HTTP (`POST /sighting`) to a
pusher on another host — no local pusher required. This is "Host C" in the
[many-to-many topology](../../docs/architecture.md#many-to-many-distributed-push): a radio
feeding a Kuma instance hosted elsewhere.

The `collector` and `pusher` compose services are independent (no `depends_on`), so just
start the collector:

```bash
docker compose up -d collector        # the pusher never starts
```

Or run it standalone, no compose — the one-liner to hand a remote contributor:

```bash
docker run -d --name kumatastic-collector --restart unless-stopped \
  -v ./kumatastic.yaml:/etc/kumatastic/kumatastic.yaml:ro \
  ghcr.io/roperscrossroads/kumatastic:latest -v collect
```

Your `kumatastic.yaml` needs **only** a `collector:` section — `collect` never reads
`pusher:`:

```yaml
collector:
  id: "my-radio"                        # unique name for your collector
  meshtastic: "tcp:192.168.1.50:4403"   # your radio (TCP or serial)
  manifest_path: "https://…/nodes.yaml" # the SAME manifest the remote pusher uses
  pusher_urls:
    - "https://remote-pusher.example.com:9100"
  sighting_token: "…"                   # or set KUMATASTIC_SIGHTING_TOKEN in .env
```

Three things the remote pusher's operator must give you:

1. **their pusher URL** → `pusher_urls`,
2. **the `sighting_token`** it expects (bearer auth on `POST /sighting` — see
   [Secrets](../../docs/configuration.md#secrets-which-one-do-i-share)),
3. **their manifest URL** → `manifest_path`, so your node set matches theirs (a collector
   only records/forwards nodes that are in its manifest).

Notes:

- **`--network host`** is only needed if the radio is on `localhost`; a `tcp:host:4403`
  radio elsewhere on your LAN works on the default bridge network.
- **No state volume is required** for a pure forwarder — the collector still keeps a local
  `state.json` inside the container, but nothing else reads it, so it's fine to let it be
  ephemeral. Add `-v state:/var/lib/kumatastic` only if you want that record to survive
  restarts.
- With a **URL manifest** (above) you don't mount `nodes.yaml` at all. Use a local file +
  `-v ./nodes.yaml:/etc/kumatastic/nodes.yaml:ro` only if you're not sharing the pusher's URL.

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | collector + pusher (+ optional Kuma) sharing a `state` volume |
| `.env.example` | `KUMATASTIC_SECRET` / `KUMATASTIC_SIGHTING_TOKEN` — copy to `.env` |
| `kumatastic.yaml.example` | app config — copy to `kumatastic.yaml` |
| `kuma-db-config.json` | pre-selects SQLite so the bundled Kuma 2.x skips its setup wizard |

`kumatastic.yaml` and `nodes.yaml` are bind-mounted read-only at
`/etc/kumatastic/`. State is a named volume at `/var/lib/kumatastic`, shared by
both containers and pre-owned by uid 1000 so writes succeed.


## Secrets

Set these in `.env`; they override the matching keys in `kumatastic.yaml` so
you can keep secrets out of the mounted config.

- **`KUMATASTIC_SECRET`** — shared secret for *distributed push*. Share this
  with another operator so their pusher reports to the **same Kuma monitors**.
- **`KUMATASTIC_SIGHTING_TOKEN`** — bearer token for *collector → pusher* HTTP
  forwarding. Only needed when a collector forwards sightings to a remote pusher.

See [Configuration → Secrets](../../docs/configuration.md#secrets-which-one-do-i-share)
for the full explanation of the difference.

## Building the image

The **published** image at `ghcr.io/roperscrossroads/kumatastic` is a multi-arch
(amd64 + arm64) manifest built in CI — see
[`.github/workflows/build.yml`](../../.github/workflows/build.yml). It doesn't use
this Dockerfile: CI layers the pip dependencies onto the Wolfi base with apko/crane
(daemonless, no emulation — arm64 wheels are cross-downloaded), matching the
`ghcr.io/roperscrossroads/python` base's own build. For most users, just
`docker compose pull` — you don't need to build anything.

The Dockerfile below is the **local-dev** path, for contributors iterating without
the CI runner:

```bash
# from the repo root
docker build -t kumatastic:dev .

# pin or swap the runtime base
docker build --build-arg BASE_IMAGE=ghcr.io/roperscrossroads/python:20260101 -t kumatastic:test .
```

The build is multi-stage: a pip-capable `python:3.13-slim` builder installs
`kumatastic[all]` into a prefix, and the packages are copied onto the distroless
runtime's `sys.path`. Both stages are glibc, so wheels copy across cleanly.

The base image is multi-arch (amd64 + arm64). By default the build targets your
host's architecture. To build for a specific one — or both at once — use buildx:

```bash
# a single non-native arch (needs binfmt/qemu registered for emulation)
docker buildx build --platform linux/arm64 -t kumatastic:arm64 --load .

# both arches in one push-ready multi-arch manifest
docker buildx build --platform linux/amd64,linux/arm64 \
    -t ghcr.io/roperscrossroads/kumatastic:latest --push .
```

## Notes & limitations

- **Serial radios:** the example uses a network radio (`tcp:host:4403`). For a
  USB radio, uncomment the `devices:` passthrough in `docker-compose.yml`, set
  `meshtastic: "serial:/dev/ttyUSB0"`, and note the container runs as uid 1000 —
  the device must be group-readable by that uid.
- **Architecture:** the base image publishes a multi-arch manifest for both
  **amd64** and **arm64** (Raspberry Pi), so `docker build` picks the right base
  for your host automatically. To build for the other architecture, pass
  `--platform` — see [Building the image](#building-the-image).
- **Manifest by URL:** instead of bind-mounting `nodes.yaml`, you can set
  `manifest_path` to an `https://` URL in `kumatastic.yaml` and drop the
  `nodes.yaml` volume — the manifest is then auto-reloaded every 30 minutes.
