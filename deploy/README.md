# Deployment

**Start here → [Docker](docker/README.md).** Pull the prebuilt multi-arch image,
`docker compose up`, done — the fast path for almost everyone.

```bash
cd deploy/docker
./bootstrap.sh          # copies example configs, pulls + starts the stack
```

## Which path?

| Path | Use it when | Guide |
|------|-------------|-------|
| **Docker** (recommended) | Almost always. Network radio (`tcp:host:4403`), prebuilt image, one `compose` stack. | [`docker/README.md`](docker/README.md) |
| **systemd / bare-metal** | No Docker, or a **USB-serial radio** where a container adds friction. | [`systemd/README.md`](systemd/README.md) |

Both use the same collector/pusher model and the same
[config options](../docs/configuration.md). Already running
[meshtastic-matrix-relay](https://github.com/geoffwhittington/meshtastic-matrix-relay)?
A collector can run as an [mmrelay plugin](../docs/configuration.md#mmrelay-plugin-configuration)
instead of a standalone service.
