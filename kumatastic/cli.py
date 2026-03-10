"""Command-line interface for Kumatastic.

Usage:
    kumatastic collect [--config kumatastic.yaml]
    kumatastic push [--config kumatastic.yaml]
    kumatastic push --once [--config kumatastic.yaml]
    kumatastic status [--config kumatastic.yaml]
    kumatastic init --target public [--config kumatastic.yaml]
    kumatastic sync [--config kumatastic.yaml]
    kumatastic sync --target internal [--config kumatastic.yaml]

If --config is omitted, searches ./kumatastic.yaml,
~/.config/kumatastic/kumatastic.yaml, /etc/kumatastic/kumatastic.yaml.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, find_config_file, get_state_path_from_config
from .manifest import create_manifest, load_manifest
from .state import JSONFileStore, StateStore, create_store


def resolve_config(args: argparse.Namespace) -> str:
    """Return config path from --config flag or find_config_file() fallback."""
    if args.config:
        return args.config
    found = find_config_file()
    if found:
        return str(found)
    print(
        "Error: No config file found. Use --config or place kumatastic.yaml "
        "in ./, ~/.config/kumatastic/, or /etc/kumatastic/",
        file=sys.stderr,
    )
    sys.exit(1)


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """Set up logging configuration."""
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_collect(args: argparse.Namespace) -> int:
    """Run the collector daemon."""
    from .collector import run_collector

    config = Config.load(resolve_config(args))
    if not config.collector:
        print("Error: No collector configuration found", file=sys.stderr)
        return 1

    state_store = create_store("json", path=config.collector.state_path)
    run_collector(config.collector, state_store)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Run the pusher daemon or one-shot push."""
    from .pusher import KumaPusher, run_pusher, start_sighting_server

    config = Config.load(resolve_config(args))
    if not config.pusher:
        print("Error: No pusher configuration found", file=sys.stderr)
        return 1

    state_store = create_store("json", path=config.pusher.state_path)

    # CLI --listen overrides config
    listen = getattr(args, "listen", None) or config.pusher.listen

    # Start HTTP sighting server if configured
    sighting_server = None
    if listen:
        sighting_server = start_sighting_server(
            listen, state_store, config.pusher.sighting_token
        )

    if args.once:
        # One-shot push
        pusher = KumaPusher(state_store, config.pusher)
        results = pusher.push_cycle()

        for target_name, result in results.items():
            print(f"\n{target_name}:")
            print(f"  Up: {len(result.up)}")
            print(f"  Down: {len(result.down)}")
            if result.monitors_created:
                print(f"  Created: {len(result.monitors_created)}")
            if result.push_failed:
                print(f"  Failed: {len(result.push_failed)}")

        pusher.stop()
        if sighting_server:
            sighting_server.shutdown()
        return 0

    # Daemon mode
    try:
        run_pusher(config.pusher, state_store)
    finally:
        if sighting_server:
            sighting_server.shutdown()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show current state status."""
    config = Config.load(resolve_config(args))
    state_path = get_state_path_from_config(config)

    # Load manifest
    manifest_path = "nodes.yaml"
    if config.collector:
        manifest_path = config.collector.manifest_path
    elif config.pusher:
        manifest_path = config.pusher.manifest_path
    manifest = create_manifest(manifest_path)

    state_store = create_store("json", path=state_path) if Path(state_path).exists() else None
    stored_nodes = state_store.get_all_nodes() if state_store else {}

    now = time.time()
    offline_threshold = (
        config.pusher.offline_threshold if config.pusher else 23400
    )

    # Build display list from manifest, merging state data
    display_nodes = []
    for node_id, mnode in manifest.nodes.items():
        state_node = stored_nodes.get(node_id)
        display_nodes.append((node_id, mnode, state_node))

    # Sort: nodes with state by last_seen desc, then unseen nodes
    display_nodes.sort(
        key=lambda t: t[2].last_seen if t[2] else -1,
        reverse=True,
    )

    # Print header
    print(f"{'Node ID':<14} {'Name':<25} {'Last Seen':<12} {'Status':<8} {'Collectors'}")
    print("-" * 80)

    online_count = 0
    offline_count = 0

    for node_id, mnode, state_node in display_nodes:
        if state_node and state_node.last_seen > 0:
            seconds_ago = now - state_node.last_seen

            if seconds_ago < 60:
                time_str = f"{int(seconds_ago)}s ago"
                status = "UP"
            elif seconds_ago < 3600:
                time_str = f"{int(seconds_ago // 60)}m ago"
                status = "UP" if seconds_ago < offline_threshold else "DOWN"
            elif seconds_ago < 86400:
                time_str = f"{int(seconds_ago // 3600)}h ago"
                status = "UP" if seconds_ago < offline_threshold else "DOWN"
            else:
                time_str = f"{int(seconds_ago // 86400)}d ago"
                status = "DOWN"
        else:
            time_str = "never"
            status = "UNKNOWN"

        if status == "UP":
            online_count += 1
        else:
            offline_count += 1

        name = mnode.name[:25]
        collectors = ",".join(state_node.collectors.keys()) if state_node else "-"

        print(f"{node_id:<14} {name:<25} {time_str:<12} {status:<8} {collectors}")

    print("-" * 80)
    print(f"Total: {len(manifest)} nodes ({online_count} up, {offline_count} down)")

    if args.json:
        print("\nJSON output:")
        output = {}
        for node_id, mnode, state_node in display_nodes:
            if state_node:
                output[node_id] = state_node.to_dict()
            else:
                output[node_id] = {"node_id": node_id, "name": mnode.name, "last_seen": 0}
        print(json.dumps(output, indent=2))

    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize monitors on a Kuma target."""
    from .manifest import derive_push_token
    from .pusher import KumaConnection

    config = Config.load(resolve_config(args))
    if not config.pusher:
        print("Error: No pusher configuration found", file=sys.stderr)
        return 1

    # Find target
    target = None
    for t in config.pusher.targets:
        if t.name == args.target:
            target = t
            break

    if not target:
        print(f"Error: Target '{args.target}' not found in config", file=sys.stderr)
        print(f"Available targets: {[t.name for t in config.pusher.targets]}")
        return 1

    # Load manifest
    manifest = create_manifest(config.pusher.manifest_path)

    if not manifest.nodes:
        print("No nodes in manifest")
        return 0

    distributed = config.pusher.distributed_mode
    if distributed:
        print(f"Distributed mode: using deterministic tokens from push_secret")

    # Connect to Kuma
    conn = KumaConnection(target, config.pusher)
    if not conn.connect():
        print("Failed to connect to Kuma", file=sys.stderr)
        return 1

    created = 0
    skipped = 0

    for node_id, mnode in manifest.nodes.items():
        # Check if monitor exists
        existing = conn.get_monitor_for_node(node_id)
        if existing:
            print(f"  Skip {node_id}: monitor already exists (ID={existing.monitor_id})")
            skipped += 1
            continue

        # Derive token if in distributed mode
        token = ""
        if distributed:
            token = derive_push_token(config.pusher.push_secret, node_id)

        # Create monitor
        result = conn.create_monitor(node_id, mnode.name, push_token=token)
        if result:
            print(f"  Created {node_id}: {mnode.name} (ID={result.monitor_id}, token={result.push_token})")
            created += 1
        else:
            print(f"  FAILED {node_id}: {mnode.name}")

    conn.disconnect()

    print(f"\nDone: {created} created, {skipped} skipped")
    return 0


def _sync_target(
    target_name: str,
    conn: "KumaConnection",
    manifest_node_ids: set[str],
    manifest_nodes: dict,
    distributed: bool,
    push_secret: str,
) -> tuple[int, int, int, int]:
    """Sync one Kuma target: create missing monitors, delete orphans.

    Returns:
        (created, skipped, deleted, failed) counts.
    """
    from .manifest import derive_push_token

    created = 0
    skipped = 0
    deleted = 0
    failed = 0

    # --- Create monitors for manifest nodes that don't have one ---
    for node_id in manifest_node_ids:
        existing = conn.get_monitor_for_node(node_id)
        if existing:
            skipped += 1
            continue

        mnode = manifest_nodes[node_id]
        token = ""
        if distributed:
            token = derive_push_token(push_secret, node_id)

        result = conn.create_monitor(node_id, mnode.name, push_token=token)
        if result:
            print(f"  [{target_name}] Created {node_id}: {mnode.name} (ID={result.monitor_id})")
            created += 1
        else:
            print(f"  [{target_name}] FAILED to create {node_id}: {mnode.name}")
            failed += 1

    # --- Delete monitors for nodes no longer in manifest ---
    # _node_monitors was populated by connect() -> _refresh_monitors()
    monitored_node_ids = set(conn._node_monitors.keys())
    orphans = monitored_node_ids - manifest_node_ids

    for node_id in sorted(orphans):
        monitor = conn._node_monitors[node_id]
        print(f"  [{target_name}] Deleting {node_id}: {monitor.name} (ID={monitor.monitor_id})")
        if conn.delete_monitor(monitor.monitor_id):
            deleted += 1
        else:
            print(f"  [{target_name}] FAILED to delete {node_id} (ID={monitor.monitor_id})")
            failed += 1

    return created, skipped, deleted, failed


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync monitors: create missing, delete orphans.

    Idempotent — safe to run from cron every 30 minutes.
    """
    from .pusher import KumaConnection

    config = Config.load(resolve_config(args))
    if not config.pusher:
        print("Error: No pusher configuration found", file=sys.stderr)
        return 1

    # Load manifest
    manifest = create_manifest(config.pusher.manifest_path)
    manifest_node_ids = set(manifest.nodes.keys())

    if not manifest_node_ids:
        print("No nodes in manifest")
        return 0

    distributed = config.pusher.distributed_mode
    if distributed:
        print(f"Distributed mode: using deterministic tokens from push_secret")

    # Determine which targets to sync
    targets = config.pusher.targets
    if args.target:
        targets = [t for t in targets if t.name == args.target]
        if not targets:
            print(f"Error: Target '{args.target}' not found in config", file=sys.stderr)
            print(f"Available targets: {[t.name for t in config.pusher.targets]}")
            return 1

    print(f"Manifest: {len(manifest_node_ids)} nodes")

    total_created = 0
    total_deleted = 0
    had_errors = False

    for target in targets:
        print(f"\nTarget: {target.name} ({target.url})")
        conn = KumaConnection(target, config.pusher)
        if not conn.connect():
            print(f"  Failed to connect — skipping")
            had_errors = True
            continue

        created, skipped, deleted, failed = _sync_target(
            target.name,
            conn,
            manifest_node_ids,
            manifest.nodes,
            distributed,
            config.pusher.push_secret,
        )

        if failed:
            had_errors = True

        print(f"  Result: {created} created, {skipped} ok, {deleted} deleted, {failed} failed")
        total_created += created
        total_deleted += deleted

        # Sync status page (refresh monitors first if any were created/deleted)
        if created or deleted:
            conn._refresh_monitors()
        if conn.sync_status_page():
            print(f"  Status page /status/all synced ({len(conn._monitors)} monitors)")
        else:
            print(f"  Warning: status page sync failed")

        conn.disconnect()

    if total_created == 0 and total_deleted == 0 and not had_errors:
        print("\nAll targets in sync.")
    elif had_errors:
        print(f"\nDone with errors: {total_created} created, {total_deleted} deleted")
    else:
        print(f"\nDone: {total_created} created, {total_deleted} deleted")

    return 1 if had_errors else 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="kumatastic",
        description="Decoupled Meshtastic node monitoring with Uptime Kuma",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # collect command
    collect_parser = subparsers.add_parser(
        "collect",
        help="Run the collector daemon",
    )
    collect_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: auto-detect)",
    )

    # push command
    push_parser = subparsers.add_parser(
        "push",
        help="Run the pusher daemon or one-shot push",
    )
    push_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: auto-detect)",
    )
    push_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one push cycle and exit",
    )
    push_parser.add_argument(
        "--listen",
        type=str,
        default=None,
        help="Start sighting server on host:port (e.g. 0.0.0.0:9100)",
    )

    # status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show current state status",
    )
    status_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: auto-detect)",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Also output JSON format",
    )

    # init command
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize monitors on a Kuma target",
    )
    init_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: auto-detect)",
    )
    init_parser.add_argument(
        "--target",
        type=str,
        required=True,
        help="Kuma target name to initialize",
    )

    # sync command
    sync_parser = subparsers.add_parser(
        "sync",
        help="Sync monitors: create missing, delete orphans (idempotent)",
    )
    sync_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: auto-detect)",
    )
    sync_parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Sync only this Kuma target (default: all targets)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, debug=args.debug)

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "collect":
            return cmd_collect(args)
        elif args.command == "push":
            return cmd_push(args)
        elif args.command == "status":
            return cmd_status(args)
        elif args.command == "init":
            return cmd_init(args)
        elif args.command == "sync":
            return cmd_sync(args)
        else:
            parser.print_help()
            return 1
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
