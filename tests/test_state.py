"""Tests for the state store module."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

import pytest

from kumatastic.state import (
    JSONFileStore,
    MemoryStore,
    NodeSighting,
    NodeState,
    create_store,
)


class TestNodeState:
    """Tests for NodeState dataclass."""

    def test_to_dict(self) -> None:
        """Test converting NodeState to dict."""
        state = NodeState(
            node_id="!abcd1234",
            name="Test Node",
            first_seen=1000.0,
            last_seen=2000.0,
            sighting_count=5,
            snr=-5.5,
            battery=85,
        )
        d = state.to_dict()
        assert d["node_id"] == "!abcd1234"
        assert d["name"] == "Test Node"
        assert d["first_seen"] == 1000.0
        assert d["last_seen"] == 2000.0
        assert d["sighting_count"] == 5
        assert d["snr"] == -5.5
        assert d["battery"] == 85

    def test_from_dict(self) -> None:
        """Test creating NodeState from dict."""
        d = {
            "node_id": "!abcd1234",
            "name": "Test Node",
            "first_seen": 1000.0,
            "last_seen": 2000.0,
            "sighting_count": 5,
            "collectors": {"collector-1": 2000.0},
        }
        state = NodeState.from_dict(d)
        assert state.node_id == "!abcd1234"
        assert state.name == "Test Node"
        assert state.first_seen == 1000.0
        assert state.last_seen == 2000.0
        assert state.sighting_count == 5
        assert state.collectors == {"collector-1": 2000.0}

    def test_from_dict_defaults(self) -> None:
        """Test creating NodeState from dict with missing fields."""
        d = {"node_id": "!abcd1234"}
        state = NodeState.from_dict(d)
        assert state.node_id == "!abcd1234"
        assert state.name == ""
        assert state.first_seen == 0.0
        assert state.sighting_count == 0
        assert state.collectors == {}


class TestMemoryStore:
    """Tests for in-memory state store."""

    def test_get_node_not_found(self) -> None:
        """Test getting a non-existent node returns None."""
        store = MemoryStore()
        assert store.get_node("!abcd1234") is None

    def test_set_and_get_node(self) -> None:
        """Test storing and retrieving a node."""
        store = MemoryStore()
        state = NodeState(node_id="!abcd1234", name="Test Node")
        store.set_node("!abcd1234", state)

        retrieved = store.get_node("!abcd1234")
        assert retrieved is not None
        assert retrieved.node_id == "!abcd1234"
        assert retrieved.name == "Test Node"

    def test_update_sighting_new_node(self) -> None:
        """Test updating sighting for a new node."""
        store = MemoryStore()
        now = time.time()

        sighting = NodeSighting(
            node_id="!abcd1234",
            last_seen=now,
            source="collector-1",
            name="Test Node",
            snr=-5.5,
        )
        result = store.update_sighting(sighting)

        assert result.node_id == "!abcd1234"
        assert result.name == "Test Node"
        assert result.first_seen == now
        assert result.last_seen == now
        assert result.sighting_count == 1
        assert result.snr == -5.5
        assert result.collectors == {"collector-1": now}

    def test_update_sighting_existing_node(self) -> None:
        """Test updating sighting for an existing node."""
        store = MemoryStore()
        first_time = time.time() - 3600  # 1 hour ago

        # First sighting
        sighting1 = NodeSighting(
            node_id="!abcd1234",
            last_seen=first_time,
            source="collector-1",
            name="Test Node",
        )
        store.update_sighting(sighting1)

        # Second sighting
        second_time = time.time()
        sighting2 = NodeSighting(
            node_id="!abcd1234",
            last_seen=second_time,
            source="collector-2",
            name="Updated Name",
            battery=75,
        )
        result = store.update_sighting(sighting2)

        assert result.node_id == "!abcd1234"
        assert result.name == "Updated Name"
        assert result.first_seen == first_time  # Should not change
        assert result.last_seen == second_time
        assert result.sighting_count == 2
        assert result.battery == 75
        assert "collector-1" in result.collectors
        assert "collector-2" in result.collectors

    def test_get_all_nodes(self) -> None:
        """Test getting all nodes."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now, "c1"))
        store.update_sighting(NodeSighting("!node2", now, "c1"))
        store.update_sighting(NodeSighting("!node3", now, "c1"))

        all_nodes = store.get_all_nodes()
        assert len(all_nodes) == 3
        assert "!node1" in all_nodes
        assert "!node2" in all_nodes
        assert "!node3" in all_nodes

    def test_get_nodes_by_ids(self) -> None:
        """Test getting nodes by ID list."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now, "c1"))
        store.update_sighting(NodeSighting("!node2", now, "c1"))
        store.update_sighting(NodeSighting("!node3", now, "c1"))

        subset = store.get_nodes_by_ids(["!node1", "!node3", "!nonexistent"])
        assert len(subset) == 2
        assert "!node1" in subset
        assert "!node3" in subset
        assert "!nonexistent" not in subset

    def test_delete_node(self) -> None:
        """Test deleting a node."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now, "c1"))
        assert store.get_node("!node1") is not None

        assert store.delete_node("!node1") is True
        assert store.get_node("!node1") is None

        # Deleting non-existent node returns False
        assert store.delete_node("!node1") is False

    def test_get_nodes_seen_since(self) -> None:
        """Test filtering nodes by last seen time."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!old", now - 7200, "c1"))  # 2 hours ago
        store.update_sighting(NodeSighting("!recent", now - 1800, "c1"))  # 30 min ago
        store.update_sighting(NodeSighting("!new", now, "c1"))  # now

        # Nodes seen in last hour
        recent = store.get_nodes_seen_since(now - 3600)
        assert len(recent) == 2
        assert "!recent" in recent
        assert "!new" in recent
        assert "!old" not in recent

    def test_get_nodes_by_collector(self) -> None:
        """Test filtering nodes by collector."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now, "collector-a"))
        store.update_sighting(NodeSighting("!node2", now, "collector-b"))
        store.update_sighting(NodeSighting("!node3", now, "collector-a"))

        a_nodes = store.get_nodes_by_collector("collector-a")
        assert len(a_nodes) == 2
        assert "!node1" in a_nodes
        assert "!node3" in a_nodes

        b_nodes = store.get_nodes_by_collector("collector-b")
        assert len(b_nodes) == 1
        assert "!node2" in b_nodes

    def test_clear(self) -> None:
        """Test clearing all state."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now, "c1"))
        store.update_sighting(NodeSighting("!node2", now, "c1"))
        assert len(store.get_all_nodes()) == 2

        store.clear()
        assert len(store.get_all_nodes()) == 0


class TestJSONFileStore:
    """Tests for JSON file-based state store."""

    def test_creates_file(self) -> None:
        """Test that store creates the file if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            assert not path.exists()

            store = JSONFileStore(path)
            assert path.exists()

            # Should be valid JSON
            with open(path) as f:
                data = json.load(f)
            assert data == {}

    def test_creates_parent_dirs(self) -> None:
        """Test that store creates parent directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "dir" / "state.json"
            assert not path.exists()

            store = JSONFileStore(path)
            assert path.exists()

    def test_set_and_get_node(self) -> None:
        """Test storing and retrieving a node."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = JSONFileStore(path)

            state = NodeState(node_id="!abcd1234", name="Test Node", battery=85)
            store.set_node("!abcd1234", state)

            retrieved = store.get_node("!abcd1234")
            assert retrieved is not None
            assert retrieved.node_id == "!abcd1234"
            assert retrieved.name == "Test Node"
            assert retrieved.battery == 85

    def test_persistence(self) -> None:
        """Test that data persists across store instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"

            # Write with first store
            store1 = JSONFileStore(path)
            now = time.time()
            store1.update_sighting(NodeSighting("!node1", now, "c1", name="Persistent"))

            # Read with second store
            store2 = JSONFileStore(path)
            node = store2.get_node("!node1")
            assert node is not None
            assert node.name == "Persistent"

    def test_preserves_file_permissions(self) -> None:
        """Test that writing state preserves existing file permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = JSONFileStore(path)

            # Set permissions to 664 (rw-rw-r--)
            os.chmod(path, 0o664)
            assert stat.S_IMODE(path.stat().st_mode) == 0o664

            # Write should preserve the 664 permissions
            now = time.time()
            store.update_sighting(NodeSighting("!node1", now, "c1", name="Test"))
            assert stat.S_IMODE(path.stat().st_mode) == 0o664

    def test_new_file_gets_default_permissions(self) -> None:
        """Test that a brand-new state file gets mkstemp default permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = JSONFileStore(path)

            # New file created by __init__ - should exist
            assert path.exists()

    def test_update_sighting(self) -> None:
        """Test updating sighting through JSON store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = JSONFileStore(path)
            now = time.time()

            sighting = NodeSighting(
                node_id="!abcd1234",
                last_seen=now,
                source="collector-1",
                name="Test",
                latitude=37.7749,
                longitude=-122.4194,
            )
            result = store.update_sighting(sighting)

            assert result.node_id == "!abcd1234"
            assert result.latitude == 37.7749
            assert result.longitude == -122.4194

            # Verify persistence
            node = store.get_node("!abcd1234")
            assert node is not None
            assert node.latitude == 37.7749


class TestCreateStore:
    """Tests for the create_store factory function."""

    def test_create_memory_store(self) -> None:
        """Test creating a memory store."""
        store = create_store("memory")
        assert isinstance(store, MemoryStore)

    def test_create_json_store(self) -> None:
        """Test creating a JSON store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = create_store("json", path=str(path))
            assert isinstance(store, JSONFileStore)

    def test_json_store_requires_path(self) -> None:
        """Test that JSON store requires a path argument."""
        with pytest.raises(ValueError, match="path"):
            create_store("json")

    def test_unknown_store_type(self) -> None:
        """Test that unknown store type raises error."""
        with pytest.raises(ValueError, match="Unknown store type"):
            create_store("postgres")

    def test_redis_not_implemented(self) -> None:
        """Test that Redis store is not yet implemented."""
        with pytest.raises(NotImplementedError):
            create_store("redis")
