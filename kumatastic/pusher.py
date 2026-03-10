"""Uptime Kuma pusher - reads state and pushes status to Kuma instances.

The pusher reads node state from the state store, determines UP/DOWN status
based on configured thresholds, and pushes to one or more Uptime Kuma instances.
It has no knowledge of Meshtastic - it only reads from the state store and
pushes to Kuma.
"""

from __future__ import annotations

import json
import logging
import secrets
import signal
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

from .config import DEFAULT_REQUEST_TIMEOUT, KumaTarget, PusherConfig
from .manifest import Manifest, ReloadableManifest, create_manifest, derive_push_token, load_manifest
from .state import NodeSighting, NodeState, StateStore

logger = logging.getLogger(__name__)

# Optional Socket.io import
try:
    import socketio

    SOCKETIO_AVAILABLE = True
except ImportError:
    SOCKETIO_AVAILABLE = False


# Tag colors for Uptime Kuma
TAG_COLORS = {
    "core": "#10B981",
    "infra": "#3B82F6",
    "mobile": "#F59E0B",
    "auto": "#6B7280",
    "stable": "#14B8A6",
    "intermittent": "#F97316",
}


@dataclass
class MonitorInfo:
    """Information about a Kuma monitor."""

    monitor_id: int
    push_token: str
    name: str
    tags: list[str] = field(default_factory=list)


@dataclass
class PushResult:
    """Result of a push cycle."""

    up: list[str] = field(default_factory=list)
    down: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    push_failed: list[str] = field(default_factory=list)
    monitors_created: list[str] = field(default_factory=list)


class KumaConnection:
    """Manages connection to an Uptime Kuma instance."""

    def __init__(
        self,
        target: KumaTarget,
        config: PusherConfig,
    ) -> None:
        """Initialize Kuma connection.

        Args:
            target: Target configuration
            config: Pusher configuration
        """
        self.target = target
        self.config = config

        self._sio: Any | None = None
        self._sio_connected = False
        self._monitors: dict[int, dict[str, Any]] = {}
        self._created_tags: set[str] = set()

        # Local tracking: node_id -> MonitorInfo
        self._node_monitors: dict[str, MonitorInfo] = {}

    def connect(self) -> bool:
        """Connect to Uptime Kuma via Socket.io.

        Returns:
            True if connected, False otherwise.
        """
        if not SOCKETIO_AVAILABLE:
            logger.error("python-socketio not installed")
            return False

        if self._sio_connected and self._sio:
            return True

        if not self.target.url:
            logger.error("No Kuma URL configured")
            return False

        if not self.target.username or not self.target.password:
            logger.error("Kuma username/password not configured")
            return False

        try:
            self._sio = socketio.Client()

            # Handle Kuma 2.x monitor list broadcast
            @self._sio.on("monitorList")
            def _on_monitor_list(data: Any) -> None:
                if isinstance(data, dict):
                    self._monitors = data
                    logger.debug(f"Received monitorList: {len(data)} monitors")

            # Connect
            self._sio.connect(
                self.target.url,
                wait_timeout=self.config.request_timeout,
            )

            # Login
            response = self._sio.call(
                "login",
                {
                    "username": self.target.username,
                    "password": self.target.password,
                    "token": "",
                },
                timeout=self.config.request_timeout,
            )

            if not response.get("ok"):
                logger.error(f"Kuma login failed: {response.get('msg')}")
                self._sio.disconnect()
                self._sio = None
                return False

            self._sio_connected = True
            logger.info(f"Connected to Kuma: {self.target.name}")

            # Refresh monitor list
            self._refresh_monitors()

            return True

        except Exception as e:
            logger.error(f"Failed to connect to Kuma: {e}")
            if self._sio:
                try:
                    self._sio.disconnect()
                except Exception:
                    pass
                self._sio = None
            return False

    def disconnect(self) -> None:
        """Disconnect from Uptime Kuma."""
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass
            self._sio = None
        self._sio_connected = False

    def _refresh_monitors(self) -> None:
        """Refresh the monitor list."""
        if not self._sio or not self._sio_connected:
            return

        try:
            response = self._sio.call(
                "getMonitorList",
                timeout=self.config.request_timeout,
            )
            # Kuma 1.x returns dict directly, 2.x returns {ok: true}
            if isinstance(response, dict) and "ok" not in response:
                self._monitors = response
            elif isinstance(response, dict) and response.get("ok"):
                # Kuma 2.x sends via event
                time.sleep(0.5)

            logger.debug(f"Refreshed {len(self._monitors)} monitors")

            # Build node -> monitor mapping
            self._node_monitors.clear()
            for mid, mdata in self._monitors.items():
                push_token = mdata.get("pushToken", "")
                if isinstance(push_token, str) and push_token.startswith("mesh-"):
                    # Extract node ID from token: mesh-<node_id>-<random>
                    parts = push_token.split("-")
                    if len(parts) >= 2:
                        node_id = f"!{parts[1]}"
                        self._node_monitors[node_id] = MonitorInfo(
                            monitor_id=int(mid),
                            push_token=push_token,
                            name=mdata.get("name", ""),
                        )

        except Exception as e:
            logger.warning(f"Failed to refresh monitors: {e}")

    def get_monitor_for_node(self, node_id: str) -> MonitorInfo | None:
        """Get monitor info for a node.

        Args:
            node_id: Meshtastic node ID

        Returns:
            MonitorInfo if monitor exists, None otherwise.
        """
        return self._node_monitors.get(node_id)

    def create_monitor(self, node_id: str, name: str, push_token: str = "") -> MonitorInfo | None:
        """Create a push monitor for a node.

        Args:
            node_id: Meshtastic node ID
            name: Display name
            push_token: Optional pre-derived push token (for distributed mode).
                        If empty, a random token is generated.

        Returns:
            MonitorInfo if created, None otherwise.
        """
        if not self.connect():
            return None

        try:
            # Use provided token or generate random one
            if not push_token:
                node_id_clean = node_id.lstrip("!")
                push_token = f"mesh-{node_id_clean}-{secrets.token_hex(8)}"

            # Calculate intervals
            push_interval = self.config.push_interval
            monitor_interval = push_interval * self.config.monitor_interval_multiplier
            retry_interval = push_interval * self.config.monitor_retry_multiplier

            monitor_data = {
                "type": "push",
                "name": f"{name} ({node_id})",
                "pushToken": push_token,
                "interval": monitor_interval,
                "retryInterval": retry_interval,
                "maxretries": self.config.maxretries,
                "active": True,
                "notificationIDList": [],
                "accepted_statuscodes": ["200-299"],
                "conditions": "[]",  # Kuma 2.x requires this
            }

            response = self._sio.call(
                "add",
                monitor_data,
                timeout=self.config.request_timeout,
            )

            if not response.get("ok"):
                logger.error(f"Failed to create monitor: {response.get('msg')}")
                return None

            monitor_id = response.get("monitorID")
            if not monitor_id:
                logger.error("No monitor ID returned")
                return None

            # Add default tag
            self._add_tag_to_monitor(monitor_id, self.target.default_tag)

            info = MonitorInfo(
                monitor_id=monitor_id,
                push_token=push_token,
                name=name,
                tags=[self.target.default_tag],
            )
            self._node_monitors[node_id] = info

            logger.info(f"Created monitor for {name} ({node_id}): ID={monitor_id}")
            return info

        except Exception as e:
            logger.error(f"Error creating monitor: {e}")
            return None

    def delete_monitor(self, monitor_id: int) -> bool:
        """Delete a monitor.

        Args:
            monitor_id: Monitor ID to delete

        Returns:
            True if deleted, False otherwise.
        """
        if not self.connect():
            return False

        try:
            response = self._sio.call(
                "deleteMonitor",
                monitor_id,
                timeout=self.config.request_timeout,
            )
            if response.get("ok"):
                logger.info(f"Deleted monitor {monitor_id}")
                return True
            else:
                logger.error(f"Failed to delete monitor: {response.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"Error deleting monitor: {e}")
            return False

    def _ensure_tag(self, tag_name: str) -> int | None:
        """Ensure a tag exists, creating if needed.

        Returns:
            Tag ID if found/created, None otherwise.
        """
        if not self._sio or not self._sio_connected:
            return None

        if tag_name in self._created_tags:
            return self._get_tag_id(tag_name)

        try:
            # Get existing tags
            response = self._sio.call("getTags", timeout=self.config.request_timeout)
            tags = (
                response
                if isinstance(response, list)
                else response.get("tags", [])
                if isinstance(response, dict)
                else []
            )

            for tag in tags:
                if tag.get("name") == tag_name:
                    self._created_tags.add(tag_name)
                    return tag.get("id")

            # Create tag
            color = TAG_COLORS.get(tag_name, "#6B7280")
            response = self._sio.call(
                "addTag",
                {"name": tag_name, "color": color},
                timeout=self.config.request_timeout,
            )

            if response.get("ok"):
                tag_id = response.get("tag", {}).get("id")
                self._created_tags.add(tag_name)
                logger.info(f"Created tag '{tag_name}' with ID {tag_id}")
                return tag_id

        except Exception as e:
            logger.error(f"Error ensuring tag: {e}")

        return None

    def _get_tag_id(self, tag_name: str) -> int | None:
        """Get tag ID by name."""
        if not self._sio or not self._sio_connected:
            return None

        try:
            response = self._sio.call("getTags", timeout=self.config.request_timeout)
            tags = (
                response
                if isinstance(response, list)
                else response.get("tags", [])
                if isinstance(response, dict)
                else []
            )
            for tag in tags:
                if tag.get("name") == tag_name:
                    return tag.get("id")
        except Exception:
            pass
        return None

    def _add_tag_to_monitor(self, monitor_id: int, tag_name: str) -> bool:
        """Add a tag to a monitor."""
        tag_id = self._ensure_tag(tag_name)
        if not tag_id:
            return False

        try:
            response = self._sio.call(
                "addMonitorTag",
                (tag_id, monitor_id, ""),
                timeout=self.config.request_timeout,
            )
            return response.get("ok", False)
        except Exception as e:
            logger.warning(f"Failed to add tag: {e}")
            return False

    def sync_status_page(
        self,
        slug: str = "all",
        title: str = "CSRA Mesh Network",
        description: str = "Real-time status of CSRA Mesh network nodes monitored by Kumatastic",
        group_name: str = "Mesh Nodes",
    ) -> bool:
        """Create or update a status page containing all monitors.

        Idempotent — creates the page if it doesn't exist, updates if it does.
        Monitors are sorted alphabetically and placed in a single group.

        Args:
            slug: Status page URL slug (page at /status/<slug>)
            title: Status page title
            description: Status page description
            group_name: Name of the monitor group

        Returns:
            True if status page was synced, False on error.
        """
        if not self._sio or not self._sio_connected:
            logger.error("Not connected to Kuma")
            return False

        if not self._monitors:
            logger.warning("No monitors found — skipping status page sync")
            return False

        try:
            # Create status page (no-op if already exists)
            resp = self._sio.call(
                "addStatusPage",
                (title, slug),
                timeout=self.config.request_timeout,
            )
            if resp.get("ok"):
                logger.info(f"Created status page '{slug}'")
            else:
                logger.debug(f"Status page '{slug}' already exists, updating")

            # Build monitor list sorted by name
            monitor_list = [
                {"id": int(mid)}
                for mid, mon in sorted(
                    self._monitors.items(),
                    key=lambda x: x[1].get("name", ""),
                )
            ]

            public_group_list = [
                {"name": group_name, "monitorList": monitor_list},
            ]

            config = {
                "slug": slug,
                "title": title,
                "description": description,
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

            resp = self._sio.call(
                "saveStatusPage",
                (slug, config, "", public_group_list),
                timeout=self.config.request_timeout,
            )

            if resp.get("ok"):
                logger.info(
                    f"Status page synced: /status/{slug} "
                    f"({len(monitor_list)} monitors)"
                )
                return True
            else:
                logger.error(f"saveStatusPage failed: {resp.get('msg', resp)}")
                return False

        except Exception as e:
            logger.error(f"Error syncing status page: {e}")
            return False

    def push(
        self,
        push_token: str,
        status: str,
        message: str,
        ping_ms: int | None = None,
    ) -> bool:
        """Push status to a monitor.

        Args:
            push_token: Monitor push token
            status: "up" or "down"
            message: Status message
            ping_ms: Optional response time

        Returns:
            True if push succeeded, False otherwise.
        """
        url = f"{self.target.url.rstrip('/')}/api/push/{push_token}"
        params: dict[str, Any] = {"status": status, "msg": message}
        if ping_ms is not None:
            params["ping"] = ping_ms

        try:
            response = requests.get(
                url,
                params=params,
                timeout=self.config.request_timeout,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning(f"Push failed: {e}")
            return False


class SightingHandler(BaseHTTPRequestHandler):
    """HTTP handler for receiving sightings from remote collectors."""

    def __init__(
        self,
        state_store: StateStore,
        sighting_token: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._state_store = state_store
        self._sighting_token = sighting_token
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        """Route request logs through the module logger."""
        logger.debug(format, *args)

    def _check_auth(self) -> bool:
        """Validate bearer token. Returns True if authorized."""
        if not self._sighting_token:
            return True  # No token configured = open
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self._sighting_token}"

    def do_POST(self) -> None:
        """Handle POST /sighting."""
        if self.path != "/sighting":
            self.send_error(404)
            return

        if not self._check_auth():
            self.send_error(401, "Unauthorized")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_error(400, "Empty body")
            return

        try:
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_error(400, "Invalid JSON")
            return

        node_id = data.get("node_id")
        last_seen = data.get("last_seen")
        source = data.get("source", "remote")

        if not node_id or not isinstance(last_seen, (int, float)):
            self.send_error(400, "Missing node_id or last_seen")
            return

        sighting = NodeSighting(
            node_id=node_id,
            last_seen=float(last_seen),
            source=source,
            name=data.get("name", ""),
            snr=data.get("snr"),
            hops=data.get("hops"),
            battery=data.get("battery"),
            voltage=data.get("voltage"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            altitude=data.get("altitude"),
            via_neighbor=data.get("via_neighbor", False),
            observer_id=data.get("observer_id"),
        )

        self._state_store.update_sighting(sighting)
        logger.debug(f"Received sighting: {node_id} from {source}")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self) -> None:
        """Handle GET /health."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_error(404)


def start_sighting_server(
    listen: str,
    state_store: StateStore,
    sighting_token: str = "",
) -> ThreadingHTTPServer:
    """Start the HTTP sighting server in a background thread.

    Args:
        listen: Bind address as "host:port" (e.g. "0.0.0.0:9100").
        state_store: State store to write received sightings to.
        sighting_token: Bearer token for authentication (empty = no auth).

    Returns:
        The running server instance.
    """
    host, port_str = listen.rsplit(":", 1)
    port = int(port_str)

    handler = partial(SightingHandler, state_store, sighting_token)
    server = ThreadingHTTPServer((host, port), handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.info(f"Sighting server listening on {listen}")
    return server


class KumaPusher:
    """Pushes node status to Uptime Kuma instances."""

    def __init__(
        self,
        state_store: StateStore,
        config: PusherConfig,
        manifest: Manifest | ReloadableManifest | None = None,
    ) -> None:
        """Initialize the pusher.

        Args:
            state_store: State store to read from
            config: Pusher configuration
            manifest: Node manifest (loaded from config.manifest_path if not provided)
        """
        self.state = state_store
        self.config = config

        if manifest is not None:
            self._manifest = manifest
        else:
            self._manifest = create_manifest(config.manifest_path)

        # Create connections for each target
        self._connections: dict[str, KumaConnection] = {}
        for target in config.targets:
            self._connections[target.name] = KumaConnection(target, config)

        self._running = False
        self._stop_event = threading.Event()

    def _compute_status(self, node: NodeState) -> tuple[str, str]:
        """Compute UP/DOWN status for a node.

        Args:
            node: Node state

        Returns:
            Tuple of (status, message).
        """
        now = time.time()
        seconds_ago = now - node.last_seen if node.last_seen > 0 else None

        if seconds_ago is None:
            return "down", "Never seen"

        is_online = seconds_ago < self.config.offline_threshold

        # Format time string
        if seconds_ago < 60:
            time_str = f"{int(seconds_ago)}s ago"
        elif seconds_ago < 3600:
            time_str = f"{int(seconds_ago // 60)}m ago"
        elif seconds_ago < 86400:
            time_str = f"{int(seconds_ago // 3600)}h ago"
        else:
            time_str = f"{int(seconds_ago // 86400)}d ago"

        # Build message
        parts = [f"Last: {time_str}"]
        if node.battery is not None:
            parts.append(f"Bat: {node.battery}%")
        if node.snr is not None:
            parts.append(f"SNR: {node.snr}dB")
        if node.hops is not None:
            if node.hops == 0:
                parts.append("Direct")
            else:
                parts.append(f"{node.hops} hops")

        status = "up" if is_online else "down"
        message = " | ".join(parts)

        return status, message

    def _get_nodes(self) -> dict[str, NodeState]:
        """Get manifest nodes from state store.

        Returns nodes declared in the manifest. Nodes not yet seen
        are returned as empty NodeState with last_seen=0.

        Returns:
            Dict of node_id -> NodeState.
        """
        node_ids = list(self._manifest.nodes.keys())
        found = self.state.get_nodes_by_ids(node_ids)

        # Include manifest nodes not yet in state (never seen)
        for node_id, mnode in self._manifest.nodes.items():
            if node_id not in found:
                found[node_id] = NodeState(node_id=node_id, name=mnode.name)

        return found

    def push_cycle(self) -> dict[str, PushResult]:
        """Run one push cycle for all targets.

        In distributed mode (push_secret configured):
        - Derives deterministic push tokens from the shared secret
        - Only pushes UP nodes — lets Kuma's timer handle DOWN
        - Uses direct HTTP push, no Socket.io monitor discovery needed

        In single-instance mode (no push_secret):
        - Uses Socket.io to discover/create monitors
        - Pushes both UP and DOWN

        Returns:
            Dict of target_name -> PushResult.
        """
        results: dict[str, PushResult] = {}

        nodes = self._get_nodes()
        distributed = self.config.distributed_mode

        for target_name, conn in self._connections.items():
            result = PushResult()

            logger.debug(f"Target {target_name}: {len(nodes)} nodes to push (distributed={distributed})")

            for node_id, node in nodes.items():
                # Use manifest name as authoritative, fall back to state name
                manifest_node = self._manifest.nodes.get(node_id)
                display_name = (manifest_node.name if manifest_node else None) or node.name or node_id

                # Compute status
                status, message = self._compute_status(node)

                if distributed:
                    # Distributed mode: derive token, push UP only
                    if status != "up":
                        result.down.append(display_name)
                        continue

                    push_token = derive_push_token(self.config.push_secret, node_id)

                    # Calculate ping (seconds since last seen, capped)
                    ping_ms = None
                    if node.last_seen > 0:
                        seconds_ago = time.time() - node.last_seen
                        ping_ms = min(int(seconds_ago * 1000), 3600000)

                    success = conn.push(push_token, "up", message, ping_ms)

                    if success:
                        result.up.append(display_name)
                    else:
                        result.push_failed.append(display_name)
                else:
                    # Single-instance mode: Socket.io monitor discovery
                    monitor = conn.get_monitor_for_node(node_id)
                    if not monitor:
                        monitor = conn.create_monitor(node_id, display_name)
                        if monitor:
                            result.monitors_created.append(display_name)
                        else:
                            result.unknown.append(display_name)
                            continue

                    # Calculate ping (seconds since last seen, capped)
                    ping_ms = None
                    if node.last_seen > 0:
                        seconds_ago = time.time() - node.last_seen
                        ping_ms = min(int(seconds_ago * 1000), 3600000)

                    success = conn.push(monitor.push_token, status, message, ping_ms)

                    if success:
                        if status == "up":
                            result.up.append(display_name)
                        else:
                            result.down.append(display_name)
                    else:
                        result.push_failed.append(display_name)

            results[target_name] = result

            # Log summary
            logger.info(
                f"Push to {target_name}: "
                f"{len(result.up)} up, {len(result.down)} down, "
                f"{len(result.push_failed)} failed"
            )

        return results

    def run(self) -> None:
        """Run the pusher daemon (blocking)."""
        logger.info("Starting pusher daemon")
        logger.info(f"Push interval: {self.config.push_interval}s")
        logger.info(f"Offline threshold: {self.config.offline_threshold}s")
        logger.info(f"Targets: {list(self._connections.keys())}")

        self._running = True
        self._stop_event.clear()

        try:
            while self._running and not self._stop_event.is_set():
                try:
                    self.push_cycle()
                except Exception as e:
                    logger.error(f"Push cycle error: {e}")

                # Wait for next cycle
                self._stop_event.wait(timeout=self.config.push_interval)

        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the pusher."""
        logger.info("Stopping pusher...")
        self._running = False
        self._stop_event.set()

        for conn in self._connections.values():
            conn.disconnect()

        logger.info("Pusher stopped")


def run_pusher(
    config: PusherConfig,
    state_store: StateStore,
    handle_signals: bool = True,
    manifest: Manifest | ReloadableManifest | None = None,
) -> None:
    """Run a pusher with signal handling.

    Args:
        config: Pusher configuration
        state_store: State store to read from
        handle_signals: Whether to set up signal handlers
        manifest: Node manifest (loaded from config.manifest_path if not provided)
    """
    pusher = KumaPusher(state_store, config, manifest=manifest)

    if handle_signals:
        def signal_handler(signum: int, frame: Any) -> None:
            logger.info(f"Received signal {signum}, stopping...")
            pusher.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    pusher.run()
