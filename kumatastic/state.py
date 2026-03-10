"""State store abstraction for node sightings.

Provides an abstract interface for storing and retrieving node sighting data,
with implementations for JSON file storage (single-host) and Redis (multi-host).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NodeSighting:
    """A single node sighting record."""

    node_id: str
    last_seen: float
    source: str  # collector ID that reported this sighting
    name: str = ""
    snr: float | None = None
    hops: int | None = None
    battery: int | None = None
    voltage: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    # Metadata from NeighborInfo
    via_neighbor: bool = False
    observer_id: str | None = None


@dataclass
class NodeState:
    """Complete state for a tracked node."""

    node_id: str
    name: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    sighting_count: int = 0

    # Best known values from most recent sighting
    snr: float | None = None
    hops: int | None = None
    battery: int | None = None
    voltage: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None

    # Multi-collector: track which collectors have seen this node
    # Format: {collector_id: last_seen_timestamp}
    collectors: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeState:
        """Create from dictionary."""
        return cls(
            node_id=data.get("node_id", ""),
            name=data.get("name", ""),
            first_seen=data.get("first_seen", 0.0),
            last_seen=data.get("last_seen", 0.0),
            sighting_count=data.get("sighting_count", 0),
            snr=data.get("snr"),
            hops=data.get("hops"),
            battery=data.get("battery"),
            voltage=data.get("voltage"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            altitude=data.get("altitude"),
            collectors=data.get("collectors", {}),
        )


class StateStore(ABC):
    """Abstract base class for state storage backends."""

    @abstractmethod
    def get_node(self, node_id: str) -> NodeState | None:
        """Get state for a specific node.

        Args:
            node_id: Meshtastic node ID (e.g., "!abcd1234")

        Returns:
            NodeState if found, None otherwise.
        """
        ...

    @abstractmethod
    def set_node(self, node_id: str, state: NodeState) -> None:
        """Store state for a node.

        Args:
            node_id: Meshtastic node ID
            state: Node state to store
        """
        ...

    @abstractmethod
    def update_sighting(self, sighting: NodeSighting) -> NodeState:
        """Update node state with a new sighting.

        This is an atomic read-modify-write operation.

        Args:
            sighting: New sighting data

        Returns:
            Updated node state.
        """
        ...

    @abstractmethod
    def get_all_nodes(self) -> dict[str, NodeState]:
        """Get all tracked nodes.

        Returns:
            Dict mapping node_id to NodeState.
        """
        ...

    @abstractmethod
    def get_nodes_by_ids(self, node_ids: list[str]) -> dict[str, NodeState]:
        """Get nodes matching a list of IDs.

        Args:
            node_ids: List of node IDs to retrieve

        Returns:
            Dict mapping node_id to NodeState (only includes found nodes).
        """
        ...

    @abstractmethod
    def delete_node(self, node_id: str) -> bool:
        """Delete a node from the store.

        Args:
            node_id: Node ID to delete

        Returns:
            True if deleted, False if not found.
        """
        ...

    def get_nodes_seen_since(self, since: float) -> dict[str, NodeState]:
        """Get nodes seen since a timestamp.

        Args:
            since: Unix timestamp

        Returns:
            Dict of nodes with last_seen >= since.
        """
        all_nodes = self.get_all_nodes()
        return {
            node_id: state
            for node_id, state in all_nodes.items()
            if state.last_seen >= since
        }

    def get_nodes_by_collector(self, collector_id: str) -> dict[str, NodeState]:
        """Get nodes seen by a specific collector.

        Args:
            collector_id: Collector identifier

        Returns:
            Dict of nodes that have been seen by this collector.
        """
        all_nodes = self.get_all_nodes()
        return {
            node_id: state
            for node_id, state in all_nodes.items()
            if collector_id in state.collectors
        }


class JSONFileStore(StateStore):
    """JSON file-based state store.

    Thread-safe implementation using file locking for concurrent access.
    Suitable for single-host deployments.
    """

    def __init__(self, path: str | Path) -> None:
        """Initialize JSON file store.

        Args:
            path: Path to the JSON state file
        """
        self.path = Path(path)
        self._lock = threading.Lock()

        # Create parent directories if needed
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize empty file if it doesn't exist
        if not self.path.exists():
            self._write_state({})

    def _read_state(self) -> dict[str, dict[str, Any]]:
        """Read state from file."""
        try:
            with open(self.path) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write_state(self, state: dict[str, dict[str, Any]]) -> None:
        """Write state to file atomically.

        Uses a unique temp file per call to avoid races between processes
        sharing the same state file. Preserves the existing file's permissions
        and ownership so that multi-user access (e.g. root pusher + user
        mmrelay) isn't broken by the atomic replace.
        """
        # Capture existing file's stat before replacing
        try:
            existing_stat = self.path.stat()
        except FileNotFoundError:
            existing_stat = None

        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=".state-", suffix=".tmp"
        )
        try:
            # Preserve permissions and ownership from the existing file
            if existing_stat is not None:
                os.fchmod(fd, existing_stat.st_mode & 0o7777)
                try:
                    os.fchown(fd, existing_stat.st_uid, existing_stat.st_gid)
                except PermissionError:
                    pass  # non-root can't chown, that's OK
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, self.path)
        except BaseException:
            # Clean up temp file on any failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_node(self, node_id: str) -> NodeState | None:
        """Get state for a specific node."""
        with self._lock:
            state = self._read_state()
            if node_id in state:
                return NodeState.from_dict(state[node_id])
            return None

    def set_node(self, node_id: str, node_state: NodeState) -> None:
        """Store state for a node."""
        with self._lock:
            state = self._read_state()
            state[node_id] = node_state.to_dict()
            self._write_state(state)

    def update_sighting(self, sighting: NodeSighting) -> NodeState:
        """Update node state with a new sighting."""
        with self._lock:
            state = self._read_state()
            now = sighting.last_seen

            if sighting.node_id in state:
                node = NodeState.from_dict(state[sighting.node_id])
                node.sighting_count += 1
            else:
                node = NodeState(
                    node_id=sighting.node_id,
                    first_seen=now,
                    sighting_count=1,
                )

            # Update with latest sighting data
            node.last_seen = now
            if sighting.name:
                node.name = sighting.name
            if sighting.snr is not None:
                node.snr = sighting.snr
            if sighting.hops is not None:
                node.hops = sighting.hops
            if sighting.battery is not None:
                node.battery = sighting.battery
            if sighting.voltage is not None:
                node.voltage = sighting.voltage
            if sighting.latitude is not None:
                node.latitude = sighting.latitude
            if sighting.longitude is not None:
                node.longitude = sighting.longitude
            if sighting.altitude is not None:
                node.altitude = sighting.altitude

            # Track collector
            node.collectors[sighting.source] = now

            state[sighting.node_id] = node.to_dict()
            self._write_state(state)
            return node

    def get_all_nodes(self) -> dict[str, NodeState]:
        """Get all tracked nodes."""
        with self._lock:
            state = self._read_state()
            return {
                node_id: NodeState.from_dict(data)
                for node_id, data in state.items()
            }

    def get_nodes_by_ids(self, node_ids: list[str]) -> dict[str, NodeState]:
        """Get nodes matching a list of IDs."""
        with self._lock:
            state = self._read_state()
            result = {}
            for node_id in node_ids:
                if node_id in state:
                    result[node_id] = NodeState.from_dict(state[node_id])
            return result

    def delete_node(self, node_id: str) -> bool:
        """Delete a node from the store."""
        with self._lock:
            state = self._read_state()
            if node_id in state:
                del state[node_id]
                self._write_state(state)
                return True
            return False


class MemoryStore(StateStore):
    """In-memory state store for testing.

    Not persistent - data is lost when the process exits.
    """

    def __init__(self) -> None:
        self._state: dict[str, NodeState] = {}
        self._lock = threading.Lock()

    def get_node(self, node_id: str) -> NodeState | None:
        with self._lock:
            return self._state.get(node_id)

    def set_node(self, node_id: str, state: NodeState) -> None:
        with self._lock:
            self._state[node_id] = state

    def update_sighting(self, sighting: NodeSighting) -> NodeState:
        with self._lock:
            now = sighting.last_seen

            if sighting.node_id in self._state:
                node = self._state[sighting.node_id]
                node.sighting_count += 1
            else:
                node = NodeState(
                    node_id=sighting.node_id,
                    first_seen=now,
                    sighting_count=1,
                )

            # Update with latest sighting data
            node.last_seen = now
            if sighting.name:
                node.name = sighting.name
            if sighting.snr is not None:
                node.snr = sighting.snr
            if sighting.hops is not None:
                node.hops = sighting.hops
            if sighting.battery is not None:
                node.battery = sighting.battery
            if sighting.voltage is not None:
                node.voltage = sighting.voltage
            if sighting.latitude is not None:
                node.latitude = sighting.latitude
            if sighting.longitude is not None:
                node.longitude = sighting.longitude
            if sighting.altitude is not None:
                node.altitude = sighting.altitude

            node.collectors[sighting.source] = now
            self._state[sighting.node_id] = node
            return node

    def get_all_nodes(self) -> dict[str, NodeState]:
        with self._lock:
            return dict(self._state)

    def get_nodes_by_ids(self, node_ids: list[str]) -> dict[str, NodeState]:
        with self._lock:
            return {
                node_id: self._state[node_id]
                for node_id in node_ids
                if node_id in self._state
            }

    def delete_node(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self._state:
                del self._state[node_id]
                return True
            return False

    def clear(self) -> None:
        """Clear all state (for testing)."""
        with self._lock:
            self._state.clear()


def create_store(store_type: str, **kwargs: Any) -> StateStore:
    """Factory function to create a state store.

    Args:
        store_type: Type of store ("json", "memory", "redis")
        **kwargs: Store-specific arguments

    Returns:
        StateStore instance.

    Raises:
        ValueError: If store_type is unknown or required args missing.
    """
    if store_type == "json":
        if "path" not in kwargs:
            raise ValueError("JSON store requires 'path' argument")
        return JSONFileStore(kwargs["path"])

    if store_type == "memory":
        return MemoryStore()

    if store_type == "redis":
        raise NotImplementedError("Redis store not yet implemented")

    raise ValueError(f"Unknown store type: {store_type}")
