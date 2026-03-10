#!/usr/bin/env python3
"""Delete ALL monitors from an Uptime Kuma instance via Socket.io.

WARNING: This is destructive — it deletes every monitor on the instance.

Requirements:
    pip install python-socketio[client]

Usage:
    KUMA_PASS='secret' python3 scripts/clean_kuma.py URL USERNAME
    python3 scripts/clean_kuma.py URL USERNAME PASSWORD

    # With uv:
    KUMA_PASS='secret' uv run --with 'python-socketio[client]' python3 scripts/clean_kuma.py URL USERNAME
"""

import os
import sys
import time

import socketio


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} URL USERNAME [PASSWORD]")
        print("  (or set KUMA_PASS env var)")
        sys.exit(1)

    url = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("KUMA_PASS", "")

    if not password:
        print("Error: password required (argument or KUMA_PASS env var)", file=sys.stderr)
        sys.exit(1)

    sio = socketio.SimpleClient()
    print(f"Connecting to {url}...")
    sio.connect(url)

    resp = sio.call("login", {"username": username, "password": password, "token": ""})
    if not resp.get("ok"):
        print(f"Login failed: {resp}")
        sys.exit(1)
    print("Logged in.")

    # Wait for monitorList event
    monitors = {}
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            event = sio.receive(timeout=2)
            if event[0] == "monitorList":
                monitors = event[1]
                break
        except socketio.exceptions.TimeoutError:
            continue

    if not monitors:
        print("No monitors found (or timed out waiting for monitorList).")
        sio.disconnect()
        return

    print(f"Found {len(monitors)} monitors. Deleting...")

    deleted = 0
    failed = 0
    for mid, mon in monitors.items():
        name = mon.get("name", "?")
        try:
            resp = sio.call("deleteMonitor", int(mid))
            if resp.get("ok") if isinstance(resp, dict) else resp:
                deleted += 1
                if deleted % 20 == 0:
                    print(f"  ...deleted {deleted}/{len(monitors)}")
            else:
                print(f"  FAILED to delete {mid} ({name}): {resp}")
                failed += 1
        except Exception as e:
            print(f"  ERROR deleting {mid} ({name}): {e}")
            failed += 1

    sio.disconnect()
    print(f"\nDone: {deleted} deleted, {failed} failed (of {len(monitors)} total)")


if __name__ == "__main__":
    main()
