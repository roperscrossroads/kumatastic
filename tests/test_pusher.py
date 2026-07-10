"""Tests for the pusher module."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from kumatastic.config import KumaTarget, PusherConfig
from kumatastic.manifest import Manifest, ManifestNode
from kumatastic.cli import _sync_target
from kumatastic.pusher import KumaConnection, KumaPusher, MonitorInfo, PushResult, start_sighting_server
from kumatastic.state import MemoryStore, NodeSighting, NodeState


def make_manifest(*node_ids: str, names: dict[str, str] | None = None) -> Manifest:
    """Create a manifest with the given node IDs."""
    names = names or {}
    nodes = {}
    for nid in node_ids:
        normalized = nid if nid.startswith("!") else f"!{nid}"
        normalized = normalized.lower()
        name = names.get(nid, f"Node {normalized}")
        nodes[normalized] = ManifestNode(node_id=normalized, name=name)
    return Manifest(nodes=nodes)


class TestKumaPusher:
    """Tests for KumaPusher status computation."""

    def make_pusher(self, offline_threshold: int = 23400) -> KumaPusher:
        """Create a pusher with a memory store."""
        store = MemoryStore()
        config = PusherConfig(
            offline_threshold=offline_threshold,
            push_interval=600,
            targets=[],
        )
        manifest = make_manifest("!abc")
        return KumaPusher(store, config, manifest=manifest)

    def test_compute_status_online(self) -> None:
        """Test status computation for an online node."""
        pusher = self.make_pusher(offline_threshold=3600)  # 1 hour
        now = time.time()

        node = NodeState(
            node_id="!abc",
            last_seen=now - 1800,  # 30 min ago
            battery=85,
            snr=-5.5,
        )

        status, message = pusher._compute_status(node)

        assert status == "up"
        assert "30m ago" in message
        assert "Bat: 85%" in message
        assert "SNR: -5.5dB" in message

    def test_compute_status_offline(self) -> None:
        """Test status computation for an offline node."""
        pusher = self.make_pusher(offline_threshold=3600)  # 1 hour
        now = time.time()

        node = NodeState(
            node_id="!abc",
            last_seen=now - 7200,  # 2 hours ago
        )

        status, message = pusher._compute_status(node)

        assert status == "down"
        assert "2h ago" in message

    def test_compute_status_never_seen(self) -> None:
        """Test status computation for a node never seen."""
        pusher = self.make_pusher()

        node = NodeState(node_id="!abc", last_seen=0)

        status, message = pusher._compute_status(node)

        assert status == "down"
        assert "Never seen" in message

    def test_compute_status_time_formats(self) -> None:
        """Test various time format outputs."""
        pusher = self.make_pusher(offline_threshold=999999)  # Very high
        now = time.time()

        # Seconds
        node = NodeState(node_id="!a", last_seen=now - 45)
        status, msg = pusher._compute_status(node)
        assert "45s ago" in msg

        # Minutes
        node = NodeState(node_id="!b", last_seen=now - 300)
        status, msg = pusher._compute_status(node)
        assert "5m ago" in msg

        # Hours
        node = NodeState(node_id="!c", last_seen=now - 10800)
        status, msg = pusher._compute_status(node)
        assert "3h ago" in msg

        # Days
        node = NodeState(node_id="!d", last_seen=now - 172800)
        status, msg = pusher._compute_status(node)
        assert "2d ago" in msg

    def test_compute_status_hops_format(self) -> None:
        """Test hops display in status message."""
        pusher = self.make_pusher(offline_threshold=999999)
        now = time.time()

        # Direct
        node = NodeState(node_id="!a", last_seen=now, hops=0)
        _, msg = pusher._compute_status(node)
        assert "Direct" in msg

        # With hops
        node = NodeState(node_id="!b", last_seen=now, hops=3)
        _, msg = pusher._compute_status(node)
        assert "3 hops" in msg


class TestGetNodes:
    """Tests for manifest-based node retrieval."""

    def make_pusher_with_nodes(self) -> tuple[KumaPusher, MemoryStore]:
        """Create a pusher with some nodes in the store."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now, "c1", name="Node 1"))
        store.update_sighting(NodeSighting("!node2", now, "c1", name="Node 2"))
        store.update_sighting(NodeSighting("!node3", now, "c1", name="Node 3"))

        manifest = make_manifest("!node1", "!node2", "!node4")
        config = PusherConfig(targets=[])
        pusher = KumaPusher(store, config, manifest=manifest)
        return pusher, store

    def test_get_nodes_returns_manifest_nodes(self) -> None:
        """Test that _get_nodes returns only manifest nodes."""
        pusher, _ = self.make_pusher_with_nodes()
        nodes = pusher._get_nodes()

        assert "!node1" in nodes
        assert "!node2" in nodes
        assert "!node3" not in nodes  # not in manifest

    def test_get_nodes_includes_unseen(self) -> None:
        """Test that _get_nodes includes manifest nodes not yet in state."""
        pusher, _ = self.make_pusher_with_nodes()
        nodes = pusher._get_nodes()

        # node4 is in manifest but not in state
        assert "!node4" in nodes
        assert nodes["!node4"].last_seen == 0  # never seen

    def test_get_nodes_preserves_state_data(self) -> None:
        """Test that _get_nodes returns actual state data for seen nodes."""
        pusher, _ = self.make_pusher_with_nodes()
        nodes = pusher._get_nodes()

        assert nodes["!node1"].name == "Node 1"
        assert nodes["!node1"].sighting_count == 1


class TestPushResult:
    """Tests for PushResult dataclass."""

    def test_empty_result(self) -> None:
        """Test empty push result."""
        result = PushResult()
        assert result.up == []
        assert result.down == []
        assert result.unknown == []
        assert result.push_failed == []
        assert result.monitors_created == []

    def test_result_with_data(self) -> None:
        """Test push result with data."""
        result = PushResult(
            up=["Node 1", "Node 2"],
            down=["Node 3"],
            monitors_created=["Node 1"],
        )
        assert len(result.up) == 2
        assert len(result.down) == 1
        assert len(result.monitors_created) == 1


class TestDistributedPush:
    """Tests for distributed mode (push_secret configured)."""

    def test_distributed_only_pushes_up(self) -> None:
        """In distributed mode, only UP nodes are pushed, DOWN are skipped."""
        store = MemoryStore()
        now = time.time()

        # One online, one offline
        store.update_sighting(NodeSighting("!node1", now - 100, "c1", name="Online"))
        store.update_sighting(NodeSighting("!node2", now - 99999, "c1", name="Offline"))

        config = PusherConfig(
            offline_threshold=3600,
            push_interval=600,
            push_secret="test-secret",
            targets=[KumaTarget(name="t", url="http://kuma:3001")],
        )
        manifest = make_manifest("!node1", "!node2")
        pusher = KumaPusher(store, config, manifest=manifest)

        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            results = pusher.push_cycle()

        result = results["t"]
        assert "Node !node1" in result.up or "Online" in result.up  # online pushed
        assert len(result.down) == 1  # offline tracked but not pushed
        assert result.push_failed == []
        # Only 1 HTTP call (for the UP node)
        assert mock_get.call_count == 1

    def test_distributed_uses_derived_token(self) -> None:
        """In distributed mode, push uses deterministic token from secret."""
        from kumatastic.manifest import derive_push_token

        store = MemoryStore()
        now = time.time()
        store.update_sighting(NodeSighting("!abcd1234", now, "c1", name="TestNode"))

        config = PusherConfig(
            offline_threshold=3600,
            push_interval=600,
            push_secret="my-secret",
            targets=[KumaTarget(name="t", url="http://kuma:3001")],
        )
        manifest = make_manifest("!abcd1234")
        pusher = KumaPusher(store, config, manifest=manifest)

        expected_token = derive_push_token("my-secret", "!abcd1234")

        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            pusher.push_cycle()

        # Verify the URL contains the derived token
        call_url = mock_get.call_args[0][0]
        assert expected_token in call_url

    def test_distributed_never_seen_not_pushed(self) -> None:
        """Nodes never seen are DOWN and not pushed in distributed mode."""
        store = MemoryStore()

        config = PusherConfig(
            offline_threshold=3600,
            push_secret="secret",
            targets=[KumaTarget(name="t", url="http://kuma:3001")],
        )
        manifest = make_manifest("!node1")
        pusher = KumaPusher(store, config, manifest=manifest)

        with patch("requests.get") as mock_get:
            results = pusher.push_cycle()

        assert mock_get.call_count == 0
        assert len(results["t"].down) == 1

    def test_single_instance_mode_pushes_both(self) -> None:
        """Without push_secret, pusher still pushes both UP and DOWN."""
        store = MemoryStore()
        now = time.time()

        store.update_sighting(NodeSighting("!node1", now - 100, "c1", name="Online"))

        config = PusherConfig(
            offline_threshold=3600,
            push_interval=600,
            push_secret="",  # single-instance mode
            targets=[KumaTarget(name="t", url="http://kuma:3001")],
        )
        manifest = make_manifest("!node1", "!node2")
        pusher = KumaPusher(store, config, manifest=manifest)

        # Mock the connection to return monitors
        conn = pusher._connections["t"]
        conn._node_monitors = {
            "!node1": MagicMock(push_token="mesh-node1-random"),
            "!node2": MagicMock(push_token="mesh-node2-random"),
        }

        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            results = pusher.push_cycle()

        # Both UP and DOWN are pushed
        assert mock_get.call_count == 2
        result = results["t"]
        assert len(result.up) == 1
        assert len(result.down) == 1

    def test_single_instance_loads_monitors_before_loop_no_duplicate(self) -> None:
        """Regression (issue #1): push_cycle must load the monitor list before
        iterating nodes. Otherwise the first node runs against an unconnected
        client (empty map), misses its existing monitor, and creates a duplicate
        every cycle."""
        store = MemoryStore()
        store.update_sighting(NodeSighting("!node1", time.time() - 100, "c1", name="Online"))

        config = PusherConfig(
            offline_threshold=3600,
            push_interval=600,
            push_secret="",  # single-instance mode
            targets=[KumaTarget(name="t", url="http://kuma:3001", username="admin", password="pw")],
        )
        manifest = make_manifest("!node1")
        pusher = KumaPusher(store, config, manifest=manifest)

        conn = pusher._connections["t"]
        # Fresh connection: monitor map is empty until connect() loads it, exactly
        # like a real first push. connect() populates the map (as _refresh_monitors
        # does) and returns True.
        existing = MonitorInfo(monitor_id=1, push_token="mesh-node1-tok", name="Online")

        def fake_connect() -> bool:
            conn._node_monitors = {"!node1": existing}
            return True

        conn.connect = MagicMock(side_effect=fake_connect)
        conn.create_monitor = MagicMock()

        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(raise_for_status=MagicMock())
            pusher.push_cycle()

        # It connected before the loop, found the existing monitor, and did NOT
        # create a duplicate.
        conn.connect.assert_called_once()
        conn.create_monitor.assert_not_called()
        assert mock_get.call_count == 1


class TestKumaConnection:
    """Tests for KumaConnection (mocked)."""

    def test_push_success(self) -> None:
        """Test successful HTTP push."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            result = conn.push("token123", "up", "Test message", ping_ms=1000)

            assert result is True
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "token123" in call_args[0][0]
            assert call_args[1]["params"]["status"] == "up"
            assert call_args[1]["params"]["msg"] == "Test message"
            assert call_args[1]["params"]["ping"] == 1000

    def test_push_failure(self) -> None:
        """Test failed HTTP push."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        with patch("requests.get") as mock_get:
            import requests
            mock_get.side_effect = requests.RequestException("Connection failed")

            result = conn.push("token123", "up", "Test message")

            assert result is False

    def test_connect_requires_credentials(self) -> None:
        """Test that connect fails without credentials."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        # Should fail because no username/password
        result = conn.connect()
        assert result is False

    def test_connect_requires_url(self) -> None:
        """Test that connect fails without URL."""
        target = KumaTarget(name="t", url="", username="admin", password="pass")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        result = conn.connect()
        assert result is False

    def test_sync_status_page_success(self) -> None:
        """Test successful status page sync."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        # Simulate connected state with monitors
        conn._sio = MagicMock()
        conn._sio_connected = True
        conn._monitors = {
            "1": {"name": "Zulu Node (!zzzz)"},
            "2": {"name": "Alpha Node (!aaaa)"},
        }

        # addStatusPage returns ok, saveStatusPage returns ok
        conn._sio.call.side_effect = [
            {"ok": True},   # addStatusPage
            {"ok": True},   # saveStatusPage
        ]

        result = conn.sync_status_page()
        assert result is True

        # Verify calls
        calls = conn._sio.call.call_args_list
        assert calls[0][0][0] == "addStatusPage"
        assert calls[1][0][0] == "saveStatusPage"

        # Verify monitors are sorted by name (Alpha before Zulu)
        _, save_args = calls[1][0]
        slug, config_dict, img, groups = save_args
        assert slug == "all"
        assert len(groups) == 1
        assert groups[0]["monitorList"] == [{"id": 2}, {"id": 1}]

    def test_sync_status_page_existing_page(self) -> None:
        """Test status page sync when page already exists."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        conn._sio = MagicMock()
        conn._sio_connected = True
        conn._monitors = {"1": {"name": "Node A"}}

        # addStatusPage fails (exists), saveStatusPage succeeds
        conn._sio.call.side_effect = [
            {"ok": False, "msg": "already exists"},
            {"ok": True},
        ]

        result = conn.sync_status_page()
        assert result is True

    def test_sync_status_page_not_connected(self) -> None:
        """Test status page sync fails when not connected."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        result = conn.sync_status_page()
        assert result is False

    def test_sync_status_page_no_monitors(self) -> None:
        """Test status page sync skips when no monitors exist."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        conn._sio = MagicMock()
        conn._sio_connected = True
        conn._monitors = {}

        result = conn.sync_status_page()
        assert result is False

    def test_sync_status_page_save_fails(self) -> None:
        """Test status page sync returns False when saveStatusPage fails."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        conn._sio = MagicMock()
        conn._sio_connected = True
        conn._monitors = {"1": {"name": "Node"}}

        conn._sio.call.side_effect = [
            {"ok": True},                              # addStatusPage
            {"ok": False, "msg": "Data truncated"},    # saveStatusPage
        ]

        result = conn.sync_status_page()
        assert result is False

    def test_sync_status_page_custom_params(self) -> None:
        """Test status page sync with custom slug/title."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        conn._sio = MagicMock()
        conn._sio_connected = True
        conn._monitors = {"1": {"name": "Node"}}

        conn._sio.call.side_effect = [{"ok": True}, {"ok": True}]

        result = conn.sync_status_page(
            slug="mesh", title="My Mesh", group_name="All Nodes"
        )
        assert result is True

        calls = conn._sio.call.call_args_list
        # addStatusPage called with custom title and slug
        assert calls[0][0][1] == ("My Mesh", "mesh")
        # saveStatusPage uses custom slug and group
        _, save_args = calls[1][0]
        assert save_args[0] == "mesh"
        assert save_args[3][0]["name"] == "All Nodes"

    def test_sync_status_page_exception(self) -> None:
        """Test status page sync handles exceptions gracefully."""
        target = KumaTarget(name="t", url="http://kuma:3001")
        config = PusherConfig()
        conn = KumaConnection(target, config)

        conn._sio = MagicMock()
        conn._sio_connected = True
        conn._monitors = {"1": {"name": "Node"}}

        conn._sio.call.side_effect = Exception("Socket.io timeout")

        result = conn.sync_status_page()
        assert result is False


class TestSightingServer:
    """Tests for the HTTP sighting server."""

    @pytest.fixture()
    def server_and_store(self):
        """Start a sighting server with a memory store."""
        store = MemoryStore()
        server = start_sighting_server("127.0.0.1:0", store, sighting_token="test-token")
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        yield base, store
        server.shutdown()

    @pytest.fixture()
    def server_no_auth(self):
        """Start a sighting server with no auth."""
        store = MemoryStore()
        server = start_sighting_server("127.0.0.1:0", store, sighting_token="")
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        yield base, store
        server.shutdown()

    def test_post_sighting(self, server_and_store) -> None:
        """Test posting a valid sighting."""
        base, store = server_and_store
        payload = {
            "node_id": "!abcd1234",
            "last_seen": time.time(),
            "source": "test-collector",
            "battery": 85,
        }
        resp = requests.post(
            f"{base}/sighting",
            json=payload,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

        node = store.get_node("!abcd1234")
        assert node is not None
        assert node.battery == 85
        assert "test-collector" in node.collectors

    def test_post_sighting_unauthorized(self, server_and_store) -> None:
        """Test posting without auth returns 401."""
        base, store = server_and_store
        payload = {
            "node_id": "!abcd1234",
            "last_seen": time.time(),
            "source": "test",
        }
        resp = requests.post(f"{base}/sighting", json=payload)
        assert resp.status_code == 401

    def test_post_sighting_wrong_token(self, server_and_store) -> None:
        """Test posting with wrong token returns 401."""
        base, store = server_and_store
        payload = {
            "node_id": "!abcd1234",
            "last_seen": time.time(),
            "source": "test",
        }
        resp = requests.post(
            f"{base}/sighting",
            json=payload,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_post_sighting_no_auth_required(self, server_no_auth) -> None:
        """Test posting works when no token is configured."""
        base, store = server_no_auth
        payload = {
            "node_id": "!abcd1234",
            "last_seen": time.time(),
            "source": "test",
        }
        resp = requests.post(f"{base}/sighting", json=payload)
        assert resp.status_code == 200
        assert store.get_node("!abcd1234") is not None

    def test_post_sighting_bad_json(self, server_and_store) -> None:
        """Test posting invalid JSON returns 400."""
        base, _ = server_and_store
        resp = requests.post(
            f"{base}/sighting",
            data=b"not json",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400

    def test_post_sighting_missing_fields(self, server_and_store) -> None:
        """Test posting with missing required fields returns 400."""
        base, _ = server_and_store
        resp = requests.post(
            f"{base}/sighting",
            json={"node_id": "!abc"},  # missing last_seen
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400

    def test_post_wrong_path(self, server_and_store) -> None:
        """Test POST to unknown path returns 404."""
        base, _ = server_and_store
        resp = requests.post(
            f"{base}/unknown",
            json={},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404

    def test_health_endpoint(self, server_and_store) -> None:
        """Test GET /health returns 200."""
        base, _ = server_and_store
        resp = requests.get(f"{base}/health")
        assert resp.status_code == 200

    def test_post_sighting_with_all_fields(self, server_and_store) -> None:
        """Test posting a sighting with all optional fields."""
        base, store = server_and_store
        now = time.time()
        payload = {
            "node_id": "!12345678",
            "last_seen": now,
            "source": "collector-1",
            "name": "Test Node",
            "snr": -5.5,
            "hops": 2,
            "battery": 90,
            "voltage": 3.95,
            "latitude": 37.7749,
            "longitude": -122.4194,
            "altitude": 10,
            "via_neighbor": True,
            "observer_id": "!observer1",
        }
        resp = requests.post(
            f"{base}/sighting",
            json=payload,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

        node = store.get_node("!12345678")
        assert node is not None
        assert node.name == "Test Node"
        assert node.snr == -5.5
        assert node.battery == 90
        assert node.latitude == 37.7749


class TestSyncTarget:
    """Tests for _sync_target (used by `kumatastic sync`)."""

    def test_creates_missing_monitors(self) -> None:
        """Test that sync creates monitors for manifest nodes without one."""
        conn = MagicMock(spec=KumaConnection)
        conn.get_monitor_for_node.return_value = None
        conn.create_monitor.return_value = MonitorInfo(
            monitor_id=1, push_token="mesh-abc-tok", name="Node"
        )
        conn._node_monitors = {}

        manifest_nodes = {
            "!abc": ManifestNode(node_id="!abc", name="Alpha"),
            "!def": ManifestNode(node_id="!def", name="Beta"),
        }

        created, skipped, deleted, failed = _sync_target(
            "test", conn, set(manifest_nodes.keys()), manifest_nodes,
            distributed=False, push_secret="",
        )

        assert created == 2
        assert skipped == 0
        assert deleted == 0
        assert failed == 0
        assert conn.create_monitor.call_count == 2

    def test_skips_existing_monitors(self) -> None:
        """Test that sync skips manifest nodes that already have monitors."""
        conn = MagicMock(spec=KumaConnection)
        conn.get_monitor_for_node.return_value = MonitorInfo(
            monitor_id=1, push_token="tok", name="Existing"
        )
        conn._node_monitors = {
            "!abc": MonitorInfo(monitor_id=1, push_token="tok", name="Existing"),
        }

        manifest_nodes = {
            "!abc": ManifestNode(node_id="!abc", name="Alpha"),
        }

        created, skipped, deleted, failed = _sync_target(
            "test", conn, {"!abc"}, manifest_nodes,
            distributed=False, push_secret="",
        )

        assert created == 0
        assert skipped == 1
        assert deleted == 0
        conn.create_monitor.assert_not_called()

    def test_deletes_orphan_monitors(self) -> None:
        """Test that sync deletes monitors for nodes not in manifest."""
        conn = MagicMock(spec=KumaConnection)
        conn.get_monitor_for_node.return_value = MonitorInfo(
            monitor_id=1, push_token="tok", name="Existing"
        )
        conn.delete_monitor.return_value = True
        # Kuma has monitors for !abc (in manifest) and !orphan (not in manifest)
        conn._node_monitors = {
            "!abc": MonitorInfo(monitor_id=1, push_token="tok1", name="Alpha"),
            "!orphan": MonitorInfo(monitor_id=2, push_token="tok2", name="Old Node"),
        }

        manifest_nodes = {
            "!abc": ManifestNode(node_id="!abc", name="Alpha"),
        }

        created, skipped, deleted, failed = _sync_target(
            "test", conn, {"!abc"}, manifest_nodes,
            distributed=False, push_secret="",
        )

        assert skipped == 1
        assert deleted == 1
        conn.delete_monitor.assert_called_once_with(2)

    def test_creates_and_deletes_in_one_pass(self) -> None:
        """Test sync that both creates new and deletes orphaned monitors."""
        conn = MagicMock(spec=KumaConnection)

        # !existing is in both, !new is manifest-only, !orphan is kuma-only
        def get_monitor(node_id):
            if node_id == "!existing":
                return MonitorInfo(monitor_id=1, push_token="t1", name="Existing")
            return None

        conn.get_monitor_for_node.side_effect = get_monitor
        conn.create_monitor.return_value = MonitorInfo(
            monitor_id=3, push_token="t3", name="New"
        )
        conn.delete_monitor.return_value = True
        conn._node_monitors = {
            "!existing": MonitorInfo(monitor_id=1, push_token="t1", name="Existing"),
            "!orphan": MonitorInfo(monitor_id=2, push_token="t2", name="Orphan"),
        }

        manifest_nodes = {
            "!existing": ManifestNode(node_id="!existing", name="Existing"),
            "!new": ManifestNode(node_id="!new", name="New Node"),
        }

        created, skipped, deleted, failed = _sync_target(
            "test", conn, set(manifest_nodes.keys()), manifest_nodes,
            distributed=False, push_secret="",
        )

        assert created == 1
        assert skipped == 1
        assert deleted == 1
        assert failed == 0

    def test_distributed_mode_uses_derived_tokens(self) -> None:
        """Test that sync in distributed mode derives tokens from secret."""
        from kumatastic.manifest import derive_push_token

        conn = MagicMock(spec=KumaConnection)
        conn.get_monitor_for_node.return_value = None
        conn.create_monitor.return_value = MonitorInfo(
            monitor_id=1, push_token="tok", name="Node"
        )
        conn._node_monitors = {}

        manifest_nodes = {
            "!abcd1234": ManifestNode(node_id="!abcd1234", name="Alpha"),
        }

        _sync_target(
            "test", conn, {"!abcd1234"}, manifest_nodes,
            distributed=True, push_secret="my-secret",
        )

        expected_token = derive_push_token("my-secret", "!abcd1234")
        conn.create_monitor.assert_called_once_with("!abcd1234", "Alpha", push_token=expected_token)

    def test_no_changes_when_in_sync(self) -> None:
        """Test that sync does nothing when manifest matches Kuma exactly."""
        conn = MagicMock(spec=KumaConnection)
        conn.get_monitor_for_node.return_value = MonitorInfo(
            monitor_id=1, push_token="tok", name="Alpha"
        )
        conn._node_monitors = {
            "!abc": MonitorInfo(monitor_id=1, push_token="tok", name="Alpha"),
        }

        manifest_nodes = {
            "!abc": ManifestNode(node_id="!abc", name="Alpha"),
        }

        created, skipped, deleted, failed = _sync_target(
            "test", conn, {"!abc"}, manifest_nodes,
            distributed=False, push_secret="",
        )

        assert created == 0
        assert skipped == 1
        assert deleted == 0
        assert failed == 0
        conn.create_monitor.assert_not_called()
        conn.delete_monitor.assert_not_called()
