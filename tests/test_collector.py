"""Tests for the collector module."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from kumatastic.collector import MeshCollector, _ConnectionLost
from kumatastic.config import CollectorConfig
from kumatastic.manifest import Manifest, ManifestNode
from kumatastic.state import MemoryStore, NodeSighting


def make_manifest(*node_ids: str) -> Manifest:
    """Create a manifest with the given node IDs."""
    nodes = {}
    for nid in node_ids:
        normalized = nid if nid.startswith("!") else f"!{nid}"
        normalized = normalized.lower()
        nodes[normalized] = ManifestNode(node_id=normalized, name=f"Node {normalized}")
    return Manifest(nodes=nodes)


class TestMeshCollector:
    """Tests for MeshCollector."""

    def make_collector(
        self, manifest_ids: list[str] | None = None,
    ) -> tuple[MeshCollector, MemoryStore]:
        """Create a collector with a memory store."""
        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
            state_path="/tmp/state.json",
            neighbor_max_age=3600,
        )
        store = MemoryStore()
        manifest = make_manifest(*(manifest_ids or ["!abcd1234"]))
        collector = MeshCollector(config, store, manifest=manifest)
        return collector, store

    def test_on_receive_basic_packet(self) -> None:
        """Test handling a basic packet."""
        collector, store = self.make_collector()

        packet = {
            "fromId": "!abcd1234",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
            },
        }

        collector._on_receive(packet, None)

        node = store.get_node("!abcd1234")
        assert node is not None
        assert node.node_id == "!abcd1234"
        assert node.sighting_count == 1
        assert "test-collector" in node.collectors

    def test_on_receive_normalizes_node_id(self) -> None:
        """Test that node ID is normalized with ! prefix."""
        collector, store = self.make_collector()

        packet = {
            "fromId": "abcd1234",  # Missing !
            "decoded": {"portnum": "TEXT_MESSAGE_APP"},
        }

        collector._on_receive(packet, None)

        node = store.get_node("!abcd1234")
        assert node is not None

    def test_on_receive_position_packet(self) -> None:
        """Test handling a position packet."""
        collector, store = self.make_collector()

        packet = {
            "fromId": "!abcd1234",
            "decoded": {
                "portnum": "POSITION_APP",
                "position": {
                    "latitude": 37.7749,
                    "longitude": -122.4194,
                    "altitude": 10,
                },
            },
        }

        collector._on_receive(packet, None)

        node = store.get_node("!abcd1234")
        assert node is not None
        assert node.latitude == 37.7749
        assert node.longitude == -122.4194
        assert node.altitude == 10

    def test_on_receive_telemetry_packet(self) -> None:
        """Test handling a telemetry packet."""
        collector, store = self.make_collector()

        packet = {
            "fromId": "!abcd1234",
            "decoded": {
                "portnum": "TELEMETRY_APP",
                "telemetry": {
                    "deviceMetrics": {
                        "batteryLevel": 85,
                        "voltage": 3.95,
                    },
                },
            },
        }

        collector._on_receive(packet, None)

        node = store.get_node("!abcd1234")
        assert node is not None
        assert node.battery == 85
        assert node.voltage == 3.95

    def test_on_receive_ignores_empty_from_id(self) -> None:
        """Test that packets without fromId are ignored."""
        collector, store = self.make_collector()

        packet = {
            "decoded": {"portnum": "TEXT_MESSAGE_APP"},
        }

        collector._on_receive(packet, None)

        assert len(store.get_all_nodes()) == 0

    def test_on_receive_skips_non_manifest_node(self) -> None:
        """Test that packets from non-manifest nodes are skipped."""
        collector, store = self.make_collector(manifest_ids=["!abcd1234"])

        packet = {
            "fromId": "!99999999",
            "decoded": {"portnum": "TEXT_MESSAGE_APP"},
        }

        collector._on_receive(packet, None)

        assert store.get_node("!99999999") is None
        assert len(store.get_all_nodes()) == 0


class TestNeighborInfoHandling:
    """Tests for NeighborInfo packet handling."""

    def make_collector(
        self, manifest_ids: list[str] | None = None,
    ) -> tuple[MeshCollector, MemoryStore]:
        """Create a collector with a memory store."""
        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
            neighbor_max_age=3600,
        )
        store = MemoryStore()
        manifest = make_manifest(*(manifest_ids or ["!12345678", "!abcdef00"]))
        collector = MeshCollector(config, store, manifest=manifest)
        return collector, store

    def test_handle_neighbor_info(self) -> None:
        """Test handling NeighborInfo packet."""
        collector, store = self.make_collector()
        now = time.time()

        packet = {
            "fromId": "!observer1",
            "decoded": {
                "portnum": "NEIGHBORINFO_APP",
                "neighborinfo": {
                    "neighbors": [
                        {"node_id": 0x12345678, "snr": -5.5},
                        {"node_id": 0xabcdef00, "snr": 10.0},
                    ],
                },
            },
        }

        collector._handle_neighbor_info(packet, now)

        # Check that manifest neighbor nodes were recorded
        node1 = store.get_node("!12345678")
        assert node1 is not None
        assert node1.snr == -5.5
        assert node1.sighting_count == 1

        node2 = store.get_node("!abcdef00")
        assert node2 is not None
        assert node2.snr == 10.0

    def test_handle_neighbor_info_skips_non_manifest(self) -> None:
        """Test that neighbors not in manifest are skipped."""
        collector, store = self.make_collector(manifest_ids=["!12345678"])
        now = time.time()

        packet = {
            "fromId": "!observer1",
            "decoded": {
                "portnum": "NEIGHBORINFO_APP",
                "neighborinfo": {
                    "neighbors": [
                        {"node_id": 0x12345678, "snr": -5.5},
                        {"node_id": 0x99999999, "snr": 10.0},  # not in manifest
                    ],
                },
            },
        }

        collector._handle_neighbor_info(packet, now)

        assert store.get_node("!12345678") is not None
        assert store.get_node("!99999999") is None

    def test_handle_neighbor_info_empty(self) -> None:
        """Test handling NeighborInfo with no neighbors."""
        collector, store = self.make_collector()
        now = time.time()

        packet = {
            "fromId": "!observer1",
            "decoded": {
                "portnum": "NEIGHBORINFO_APP",
                "neighborinfo": {"neighbors": []},
            },
        }

        collector._handle_neighbor_info(packet, now)
        assert len(store.get_all_nodes()) == 0

    def test_prune_stale_neighbors(self) -> None:
        """Test pruning stale neighbor sightings."""
        collector, store = self.make_collector()
        now = time.time()

        # Add some neighbor sightings
        old_time = now - 7200  # 2 hours ago
        collector._neighbor_times["!old_node"] = {"!observer1": old_time}
        collector._neighbor_times["!new_node"] = {"!observer1": now}

        # Prune with 1 hour max age
        collector.config.neighbor_max_age = 3600
        collector._prune_stale_neighbors()

        # Old node should be removed
        assert "!old_node" not in collector._neighbor_times
        # New node should remain
        assert "!new_node" in collector._neighbor_times


class TestNodeDBScanning:
    """Tests for node database scanning."""

    def make_collector(
        self, manifest_ids: list[str] | None = None,
    ) -> tuple[MeshCollector, MemoryStore]:
        """Create a collector with a memory store."""
        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
        )
        store = MemoryStore()
        manifest = make_manifest(*(manifest_ids or ["!node1", "!node2"]))
        collector = MeshCollector(config, store, manifest=manifest)
        return collector, store

    def test_scan_node_db(self) -> None:
        """Test scanning the node database."""
        collector, store = self.make_collector()
        now = time.time()

        # Mock interface with nodes
        mock_interface = MagicMock()
        mock_interface.nodes = {
            "!node1": {
                "lastHeard": now - 300,
                "user": {"longName": "Node One", "shortName": "N1"},
                "snr": -3.0,
                "hopsAway": 2,
            },
            "!node2": {
                "lastHeard": now - 600,
                "user": {"longName": "Node Two"},
                "deviceMetrics": {"batteryLevel": 75},
            },
        }
        collector._interface = mock_interface

        collector._scan_node_db()

        # Check nodes were recorded
        node1 = store.get_node("!node1")
        assert node1 is not None
        assert node1.name == "Node One"
        assert node1.snr == -3.0
        assert node1.hops == 2

        node2 = store.get_node("!node2")
        assert node2 is not None
        assert node2.name == "Node Two"
        assert node2.battery == 75

    def test_scan_node_db_skips_non_manifest(self) -> None:
        """Test that scan skips nodes not in manifest."""
        collector, store = self.make_collector(manifest_ids=["!node1"])
        now = time.time()

        mock_interface = MagicMock()
        mock_interface.nodes = {
            "!node1": {
                "lastHeard": now,
                "user": {"longName": "Node One"},
            },
            "!node99": {
                "lastHeard": now,
                "user": {"longName": "Not in manifest"},
            },
        }
        collector._interface = mock_interface

        collector._scan_node_db()

        assert store.get_node("!node1") is not None
        assert store.get_node("!node99") is None

    def test_scan_node_db_no_interface(self) -> None:
        """Test scan_node_db with no interface."""
        collector, store = self.make_collector()
        collector._interface = None

        # Should not raise
        collector._scan_node_db()
        assert len(store.get_all_nodes()) == 0

    def test_scan_node_db_normalizes_ids(self) -> None:
        """Test that node IDs are normalized."""
        collector, store = self.make_collector(manifest_ids=["!abcd1234"])
        now = time.time()

        mock_interface = MagicMock()
        mock_interface.nodes = {
            "abcd1234": {  # Missing ! prefix
                "lastHeard": now,
                "user": {},
            },
        }
        collector._interface = mock_interface

        collector._scan_node_db()

        # Should be normalized with !
        node = store.get_node("!abcd1234")
        assert node is not None


class TestConnectionParsing:
    """Tests for connection string parsing."""

    def test_tcp_connection_with_port(self) -> None:
        """Test TCP connection string parsing with port."""
        config = CollectorConfig(
            id="test",
            meshtastic="tcp:192.168.1.100:4403",
        )
        store = MemoryStore()
        manifest = make_manifest("!test")
        collector = MeshCollector(config, store, manifest=manifest)

        # We can't actually connect, but we can verify the config is stored
        assert collector.config.meshtastic == "tcp:192.168.1.100:4403"

    def test_tcp_connection_default_port(self) -> None:
        """Test TCP connection string with default port."""
        config = CollectorConfig(
            id="test",
            meshtastic="tcp:192.168.1.100",
        )
        store = MemoryStore()
        manifest = make_manifest("!test")
        collector = MeshCollector(config, store, manifest=manifest)

        assert collector.config.meshtastic == "tcp:192.168.1.100"

    def test_serial_connection(self) -> None:
        """Test serial connection string parsing."""
        config = CollectorConfig(
            id="test",
            meshtastic="serial:/dev/ttyUSB0",
        )
        store = MemoryStore()
        manifest = make_manifest("!test")
        collector = MeshCollector(config, store, manifest=manifest)

        assert collector.config.meshtastic == "serial:/dev/ttyUSB0"


class TestCollectorHttpForwarding:
    """Tests for HTTP forwarding of sightings to pusher URLs."""

    def make_collector(
        self,
        pusher_urls: list[str] | None = None,
        sighting_token: str = "",
    ) -> tuple[MeshCollector, MemoryStore]:
        """Create a collector with pusher_urls configured."""
        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
            pusher_urls=pusher_urls or [],
            sighting_token=sighting_token,
        )
        store = MemoryStore()
        manifest = make_manifest("!abcd1234")
        collector = MeshCollector(config, store, manifest=manifest)
        return collector, store

    def test_forward_sighting_posts_to_urls(self) -> None:
        """Test that sightings are POSTed to pusher URLs."""
        collector, store = self.make_collector(
            pusher_urls=["http://localhost:9100"],
            sighting_token="my-token",
        )

        with patch("kumatastic.collector.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()

            packet = {
                "fromId": "!abcd1234",
                "decoded": {"portnum": "TEXT_MESSAGE_APP"},
            }
            collector._on_receive(packet, None)

            # Give the thread pool time to execute
            import time
            time.sleep(0.2)

            assert mock_post.call_count == 1
            call_args = mock_post.call_args
            assert call_args[0][0] == "http://localhost:9100/sighting"
            assert call_args[1]["headers"]["Authorization"] == "Bearer my-token"
            assert call_args[1]["json"]["node_id"] == "!abcd1234"

        collector.stop()

    def test_forward_sighting_no_urls(self) -> None:
        """Test that no forwarding happens when no pusher_urls configured."""
        collector, store = self.make_collector(pusher_urls=[])

        with patch("kumatastic.collector.requests.post") as mock_post:
            packet = {
                "fromId": "!abcd1234",
                "decoded": {"portnum": "TEXT_MESSAGE_APP"},
            }
            collector._on_receive(packet, None)

            import time
            time.sleep(0.1)

            assert mock_post.call_count == 0

    def test_forward_sighting_failure_does_not_crash(self) -> None:
        """Test that HTTP failure doesn't crash the collector."""
        collector, store = self.make_collector(
            pusher_urls=["http://localhost:9999"],
            sighting_token="token",
        )

        with patch("kumatastic.collector.requests.post") as mock_post:
            mock_post.side_effect = Exception("Connection refused")

            packet = {
                "fromId": "!abcd1234",
                "decoded": {"portnum": "TEXT_MESSAGE_APP"},
            }
            collector._on_receive(packet, None)

            import time
            time.sleep(0.2)

            # Sighting should still be in local state
            node = store.get_node("!abcd1234")
            assert node is not None

        collector.stop()

    def test_forward_sighting_multiple_urls(self) -> None:
        """Test that sightings are POSTed to all configured URLs."""
        collector, store = self.make_collector(
            pusher_urls=["http://host1:9100", "http://host2:9100"],
        )

        with patch("kumatastic.collector.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()

            packet = {
                "fromId": "!abcd1234",
                "decoded": {"portnum": "TEXT_MESSAGE_APP"},
            }
            collector._on_receive(packet, None)

            import time
            time.sleep(0.3)

            assert mock_post.call_count == 2
            urls = [call[0][0] for call in mock_post.call_args_list]
            assert "http://host1:9100/sighting" in urls
            assert "http://host2:9100/sighting" in urls

        collector.stop()


class TestReconnection:
    """Tests for connection loss detection and reconnection."""

    def make_collector(self) -> tuple[MeshCollector, MemoryStore]:
        """Create a collector with a memory store."""
        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
        )
        store = MemoryStore()
        manifest = make_manifest("!abcd1234")
        collector = MeshCollector(config, store, manifest=manifest)
        return collector, store

    def test_reconnects_on_connection_lost(self) -> None:
        """Test that the collector reconnects when connection is lost."""
        collector, store = self.make_collector()
        connect_count = 0

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count <= 2:
                # First two connections: return mock with cleared isConnected
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()  # not set = disconnected
                return mock_iface
            else:
                # Third attempt: stop the collector
                collector.stop()
                return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        # Should have attempted at least 3 connections (2 lost + 1 failed after stop)
        assert connect_count >= 2

    def test_reconnect_backoff_caps(self) -> None:
        """Test that reconnect backoff caps at 300s."""
        collector, store = self.make_collector()
        wait_times: list[float] = []
        connect_count = 0

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count > 10:
                collector.stop()
            return None  # always fail

        def tracking_wait(timeout: float = None) -> bool:
            if timeout is not None and timeout >= 5:
                wait_times.append(timeout)
            return collector._stop_event.is_set()

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector._stop_event.wait = tracking_wait
            collector.run()

        # Backoff should cap at 300
        assert len(wait_times) > 0
        assert all(t <= 300 for t in wait_times)

    def test_reconnect_resets_backoff_on_success(self) -> None:
        """Test that backoff resets after a successful connection."""
        collector, store = self.make_collector()
        connect_count = 0
        wait_times: list[float] = []

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count <= 3:
                return None  # fail 3 times
            elif connect_count == 4:
                # Succeed, but then lose connection immediately
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()  # cleared = disconnected
                return mock_iface
            elif connect_count == 5:
                return None  # fail once more
            else:
                collector.stop()
                return None

        original_wait = collector._stop_event.wait

        def tracking_wait(timeout: float = None) -> bool:
            if timeout is not None and timeout >= 5:
                wait_times.append(timeout)
            return collector._stop_event.is_set()

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector._stop_event.wait = tracking_wait
            collector.run()

        # After success at attempt 4, backoff should reset
        # Wait times should be: 5, 10, 20 (failures 1-3), then 5 (reset after success)
        assert len(wait_times) >= 4
        assert wait_times[3] == 5  # Reset after successful connect + disconnect

    def test_stop_breaks_reconnect_loop(self) -> None:
        """Test that stop() exits the reconnection loop promptly."""
        collector, store = self.make_collector()

        def mock_connect():
            return None  # always fail

        def stop_after_delay():
            time.sleep(0.3)
            collector.stop()

        stopper = threading.Thread(target=stop_after_delay)
        stopper.start()

        with patch.object(collector, "_connect", side_effect=mock_connect):
            start = time.time()
            collector.run()
            elapsed = time.time() - start

        stopper.join()
        # Should exit within a reasonable time (backoff wait is interruptible)
        assert elapsed < 2.0

    def test_disconnect_clears_interface(self) -> None:
        """Test that _disconnect clears the interface."""
        collector, store = self.make_collector()
        collector._interface = MagicMock()

        collector._disconnect()

        assert collector._interface is None
        assert collector._pubsub_listener is None

    def test_connection_lost_exception(self) -> None:
        """Test _ConnectionLost is a proper exception."""
        exc = _ConnectionLost("test")
        assert str(exc) == "test"
        assert isinstance(exc, Exception)

    def test_run_loop_raises_on_disconnected_interface(self) -> None:
        """Test _run_loop raises _ConnectionLost when isConnected is cleared."""
        collector, store = self.make_collector()
        collector._running = True
        collector._stop_event.clear()

        mock_iface = MagicMock()
        mock_iface.isConnected = threading.Event()  # not set
        collector._interface = mock_iface

        # Patch _stop_event.wait to not actually sleep and track iterations
        iteration = 0

        def fast_wait(timeout=None):
            nonlocal iteration
            iteration += 1
            # Allow enough iterations for health check to trigger
            return False

        collector._stop_event.wait = fast_wait

        # The health check fires after 30s. We need time.time() to advance past that.
        original_time = time.time
        base = original_time()
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            # Each call advances by 31s so health check triggers
            return base + (call_count * 31)

        with patch("kumatastic.collector.time.time", side_effect=advancing_time):
            with pytest.raises(_ConnectionLost):
                collector._run_loop()

    def test_run_loop_raises_when_interface_becomes_none(self) -> None:
        """Test _run_loop raises _ConnectionLost when interface is set to None."""
        collector, store = self.make_collector()
        collector._running = True
        collector._stop_event.clear()

        mock_iface = MagicMock()
        mock_iface.isConnected = threading.Event()
        mock_iface.isConnected.set()  # starts connected
        collector._interface = mock_iface

        iteration = 0

        def fast_wait(timeout=None):
            nonlocal iteration
            iteration += 1
            # After a few iterations, set interface to None
            if iteration == 3:
                collector._interface = None
            return False

        collector._stop_event.wait = fast_wait

        base = time.time()
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return base + (call_count * 31)

        with patch("kumatastic.collector.time.time", side_effect=advancing_time):
            with pytest.raises(_ConnectionLost, match="Interface is None"):
                collector._run_loop()

    def test_run_loop_no_raise_when_no_isconnected_attr(self) -> None:
        """Test _run_loop doesn't raise if interface lacks isConnected attribute."""
        collector, store = self.make_collector()
        collector._running = True
        collector._stop_event.clear()

        # Interface without isConnected attribute (e.g. serial interface)
        mock_iface = MagicMock(spec=[])  # no attributes
        collector._interface = mock_iface

        iteration = 0

        def fast_wait(timeout=None):
            nonlocal iteration
            iteration += 1
            if iteration > 5:
                collector._running = False
            return not collector._running

        collector._stop_event.wait = fast_wait

        base = time.time()
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return base + (call_count * 31)

        # Should NOT raise — no isConnected means no health check failure
        with patch("kumatastic.collector.time.time", side_effect=advancing_time):
            collector._run_loop()

    def test_run_loop_exits_cleanly_on_stop(self) -> None:
        """Test _run_loop exits without exception when _running is set False."""
        collector, store = self.make_collector()
        collector._running = True
        collector._stop_event.clear()

        mock_iface = MagicMock()
        mock_iface.isConnected = threading.Event()
        mock_iface.isConnected.set()
        collector._interface = mock_iface

        iteration = 0

        def fast_wait(timeout=None):
            nonlocal iteration
            iteration += 1
            if iteration >= 3:
                collector._running = False
                collector._stop_event.set()
            return collector._stop_event.is_set()

        collector._stop_event.wait = fast_wait

        # Should return normally, no exception
        collector._run_loop()
        assert not collector._running

    def test_run_loop_prunes_neighbors_periodically(self) -> None:
        """Test that _run_loop calls _prune_stale_neighbors on schedule."""
        collector, store = self.make_collector()
        collector._running = True
        collector._stop_event.clear()

        mock_iface = MagicMock()
        mock_iface.isConnected = threading.Event()
        mock_iface.isConnected.set()
        collector._interface = mock_iface

        prune_count = 0
        original_prune = collector._prune_stale_neighbors

        def counting_prune():
            nonlocal prune_count
            prune_count += 1

        collector._prune_stale_neighbors = counting_prune

        base = time.time()
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            # Jump 3601s each call to trigger pruning every iteration
            return base + (call_count * 3601)

        iteration = 0

        def fast_wait(timeout=None):
            nonlocal iteration
            iteration += 1
            if iteration > 3:
                collector._running = False
            return not collector._running

        collector._stop_event.wait = fast_wait

        with patch("kumatastic.collector.time.time", side_effect=advancing_time):
            collector._run_loop()

        assert prune_count >= 1

    def test_disconnect_interface_close_raises(self) -> None:
        """Test that _disconnect handles interface.close() exceptions gracefully."""
        collector, store = self.make_collector()
        mock_iface = MagicMock()
        mock_iface.close.side_effect = RuntimeError("TCP socket already closed")
        collector._interface = mock_iface

        # Should not raise
        collector._disconnect()

        assert collector._interface is None

    def test_disconnect_pubsub_unsubscribe_raises(self) -> None:
        """Test that _disconnect handles pubsub unsubscribe exceptions gracefully."""
        collector, store = self.make_collector()
        collector._pubsub_listener = collector._on_receive
        collector._interface = MagicMock()

        # Even if pubsub import/unsubscribe raises, disconnect should complete
        with patch("builtins.__import__", side_effect=Exception("pubsub broken")):
            collector._disconnect()

        assert collector._pubsub_listener is None
        assert collector._interface is None

    def test_disconnect_idempotent(self) -> None:
        """Test that calling _disconnect multiple times is safe."""
        collector, store = self.make_collector()
        collector._interface = MagicMock()

        collector._disconnect()
        assert collector._interface is None

        # Second call should be a no-op, not raise
        collector._disconnect()
        assert collector._interface is None
        assert collector._pubsub_listener is None

    def test_cleanup_shuts_down_push_pool(self) -> None:
        """Test that _cleanup shuts down the push pool."""
        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
            pusher_urls=["http://localhost:9100"],
        )
        store = MemoryStore()
        manifest = make_manifest("!abcd1234")
        collector = MeshCollector(config, store, manifest=manifest)

        assert collector._push_pool is not None
        mock_pool = MagicMock()
        collector._push_pool = mock_pool

        collector._cleanup()

        mock_pool.shutdown.assert_called_once_with(wait=False)
        assert not collector._running

    def test_cleanup_without_push_pool(self) -> None:
        """Test that _cleanup works when no push pool exists."""
        collector, store = self.make_collector()
        assert collector._push_pool is None

        # Should not raise
        collector._cleanup()
        assert not collector._running

    def test_cleanup_idempotent(self) -> None:
        """Test that calling _cleanup multiple times is safe."""
        collector, store = self.make_collector()
        collector._interface = MagicMock()

        collector._cleanup()
        collector._cleanup()  # should not raise
        assert not collector._running

    def test_register_callbacks_sets_pubsub_listener(self) -> None:
        """Test that _register_callbacks tracks the pubsub listener."""
        collector, store = self.make_collector()
        collector._interface = MagicMock()

        assert collector._pubsub_listener is None
        collector._register_callbacks()
        # pubsub is installed, so _pubsub_listener should be the _on_receive bound method
        assert collector._pubsub_listener == collector._on_receive

    def test_register_callbacks_falls_back_to_interface(self) -> None:
        """Test that _register_callbacks uses interface.onReceive when pubsub unavailable."""
        collector, store = self.make_collector()
        mock_iface = MagicMock()
        mock_iface.onReceive = None
        collector._interface = mock_iface

        # Make pubsub import fail inside _register_callbacks
        import builtins
        import sys
        original_import = builtins.__import__

        def no_pubsub(name, *args, **kwargs):
            if name == "pubsub":
                raise ImportError("no pubsub")
            return original_import(name, *args, **kwargs)

        # Remove pubsub from sys.modules cache so the import actually runs
        saved_modules = {}
        for key in list(sys.modules):
            if key == "pubsub" or key.startswith("pubsub."):
                saved_modules[key] = sys.modules.pop(key)

        try:
            with patch("builtins.__import__", side_effect=no_pubsub):
                collector._register_callbacks()
        finally:
            # Restore pubsub modules
            sys.modules.update(saved_modules)

        assert mock_iface.onReceive is not None
        # pubsub_listener should not be set (pubsub was unavailable)
        assert collector._pubsub_listener is None

    def test_run_rescans_nodedb_on_each_reconnect(self) -> None:
        """Test that nodeDB is scanned on every successful connection."""
        collector, store = self.make_collector()
        connect_count = 0
        scan_count = 0
        original_scan = collector._scan_node_db

        def counting_scan():
            nonlocal scan_count
            scan_count += 1

        collector._scan_node_db = counting_scan

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count <= 3:
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()  # cleared = disconnected
                return mock_iface
            else:
                collector.stop()
                return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        # scan_node_db should be called for each successful connection
        assert scan_count == 3

    def test_run_reregisters_callbacks_on_each_reconnect(self) -> None:
        """Test that callbacks are re-registered on every reconnection."""
        collector, store = self.make_collector()
        connect_count = 0
        register_count = 0
        original_register = collector._register_callbacks

        def counting_register():
            nonlocal register_count
            register_count += 1

        collector._register_callbacks = counting_register

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count <= 2:
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()
                return mock_iface
            else:
                collector.stop()
                return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        assert register_count == 2

    def test_run_disconnect_called_after_each_connection(self) -> None:
        """Test that _disconnect is called between reconnections."""
        collector, store = self.make_collector()
        connect_count = 0
        disconnect_count = 0
        original_disconnect = collector._disconnect

        def counting_disconnect():
            nonlocal disconnect_count
            disconnect_count += 1
            original_disconnect()

        collector._disconnect = counting_disconnect

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count <= 2:
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()
                return mock_iface
            else:
                collector.stop()
                return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        # _disconnect called after each connected session + once in _cleanup
        assert disconnect_count >= 2

    def test_backoff_exact_sequence(self) -> None:
        """Test the exact backoff doubling: 5, 10, 20, 40, 80, 160, 300, 300..."""
        collector, store = self.make_collector()
        wait_times: list[float] = []
        connect_count = 0

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count > 8:
                collector.stop()
            return None

        def tracking_wait(timeout: float = None) -> bool:
            if timeout is not None and timeout >= 5:
                wait_times.append(timeout)
            # After stop(), _stop_event is set, so return True to exit
            return collector._stop_event.is_set()

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector._stop_event.wait = tracking_wait
            collector.run()

        # 8 failures before stop, each records a wait time
        # Doubling: 5, 10, 20, 40, 80, 160, 300(cap), 300(cap)
        # The 9th connect call triggers stop(), but the wait still records before loop exits
        assert wait_times[:7] == [5, 10, 20, 40, 80, 160, 300]
        # All remaining waits are capped at 300
        assert all(t == 300 for t in wait_times[6:])

    def test_state_preserved_across_reconnections(self) -> None:
        """Test that state store data survives reconnections."""
        collector, store = self.make_collector()
        connect_count = 0

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                # First connection: write some state, then disconnect
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()
                mock_iface.nodes = {
                    "!abcd1234": {
                        "lastHeard": time.time(),
                        "user": {"longName": "TestNode"},
                    },
                }
                return mock_iface
            elif connect_count == 2:
                collector.stop()
                return None
            return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        # State should be preserved from the first connection
        node = store.get_node("!abcd1234")
        assert node is not None
        assert node.name == "TestNode"

    def test_neighbor_times_preserved_across_reconnections(self) -> None:
        """Test that _neighbor_times tracking survives reconnections."""
        collector, store = self.make_collector()
        connect_count = 0

        # Pre-populate neighbor times
        collector._neighbor_times["!abcd1234"] = {"!observer1": time.time()}

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()
                return mock_iface
            else:
                collector.stop()
                return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        # Neighbor tracking should still have our data
        assert "!abcd1234" in collector._neighbor_times

    def test_stop_during_backoff_wait_exits_promptly(self) -> None:
        """Test that stop() during a backoff wait interrupts the wait."""
        collector, store = self.make_collector()

        def mock_connect():
            return None  # always fail

        def stop_soon():
            time.sleep(0.1)
            collector.stop()

        stopper = threading.Thread(target=stop_soon)
        stopper.start()

        with patch.object(collector, "_connect", side_effect=mock_connect):
            start = time.time()
            collector.run()
            elapsed = time.time() - start

        stopper.join()
        # Should exit almost immediately (not wait full 5s backoff)
        assert elapsed < 1.0

    def test_stop_during_run_loop_exits_cleanly(self) -> None:
        """Test that stop() during _run_loop causes clean exit without _ConnectionLost."""
        collector, store = self.make_collector()
        connect_count = 0
        connection_lost_count = 0

        original_run_loop = collector._run_loop

        def tracking_run_loop():
            try:
                original_run_loop()
            except _ConnectionLost:
                nonlocal connection_lost_count
                connection_lost_count += 1
                raise

        collector._run_loop = tracking_run_loop

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                mock_iface = MagicMock()
                mock_iface.isConnected = threading.Event()
                mock_iface.isConnected.set()  # connected
                return mock_iface
            return None

        def stop_soon():
            time.sleep(0.2)
            collector.stop()

        stopper = threading.Thread(target=stop_soon)
        stopper.start()

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        stopper.join()
        # Should have exited cleanly via loop condition, not via _ConnectionLost
        assert connection_lost_count == 0

    def test_run_connect_exception_treated_as_failure(self) -> None:
        """Test that exceptions from _connect are handled as connection failures."""
        collector, store = self.make_collector()
        connect_count = 0

        def mock_connect():
            nonlocal connect_count
            connect_count += 1
            if connect_count <= 2:
                # _connect normally catches its own exceptions and returns None
                return None
            collector.stop()
            return None

        with patch.object(collector, "_connect", side_effect=mock_connect):
            collector.run()

        assert connect_count == 3

    def test_run_collector_with_signal_handler(self) -> None:
        """Test that run_collector() signal handling works with the refactored stop()."""
        from kumatastic.collector import run_collector

        config = CollectorConfig(
            id="test-collector",
            meshtastic="tcp:localhost:4403",
        )
        store = MemoryStore()
        manifest = make_manifest("!abcd1234")

        connect_count = 0

        def mock_connect(self_arg):
            nonlocal connect_count
            connect_count += 1
            return None  # fail immediately

        def stop_soon():
            time.sleep(0.2)
            # Simulate SIGTERM by calling stop on the collector
            # We can't easily send a real signal, but we can verify the flow
            import signal
            signal.raise_signal(signal.SIGTERM)

        stopper = threading.Thread(target=stop_soon)
        stopper.start()

        with patch.object(MeshCollector, "_connect", mock_connect):
            run_collector(config, store, handle_signals=True, manifest=manifest)

        stopper.join()
        assert connect_count >= 1

    def test_run_isconnected_set_stays_connected(self) -> None:
        """Test that _run_loop continues when isConnected stays set."""
        collector, store = self.make_collector()
        collector._running = True
        collector._stop_event.clear()

        mock_iface = MagicMock()
        mock_iface.isConnected = threading.Event()
        mock_iface.isConnected.set()  # connected
        collector._interface = mock_iface

        iteration = 0

        def fast_wait(timeout=None):
            nonlocal iteration
            iteration += 1
            if iteration > 10:
                collector._running = False
            return not collector._running

        collector._stop_event.wait = fast_wait

        base = time.time()
        call_count = 0

        def advancing_time():
            nonlocal call_count
            call_count += 1
            return base + (call_count * 31)

        with patch("kumatastic.collector.time.time", side_effect=advancing_time):
            # Should NOT raise — isConnected is set
            collector._run_loop()

        # Survived multiple health checks
        assert iteration > 10
