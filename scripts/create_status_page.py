#!/usr/bin/env python3
"""Create or update an Uptime Kuma status page with all monitors.

Creates a public status page at /status/<slug> containing every monitor on the
instance, grouped under a single "Mesh Nodes" group sorted alphabetically.

Idempotent — safe to re-run. If the page already exists it updates the config
and monitor list in place.

Requirements:
    pip install python-socketio[client]

Usage:
    # Password via env var (recommended — avoids shell escaping issues):
    KUMA_PASS='secret' python3 scripts/create_status_page.py URL USERNAME

    # Password as argument:
    python3 scripts/create_status_page.py URL USERNAME PASSWORD

    # With custom slug and title:
    python3 scripts/create_status_page.py URL USERNAME PASSWORD --slug mesh --title "My Mesh"

    # With uv (no install needed):
    KUMA_PASS='secret' uv run --with 'python-socketio[client]' python3 scripts/create_status_page.py URL USERNAME

Examples:
    KUMA_PASS='secret' python3 scripts/create_status_page.py http://localhost:3001 admin
    KUMA_PASS='secret' python3 scripts/create_status_page.py https://kuma.example.com admin

Kuma Socket.io API calls used:
    addStatusPage(title, slug)  — creates the status page record
    saveStatusPage(slug, config, imgDataUrl, publicGroupList)  — saves config + monitor groups

The publicGroupList is an array of groups, each with a monitorList of {id: int}.
The config fields analyticsType/footerText/etc must be None (not "") or Kuma 2.x
raises "Data truncated" on the analytics_type column.
"""

import argparse
import os
import sys
import time

import socketio


def get_monitors(sio, timeout=10):
    """Wait for the monitorList event after login."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = sio.receive(timeout=2)
            if event[0] == "monitorList":
                return event[1]
        except socketio.exceptions.TimeoutError:
            continue
    return {}


def main():
    parser = argparse.ArgumentParser(
        description="Create or update an Uptime Kuma status page with all monitors."
    )
    parser.add_argument("url", help="Kuma URL (e.g. http://localhost:3001)")
    parser.add_argument("username", help="Kuma username")
    parser.add_argument("password", nargs="?", default=None,
                        help="Kuma password (or set KUMA_PASS env var)")
    parser.add_argument("--slug", default="all",
                        help="Status page slug (default: all)")
    parser.add_argument("--title", default="CSRA Mesh Network",
                        help="Status page title")
    parser.add_argument("--description", default="Real-time status of CSRA Mesh network nodes monitored by Kumatastic",
                        help="Status page description")
    parser.add_argument("--group", default="Mesh Nodes",
                        help="Monitor group name (default: Mesh Nodes)")
    args = parser.parse_args()

    password = args.password or os.environ.get("KUMA_PASS")
    if not password:
        print("Error: password required (argument or KUMA_PASS env var)", file=sys.stderr)
        sys.exit(1)

    # Connect and login
    sio = socketio.SimpleClient()
    print(f"Connecting to {args.url}...")
    sio.connect(args.url)

    resp = sio.call("login", {"username": args.username, "password": password, "token": ""})
    if not resp.get("ok"):
        print(f"Login failed: {resp.get('msg', resp)}", file=sys.stderr)
        sio.disconnect()
        sys.exit(1)
    print("Logged in.")

    # Get all monitors
    monitors = get_monitors(sio)
    if not monitors:
        print("No monitors found — nothing to do.")
        sio.disconnect()
        return

    print(f"Found {len(monitors)} monitors.")

    # Create status page (no-op if already exists)
    resp = sio.call("addStatusPage", (args.title, args.slug))
    if resp.get("ok"):
        print(f"Created status page '{args.slug}'.")
    else:
        print(f"Status page '{args.slug}' already exists, updating.")

    # Build monitor list sorted by name
    monitor_list = [
        {"id": int(mid)}
        for mid, mon in sorted(monitors.items(), key=lambda x: x[1].get("name", ""))
    ]

    public_group_list = [
        {"name": args.group, "monitorList": monitor_list},
    ]

    # Status page config
    # Note: string fields that map to Kuma enum columns (analyticsType) must be
    # None, not "". Empty string causes "Data truncated" on Kuma 2.x.
    config = {
        "slug": args.slug,
        "title": args.title,
        "description": args.description,
        "theme": "auto",
        "published": True,
        "showTags": True,
        "showPoweredBy": True,
        "showOnlyLastHeartbeat": False,
        "showCertificateExpiry": False,
        "domainNameList": [],
        "footerText": None,
        "customCSS": None,
        "autoRefreshInterval": 300,
        "rssTitle": None,
        "analyticsId": None,
        "analyticsScriptUrl": None,
        "analyticsType": None,
    }

    resp = sio.call("saveStatusPage", (args.slug, config, "", public_group_list))
    if resp.get("ok"):
        print(f"\nStatus page ready: {args.url}/status/{args.slug}")
        print(f"  {len(monitor_list)} monitors in group '{args.group}'")
    else:
        print(f"saveStatusPage failed: {resp.get('msg', resp)}", file=sys.stderr)
        sio.disconnect()
        sys.exit(1)

    sio.disconnect()


if __name__ == "__main__":
    main()
