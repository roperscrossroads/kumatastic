"""Meshtastic collector - listens to mesh and writes sightings to state store.

The collector connects to a Meshtastic device (via TCP or serial), listens for
node activity, and writes sightings to a state store. It has no knowledge of
Uptime Kuma - it only collects and stores node sighting data.

Sighting sources:
1. NodeDB updates - when the device's node database is updated
2. NeighborInfo packets - mesh-wide visibility of neighbors
3. Position packets - explicit GPS broadcasts
4. Telemetry packets - device metrics
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable

import requests

from .config import CollectorConfig
from .manifest import Manifest, ReloadableManifest, create_manifest, load_manifest
from .state import NodeSighting, StateStore

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)


class _ConnectionLost(Exception):
    """Raised when the Meshtastic connection is detected as lost."""


class MeshCollector:
    """Collects node sightings from a Meshtastic device."""

    def __init__(
        self,
        config: CollectorConfig,
        state_store: StateStore,
        manifest: Manifest | ReloadableManifest | None = None,
    ) -> None:
        """Initialize the collector.

        Args:
            config: Collector configuration
            state_store: State store for persisting sightings
            manifest: Node manifest (loaded from config.manifest_path if not provided)
        """
        self.config = config
        self.state = state_store
        self.collector_id = config.id

        if manifest is not None:
            self._manifest = manifest
        else:
            self._manifest = create_manifest(config.manifest_path)

        self._interface: MeshInterface | None = None
        self._running = False
        self._stop_event = threading.Event()
        self._pubsub_listener: Callable | None = None

        # Track neighbor info for pruning stale entries
        # Format: {target_node_id: {observer_id: report_time}}
        self._neighbor_times: dict[str, dict[str, float]] = {}
        self._neighbor_lock = threading.Lock()

        # HTTP push pool for forwarding sightings to remote pushers
        self._pusher_urls = config.pusher_urls
        self._sighting_token = config.sighting_token
        if self._pusher_urls:
            self._push_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sighting-push")
        else:
            self._push_pool = None

    def _forward_sighting(self, sighting: NodeSighting) -> None:
        """Forward a sighting to configured pusher URLs (fire-and-forget)."""
        if not self._push_pool or not self._pusher_urls:
            return

        payload = {
            "node_id": sighting.node_id,
            "last_seen": sighting.last_seen,
            "source": sighting.source,
        }
        # Include optional fields only if set
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
            logger.warning(f"Failed to forward sighting to {base_url}: {e}")

    def _connect(self) -> MeshInterface | None:
        """Connect to the Meshtastic device.

        Returns:
            MeshInterface if connected, None otherwise.
        """
        try:
            from meshtastic import tcp_interface, serial_interface

            connection = self.config.meshtastic

            if connection.startswith("tcp:"):
                # Format: tcp:host:port or tcp:host
                parts = connection[4:].split(":")
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 4403
                logger.info(f"Connecting to Meshtastic via TCP: {host}:{port}")
                return tcp_interface.TCPInterface(hostname=host, portNumber=port)

            elif connection.startswith("serial:"):
                # Format: serial:/dev/ttyUSB0
                device = connection[7:]
                logger.info(f"Connecting to Meshtastic via serial: {device}")
                return serial_interface.SerialInterface(device)

            else:
                # Assume serial device path
                logger.info(f"Connecting to Meshtastic via serial: {connection}")
                return serial_interface.SerialInterface(connection)

        except Exception as e:
            logger.error(f"Failed to connect to Meshtastic: {e}")
            return None

    def _on_receive(self, packet: dict[str, Any], interface: Any) -> None:
        """Handle received packet from mesh.

        Args:
            packet: The received packet
            interface: The mesh interface (unused)
        """
        _ = interface

        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum", "")
        from_id = packet.get("fromId")
        now = time.time()

        if not from_id:
            return

        # Normalize node ID
        if not from_id.startswith("!"):
            from_id = f"!{from_id}"

        # Handle NeighborInfo packets for multi-hop visibility
        if portnum == "NEIGHBORINFO_APP":
            self._handle_neighbor_info(packet, now)
            return

        # Skip nodes not in manifest
        if not self._manifest.contains(from_id):
            return

        # Handle position packets
        if portnum == "POSITION_APP":
            position = decoded.get("position", {})
            if position:
                sighting = NodeSighting(
                    node_id=from_id,
                    last_seen=now,
                    source=self.collector_id,
                    latitude=position.get("latitude"),
                    longitude=position.get("longitude"),
                    altitude=position.get("altitude"),
                )
                self.state.update_sighting(sighting)
                self._forward_sighting(sighting)
                logger.debug(f"Position sighting: {from_id}")
            return

        # Handle telemetry packets
        if portnum == "TELEMETRY_APP":
            telemetry = decoded.get("telemetry", {})
            device_metrics = telemetry.get("deviceMetrics", {})
            if device_metrics:
                sighting = NodeSighting(
                    node_id=from_id,
                    last_seen=now,
                    source=self.collector_id,
                    battery=device_metrics.get("batteryLevel"),
                    voltage=device_metrics.get("voltage"),
                )
                self.state.update_sighting(sighting)
                self._forward_sighting(sighting)
                logger.debug(f"Telemetry sighting: {from_id}")
            return

        # Handle any other packet as a basic sighting
        # This catches TEXT_MESSAGE_APP, ADMIN_APP, etc.
        sighting = NodeSighting(
            node_id=from_id,
            last_seen=now,
            source=self.collector_id,
        )
        self.state.update_sighting(sighting)
        self._forward_sighting(sighting)
        logger.debug(f"Packet sighting: {from_id} ({portnum})")

    def _handle_neighbor_info(self, packet: dict[str, Any], now: float) -> None:
        """Handle NeighborInfo packet.

        NeighborInfo packets report which nodes an observer can see. This gives
        us multi-hop visibility - even if we can't hear a node directly, we
        know it's alive if another node reports seeing it.

        Args:
            packet: The NeighborInfo packet
            now: Current timestamp
        """
        decoded = packet.get("decoded", {})
        neighbor_info = decoded.get("neighborinfo", {})
        observer_id = packet.get("fromId")

        if not observer_id or not neighbor_info.get("neighbors"):
            return

        if not observer_id.startswith("!"):
            observer_id = f"!{observer_id}"

        logger.debug(
            f"NeighborInfo from {observer_id}: "
            f"{len(neighbor_info['neighbors'])} neighbors"
        )

        with self._neighbor_lock:
            for neighbor in neighbor_info.get("neighbors", []):
                # NeighborInfo uses numeric node_id
                raw_node_id = neighbor.get("node_id")
                if not raw_node_id:
                    continue

                # Convert to hex format (!abcd1234)
                target_id = f"!{raw_node_id:08x}"

                # Skip neighbors not in manifest
                if not self._manifest.contains(target_id):
                    continue

                snr = neighbor.get("snr")

                # Create sighting for the neighbor
                sighting = NodeSighting(
                    node_id=target_id,
                    last_seen=now,
                    source=self.collector_id,
                    snr=snr,
                    via_neighbor=True,
                    observer_id=observer_id,
                )
                self.state.update_sighting(sighting)
                self._forward_sighting(sighting)

                # Track for staleness pruning
                if target_id not in self._neighbor_times:
                    self._neighbor_times[target_id] = {}
                self._neighbor_times[target_id][observer_id] = now

    def _on_node_update(self, node: dict[str, Any]) -> None:
        """Handle node database update.

        Called when a node's info is updated in the local database.

        Args:
            node: The updated node info
        """
        node_id = node.get("num")
        if node_id is None:
            return

        # Convert numeric ID to hex format
        if isinstance(node_id, int):
            node_id = f"!{node_id:08x}"
        elif not node_id.startswith("!"):
            node_id = f"!{node_id}"

        # Skip nodes not in manifest
        if not self._manifest.contains(node_id):
            return

        now = time.time()
        user = node.get("user", {})
        name = user.get("longName") or user.get("shortName") or ""

        device_metrics = node.get("deviceMetrics", {})
        position = node.get("position", {})

        sighting = NodeSighting(
            node_id=node_id,
            last_seen=now,
            source=self.collector_id,
            name=name,
            snr=node.get("snr"),
            hops=node.get("hopsAway"),
            battery=device_metrics.get("batteryLevel"),
            voltage=device_metrics.get("voltage"),
            latitude=position.get("latitude"),
            longitude=position.get("longitude"),
            altitude=position.get("altitude"),
        )
        self.state.update_sighting(sighting)
        self._forward_sighting(sighting)
        logger.debug(f"NodeDB sighting: {node_id} ({name})")

    def _scan_node_db(self) -> None:
        """Scan the current node database and record sightings."""
        if not self._interface:
            return

        nodes = getattr(self._interface, "nodes", {})
        if not nodes:
            return

        logger.info(f"Scanning nodeDB: {len(nodes)} nodes")
        now = time.time()

        for node_id, node_info in nodes.items():
            # Normalize node ID
            if not node_id.startswith("!"):
                node_id = f"!{node_id}"

            # Skip nodes not in manifest
            if not self._manifest.contains(node_id):
                continue

            user = node_info.get("user", {})
            name = user.get("longName") or user.get("shortName") or ""

            # Use lastHeard as the sighting time if available
            last_heard = node_info.get("lastHeard", 0)
            sighting_time = last_heard if last_heard > 0 else now

            device_metrics = node_info.get("deviceMetrics", {})
            position = node_info.get("position", {})

            sighting = NodeSighting(
                node_id=node_id,
                last_seen=sighting_time,
                source=self.collector_id,
                name=name,
                snr=node_info.get("snr"),
                hops=node_info.get("hopsAway"),
                battery=device_metrics.get("batteryLevel"),
                voltage=device_metrics.get("voltage"),
                latitude=position.get("latitude"),
                longitude=position.get("longitude"),
                altitude=position.get("altitude"),
            )
            self.state.update_sighting(sighting)
            self._forward_sighting(sighting)

    def _prune_stale_neighbors(self) -> None:
        """Remove stale neighbor sightings."""
        max_age = self.config.neighbor_max_age
        cutoff = time.time() - max_age

        with self._neighbor_lock:
            for target_id in list(self._neighbor_times.keys()):
                observers = self._neighbor_times[target_id]
                for observer_id in list(observers.keys()):
                    if observers[observer_id] < cutoff:
                        del observers[observer_id]
                if not observers:
                    del self._neighbor_times[target_id]

    def _register_callbacks(self) -> None:
        """Subscribe to meshtastic.receive via pubsub (or interface callback fallback)."""
        try:
            from pubsub import pub

            pub.subscribe(self._on_receive, "meshtastic.receive")
            self._pubsub_listener = self._on_receive
            logger.debug("Subscribed to meshtastic.receive")
        except ImportError:
            logger.warning("pubsub not available, using interface callbacks")
            if self._interface and hasattr(self._interface, "onReceive"):
                self._interface.onReceive = self._on_receive

    def _disconnect(self) -> None:
        """Close the interface and unsubscribe pubsub listener.

        Does NOT set _running = False — used between reconnections.
        """
        if self._pubsub_listener:
            try:
                from pubsub import pub

                pub.unsubscribe(self._pubsub_listener, "meshtastic.receive")
            except Exception:
                pass
            self._pubsub_listener = None

        if self._interface:
            try:
                self._interface.close()
            except Exception as e:
                logger.warning(f"Error closing interface: {e}")
            self._interface = None

    def _run_loop(self) -> None:
        """Inner loop: run while connected, checking health periodically.

        Raises _ConnectionLost if the interface disconnects.
        """
        prune_interval = 3600  # Prune stale neighbors every hour
        last_prune = time.time()
        health_check_interval = 30  # Check connection every 30s
        last_health_check = time.time()

        while self._running and not self._stop_event.is_set():
            now = time.time()

            # Periodic neighbor pruning
            if now - last_prune > prune_interval:
                self._prune_stale_neighbors()
                last_prune = now

            # Periodic connection health check
            if now - last_health_check > health_check_interval:
                if self._interface is None:
                    raise _ConnectionLost("Interface is None")
                is_connected = getattr(self._interface, "isConnected", None)
                if is_connected is not None and not is_connected.is_set():
                    raise _ConnectionLost("isConnected event cleared")
                last_health_check = now

            # Sleep in small increments to allow responsive shutdown
            self._stop_event.wait(timeout=1.0)

    def run(self) -> None:
        """Run the collector (blocking).

        Connects to the Meshtastic device and processes events until stopped.
        Automatically reconnects with exponential backoff on connection loss.
        """
        logger.info(f"Starting collector: {self.collector_id}")
        logger.info(f"Meshtastic connection: {self.config.meshtastic}")
        logger.info(f"State path: {self.config.state_path}")

        self._running = True
        self._stop_event.clear()
        backoff = 5  # start at 5s

        try:
            while self._running and not self._stop_event.is_set():
                # Connect
                self._interface = self._connect()
                if not self._interface:
                    logger.warning(f"Connection failed, retrying in {backoff}s...")
                    self._stop_event.wait(timeout=backoff)
                    backoff = min(backoff * 2, 300)  # cap at 5 minutes
                    continue

                logger.info("Connected to Meshtastic device")
                backoff = 5  # reset on successful connect
                self._register_callbacks()
                self._scan_node_db()

                # Inner loop: run while connected
                try:
                    self._run_loop()
                except _ConnectionLost:
                    logger.warning("Connection lost, will reconnect...")
                finally:
                    self._disconnect()

        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """Final cleanup on exit."""
        self._running = False
        self._stop_event.set()
        self._disconnect()

        if self._push_pool:
            self._push_pool.shutdown(wait=False)

        logger.info("Collector stopped")

    def stop(self) -> None:
        """Stop the collector."""
        logger.info("Stopping collector...")
        self._running = False
        self._stop_event.set()


def run_collector(
    config: CollectorConfig,
    state_store: StateStore,
    handle_signals: bool = True,
    manifest: Manifest | ReloadableManifest | None = None,
) -> None:
    """Run a collector with signal handling.

    Args:
        config: Collector configuration
        state_store: State store to write to
        handle_signals: Whether to set up signal handlers
        manifest: Node manifest (loaded from config.manifest_path if not provided)
    """
    collector = MeshCollector(config, state_store, manifest=manifest)

    if handle_signals:
        def signal_handler(signum: int, frame: Any) -> None:
            logger.info(f"Received signal {signum}, stopping...")
            collector.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    collector.run()
