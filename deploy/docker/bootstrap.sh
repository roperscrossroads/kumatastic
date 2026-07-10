#!/usr/bin/env sh
# Bootstrap the kumatastic Docker deployment.
#
#   ./bootstrap.sh              # first run: seed config files, then tell you to edit them
#                               # after editing: pull the published image + start the stack
#   ./bootstrap.sh --with-kuma  # also start a bundled throwaway Uptime Kuma (for testing)
#
# Idempotent: existing config files are never overwritten.
set -eu

cd "$(dirname "$0")"

WITH_KUMA=0
[ "${1:-}" = "--with-kuma" ] && WITH_KUMA=1

# --- phase 1: seed config files if missing ---------------------------------
seeded=0
seed() {  # src dst
  if [ ! -f "$2" ]; then cp "$1" "$2"; echo "  created $2"; seeded=1; fi
}
seed .env.example .env
seed kumatastic.yaml.example kumatastic.yaml
seed ../../nodes.yaml.example nodes.yaml

if [ "$seeded" = 1 ]; then
  extra=""; [ "$WITH_KUMA" = 1 ] && extra=" --with-kuma"
  cat <<EOF

Config files created — edit these before starting:

  kumatastic.yaml   collector.meshtastic (tcp:YOUR_RADIO_IP:4403) + the pusher
                    target Kuma URL/credentials
  nodes.yaml        the nodes to monitor  (or set manifest_path to an https URL
                    in kumatastic.yaml and delete nodes.yaml)
  .env              optional secrets (leave blank for a single-host setup)

Then re-run:  ./bootstrap.sh$extra
EOF
  exit 0
fi

# --- phase 2: pull + start --------------------------------------------------
echo "Pulling published image (ghcr.io/roperscrossroads/kumatastic)..."
docker compose pull

if [ "$WITH_KUMA" = 1 ]; then
  docker compose --profile kuma up -d
  cat <<'EOF'

Stack is up: collector + pusher + a bundled Uptime Kuma on http://localhost:3001

One-time steps to finish:
  1. Open http://localhost:3001 and create the admin account, using the SAME
     username/password you set under pusher.targets in kumatastic.yaml.
  2. Create the monitors:
       docker compose run --rm pusher init --target kuma
  3. Watch it work:
       docker compose logs -f
EOF
else
  docker compose up -d
  cat <<'EOF'

Stack is up: collector + pusher.

Finish by creating the monitors on your Kuma (once), using your target's name
from kumatastic.yaml:
    docker compose run --rm pusher init --target <target-name>

Watch it work:
    docker compose logs -f
EOF
fi
