"""Meshtastic-Matrix-Relay plugin adapter for Kumatastic.

This plugin integrates kumatastic with mmrelay, allowing you to use mmrelay's
Meshtastic connection while benefiting from kumatastic's multi-target pusher.

The plugin:
1. Receives Meshtastic packets from mmrelay
2. Writes sightings to the kumatastic state store
3. Optionally runs the pusher in background threads

This gives you the best of both worlds:
- mmrelay handles Meshtastic connection, Matrix bridging, and other plugins
- kumatastic handles multi-target Uptime Kuma pushing

Configuration (config.yaml):
    custom-plugins:
      kumatastic:
        active: true

        # State store path (shared with standalone pusher if running)
        state_path: "/var/lib/kumatastic/state.json"

        # Collector ID for this mmrelay instance
        collector_id: "mmrelay-1"

        # Optional: run pusher in-process (or run standalone pusher separately)
        pusher:
          enabled: true
          offline_threshold: 23400
          push_interval: 600
          targets:
            - name: "internal"
              url: "http://kuma:3001"
              username: "admin"
              password: "secret"
              node_filter: "all"
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import requests
from mmrelay.plugins.base_plugin import BasePlugin

# Import kumatastic components
try:
    from kumatastic.state import JSONFileStore, NodeSighting, StateStore
    from kumatastic.config import PusherConfig, KumaTarget
    from kumatastic.manifest import Manifest, create_manifest, load_manifest
    from kumatastic.pusher import KumaPusher
    KUMATASTIC_AVAILABLE = True
except ImportError:
    KUMATASTIC_AVAILABLE = False

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface
    from nio import MatrixRoom, RoomMessageText

# Default configuration
DEFAULT_STATE_PATH = "/var/lib/kumatastic/state.json"
DEFAULT_COLLECTOR_ID = "mmrelay"
DEFAULT_NEIGHBOR_MAX_AGE = 14400  # 4 hours


class Plugin(BasePlugin):
    """Kumatastic adapter plugin for meshtastic-matrix-relay.

    Writes Meshtastic sightings to the kumatastic state store, enabling
    the kumatastic pusher to push to multiple Uptime Kuma instances.

    Configuration:
        custom-plugins:
          kumatastic:
            active: true
            state_path: "/var/lib/kumatastic/state.json"
            collector_id: "mmrelay-1"
            neighbor_max_age: 14400

            # Optional: enable in-process pusher
            pusher:
              enabled: true
              offline_threshold: 23400
              push_interval: 600
              targets:
                - name: "kuma"
                  url: "http://localhost:3001"
                  username: "admin"
                  password: "secret"
    """

    plugin_name = "kumatastic"
    is_core_plugin = False

    def __init__(self, plugin_name: str | None = None) -> None:
        """Initialize the plugin."""
        super().__init__(plugin_name)
        self._state_store: StateStore | None = None
        self._collector_id = DEFAULT_COLLECTOR_ID
        self._manifest: Manifest | None = None
        self._pusher: KumaPusher | None = None
        self._pusher_thread: threading.Thread | None = None
        self._pusher_stop_event = threading.Event()

        # HTTP push to remote pushers
        self._pusher_urls: list[str] = []
        self._sighting_token: str = ""
        self._push_pool: ThreadPoolExecutor | None = None

        # Track neighbor sightings for multi-hop visibility
        self._neighbor_sightings: dict[str, dict[str, dict[str, Any]]] = {}
        self._neighbor_lock = threading.Lock()

    @property
    def description(self) -> str:
        """Return plugin description."""
        return "Kumatastic adapter - writes sightings to kumatastic state store"

    def get_matrix_commands(self) -> list[str]:
        """Return Matrix command names."""
        return ["kumatastic"]

    def start(self) -> None:
        """Start the plugin."""
        if not KUMATASTIC_AVAILABLE:
            self.logger.error(
                "kumatastic package not installed. "
                "Install with: pip install kumatastic"
            )
            return

        # Initialize state store
        state_path = self.config.get("state_path", DEFAULT_STATE_PATH)
        self._collector_id = self.config.get("collector_id", DEFAULT_COLLECTOR_ID)

        # Load manifest
        manifest_path = self.config.get("manifest_path", "nodes.yaml")
        try:
            self._manifest = create_manifest(manifest_path)
            self.logger.info(f"Loaded manifest: {len(self._manifest)} nodes from {manifest_path}")
        except Exception as e:
            self.logger.error(f"Failed to load manifest: {e}")
            return

        try:
            self._state_store = JSONFileStore(state_path)
            self.logger.info(f"Kumatastic state store: {state_path}")
            self.logger.info(f"Collector ID: {self._collector_id}")
        except Exception as e:
            self.logger.error(f"Failed to initialize state store: {e}")
            return

        # Configure HTTP push to remote pushers
        import os
        self._pusher_urls = self.config.get("pusher_urls", [])
        self._sighting_token = self.config.get("sighting_token", "") or os.environ.get(
            "KUMATASTIC_SIGHTING_TOKEN", ""
        )
        if self._pusher_urls:
            self._push_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sighting-push")
            self.logger.info(f"Forwarding sightings to {len(self._pusher_urls)} pusher(s)")

        # Optionally start in-process pusher
        pusher_config = self.config.get("pusher", {})
        if pusher_config.get("enabled", False):
            self._start_pusher(pusher_config)

        super().start()

    def on_stop(self) -> None:
        """Stop the plugin."""
        self._stop_pusher()
        if self._push_pool:
            self._push_pool.shutdown(wait=False)

    def _start_pusher(self, pusher_config: dict[str, Any]) -> None:
        """Start the in-process pusher thread."""
        # Build pusher config
        targets = []
        for t in pusher_config.get("targets", []):
            targets.append(KumaTarget(
                name=t.get("name", "default"),
                url=t.get("url", ""),
                username=t.get("username", ""),
                password=t.get("password", ""),
                default_tag=t.get("default_tag", "auto"),
            ))

        if not targets:
            self.logger.warning("No pusher targets configured")
            return

        config = PusherConfig(
            state_path=self.config.get("state_path", DEFAULT_STATE_PATH),
            offline_threshold=pusher_config.get("offline_threshold", 23400),
            push_interval=pusher_config.get("push_interval", 600),
            request_timeout=pusher_config.get("request_timeout", 10),
            maxretries=pusher_config.get("maxretries", 6),
            targets=targets,
        )

        self._pusher = KumaPusher(self._state_store, config, manifest=self._manifest)
        self._pusher_stop_event.clear()

        def pusher_loop() -> None:
            """Pusher thread main loop."""
            self.logger.info("Pusher thread started")
            while not self._pusher_stop_event.is_set():
                try:
                    self._pusher.push_cycle()
                except Exception as e:
                    self.logger.error(f"Pusher cycle error: {e}")

                self._pusher_stop_event.wait(timeout=config.push_interval)

            self._pusher.stop()
            self.logger.info("Pusher thread stopped")

        self._pusher_thread = threading.Thread(target=pusher_loop, daemon=True)
        self._pusher_thread.start()
        self.logger.info(f"Started in-process pusher with {len(targets)} target(s)")

    def _stop_pusher(self) -> None:
        """Stop the in-process pusher thread."""
        if self._pusher_thread and self._pusher_thread.is_alive():
            self._pusher_stop_event.set()
            self._pusher_thread.join(timeout=5)

    def _record_sighting(
        self,
        node_id: str,
        name: str = "",
        snr: float | None = None,
        hops: int | None = None,
        battery: int | None = None,
        voltage: float | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        altitude: float | None = None,
        via_neighbor: bool = False,
        observer_id: str | None = None,
    ) -> None:
        """Record a node sighting to the state store."""
        if not self._state_store:
            return

        # Normalize node ID
        if not node_id.startswith("!"):
            node_id = f"!{node_id}"

        # Skip nodes not in manifest
        if self._manifest and not self._manifest.contains(node_id):
            return

        sighting = NodeSighting(
            node_id=node_id,
            last_seen=time.time(),
            source=self._collector_id,
            name=name,
            snr=snr,
            hops=hops,
            battery=battery,
            voltage=voltage,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            via_neighbor=via_neighbor,
            observer_id=observer_id,
        )

        try:
            self._state_store.update_sighting(sighting)
            self._forward_sighting(sighting)
            self.logger.debug(f"Recorded sighting: {node_id}")
        except Exception as e:
            self.logger.warning(f"Failed to record sighting for {node_id}: {e}")

    def _forward_sighting(self, sighting: NodeSighting) -> None:
        """Forward a sighting to configured pusher URLs (fire-and-forget)."""
        if not self._push_pool or not self._pusher_urls:
            return

        payload: dict[str, Any] = {
            "node_id": sighting.node_id,
            "last_seen": sighting.last_seen,
            "source": sighting.source,
        }
        if sighting.name:
            payload["name"] = sighting.name
        if sighting.snr is not None:
            payload["snr"] = sighting.snr
        if sighting.hops is not None:
            payload["hops"] = sighting.hops
        if sighting.battery is not None:
            payload["battery"] = sighting.battery
        if sighting.voltage is not None:
            payload["voltage"] = sighting.voltage
        if sighting.latitude is not None:
            payload["latitude"] = sighting.latitude
        if sighting.longitude is not None:
            payload["longitude"] = sighting.longitude
        if sighting.altitude is not None:
            payload["altitude"] = sighting.altitude
        if sighting.via_neighbor:
            payload["via_neighbor"] = True
        if sighting.observer_id:
            payload["observer_id"] = sighting.observer_id

        for url in self._pusher_urls:
            self._push_pool.submit(self._post_sighting, url, payload)

    def _post_sighting(self, base_url: str, payload: dict[str, Any]) -> None:
        """POST a sighting to a pusher URL. Runs in background thread."""
        url = f"{base_url.rstrip('/')}/sighting"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._sighting_token:
            headers["Authorization"] = f"Bearer {self._sighting_token}"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=5)
            resp.raise_for_status()
        except Exception as e:
            self.logger.warning(f"Failed to forward sighting to {base_url}: {e}")

    async def handle_meshtastic_message(
        self,
        packet: dict[str, Any],
        formatted_message: str,
        longname: str,
        meshnet_name: str,
    ) -> bool:
        """Handle incoming Meshtastic packet."""
        _ = formatted_message, longname, meshnet_name

        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum", "")
        from_id = packet.get("fromId")

        if not from_id:
            return False

        # Normalize node ID
        if not from_id.startswith("!"):
            from_id = f"!{from_id}"

        # Handle NeighborInfo for multi-hop visibility
        if portnum == "NEIGHBORINFO_APP":
            self._handle_neighbor_info(packet)
            return False

        # Handle position packets
        if portnum == "POSITION_APP":
            position = decoded.get("position", {})
            if position:
                self._record_sighting(
                    node_id=from_id,
                    latitude=position.get("latitude"),
                    longitude=position.get("longitude"),
                    altitude=position.get("altitude"),
                )
            return False

        # Handle telemetry packets
        if portnum == "TELEMETRY_APP":
            telemetry = decoded.get("telemetry", {})
            device_metrics = telemetry.get("deviceMetrics", {})
            if device_metrics:
                self._record_sighting(
                    node_id=from_id,
                    battery=device_metrics.get("batteryLevel"),
                    voltage=device_metrics.get("voltage"),
                )
            return False

        # Any other packet is a basic sighting
        self._record_sighting(node_id=from_id)
        return False

    def _handle_neighbor_info(self, packet: dict[str, Any]) -> None:
        """Handle NeighborInfo packet for multi-hop visibility."""
        decoded = packet.get("decoded", {})
        neighbor_info = decoded.get("neighborinfo", {})
        observer_id = packet.get("fromId")

        if not observer_id or not neighbor_info.get("neighbors"):
            return

        if not observer_id.startswith("!"):
            observer_id = f"!{observer_id}"

        self.logger.debug(
            f"NeighborInfo from {observer_id}: "
            f"{len(neighbor_info['neighbors'])} neighbors"
        )

        for neighbor in neighbor_info.get("neighbors", []):
            raw_node_id = neighbor.get("node_id")
            if not raw_node_id:
                continue

            # Convert numeric ID to hex format
            target_id = f"!{raw_node_id:08x}"
            snr = neighbor.get("snr")

            self._record_sighting(
                node_id=target_id,
                snr=snr,
                via_neighbor=True,
                observer_id=observer_id,
            )

    def background_job(self) -> None:
        """Periodic background job - scan nodeDB for sightings."""
        from mmrelay.meshtastic_utils import connect_meshtastic

        meshtastic_client = connect_meshtastic()
        if not meshtastic_client:
            return

        nodes = getattr(meshtastic_client, "nodes", {})
        if not nodes:
            return

        self.logger.debug(f"Scanning nodeDB: {len(nodes)} nodes")

        for node_id, node_info in nodes.items():
            if not node_id.startswith("!"):
                node_id = f"!{node_id}"

            user = node_info.get("user", {})
            name = user.get("longName") or user.get("shortName") or ""
            device_metrics = node_info.get("deviceMetrics", {})
            position = node_info.get("position", {})

            # Only record if node has been heard recently
            last_heard = node_info.get("lastHeard", 0)
            if last_heard > 0:
                self._record_sighting(
                    node_id=node_id,
                    name=name,
                    snr=node_info.get("snr"),
                    hops=node_info.get("hopsAway"),
                    battery=device_metrics.get("batteryLevel"),
                    voltage=device_metrics.get("voltage"),
                    latitude=position.get("latitude"),
                    longitude=position.get("longitude"),
                    altitude=position.get("altitude"),
                )

        # Prune stale neighbor sightings
        self._prune_stale_neighbors()

    def _prune_stale_neighbors(self) -> None:
        """Remove stale neighbor sightings."""
        max_age = self.config.get("neighbor_max_age", DEFAULT_NEIGHBOR_MAX_AGE)
        cutoff = time.time() - max_age

        with self._neighbor_lock:
            for target_id in list(self._neighbor_sightings.keys()):
                observers = self._neighbor_sightings[target_id]
                for observer_id in list(observers.keys()):
                    if observers[observer_id].get("time", 0) < cutoff:
                        del observers[observer_id]
                if not observers:
                    del self._neighbor_sightings[target_id]

    async def handle_room_message(
        self,
        room: "MatrixRoom",
        event: "RoomMessageText",
        full_message: str,
    ) -> bool:
        """Handle Matrix room message."""
        if not self.matches(event):
            return False

        args = self.extract_command_args("kumatastic", full_message)
        if args is None:
            return False

        parts = args.split()
        subcommand = parts[0].lower() if parts else "status"

        if subcommand == "status":
            response = self._cmd_status()
        elif subcommand == "help":
            response = self._cmd_help()
        else:
            response = self._cmd_help()

        await self.send_matrix_message(room.room_id, response)
        return True

    def _cmd_help(self) -> str:
        """Return help text."""
        return """**Kumatastic Commands**

- `!kumatastic status` - Show state store summary
- `!kumatastic help` - Show this help"""

    def _cmd_status(self) -> str:
        """Generate status summary."""
        if not self._state_store:
            return "State store not initialized"

        nodes = self._state_store.get_all_nodes()
        if not nodes:
            return "No nodes in state store"

        now = time.time()
        offline_threshold = self.config.get("pusher", {}).get("offline_threshold", 23400)

        online = 0
        offline = 0

        for node in nodes.values():
            seconds_ago = now - node.last_seen if node.last_seen > 0 else None
            if seconds_ago is not None and seconds_ago < offline_threshold:
                online += 1
            else:
                offline += 1

        lines = [
            "**Kumatastic Status**",
            f"",
            f"Collector ID: {self._collector_id}",
            f"Total nodes: {len(nodes)}",
            f"Online: {online}",
            f"Offline: {offline}",
        ]

        if self._pusher:
            lines.append(f"")
            lines.append(f"Pusher: running in-process")
            lines.append(f"Targets: {len(self._pusher.config.targets)}")
        else:
            lines.append(f"")
            lines.append(f"Pusher: external (run `kumatastic push`)")

        return "\n".join(lines)
