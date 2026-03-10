"""Node manifest — declares which nodes to track.

The manifest is the single source of truth for monitored nodes.
Only nodes listed in the manifest are collected and pushed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_RELOAD_INTERVAL = 1800  # 30 minutes


@dataclass
class ManifestNode:
    """A declared node in the manifest."""

    node_id: str  # normalized "!abcd1234"
    name: str
    tags: list[str] = field(default_factory=lambda: ["auto"])


@dataclass
class Manifest:
    """Collection of declared nodes."""

    nodes: dict[str, ManifestNode]  # node_id -> ManifestNode

    def contains(self, node_id: str) -> bool:
        """Check if a node ID is in the manifest."""
        normalized = _normalize_node_id(node_id)
        return normalized in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)


def derive_push_token(secret: str, node_id: str) -> str:
    """Derive a deterministic push token from a shared secret and node ID.

    All pushers with the same secret derive the same token for a given node,
    enabling distributed push without shared state.

    Args:
        secret: Shared secret string.
        node_id: Meshtastic node ID (any format, will be normalized).

    Returns:
        Deterministic push token string like "mesh-69859178-a8f3b2c1e9d7046f".
    """
    normalized = node_id.strip().lower().lstrip("!")
    mac = hmac.new(secret.encode(), normalized.encode(), hashlib.sha256).hexdigest()[:16]
    return f"mesh-{normalized}-{mac}"


def _normalize_node_id(node_id: str) -> str:
    """Normalize a node ID to '!abcd1234' format."""
    node_id = node_id.strip()
    if not node_id.startswith("!"):
        node_id = f"!{node_id}"
    return node_id.lower()


def _parse_manifest_yaml(data: dict[str, Any]) -> dict[str, ManifestNode]:
    """Parse manifest YAML data into ManifestNode dict.

    Args:
        data: Parsed YAML dict.

    Returns:
        Dict of node_id -> ManifestNode.

    Raises:
        ValueError: If the manifest format is invalid.
    """
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict):
        raise ValueError("Manifest must contain a 'nodes' dict")

    nodes: dict[str, ManifestNode] = {}
    for raw_id, info in raw_nodes.items():
        node_id = _normalize_node_id(str(raw_id))

        if not isinstance(info, dict):
            raise ValueError(f"Node {raw_id}: expected a mapping, got {type(info).__name__}")

        name = info.get("name")
        if not name:
            raise ValueError(f"Node {raw_id}: 'name' is required")

        tags = info.get("tags", ["auto"])
        if not isinstance(tags, list):
            raise ValueError(f"Node {raw_id}: 'tags' must be a list")

        nodes[node_id] = ManifestNode(node_id=node_id, name=str(name), tags=tags)

    return nodes


def load_manifest(path: str | Path) -> Manifest:
    """Load a node manifest from a YAML file.

    Args:
        path: Path to the manifest YAML file.

    Returns:
        Loaded Manifest instance.

    Raises:
        FileNotFoundError: If the manifest file doesn't exist.
        ImportError: If PyYAML is not installed.
        ValueError: If the manifest format is invalid.
    """
    if not YAML_AVAILABLE:
        raise ImportError(
            "PyYAML is required for manifest loading. "
            "Install with: pip install pyyaml"
        )

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    return Manifest(nodes=_parse_manifest_yaml(data))


def load_manifest_from_url(url: str) -> Manifest:
    """Load a node manifest from an HTTP(S) URL.

    Args:
        url: URL to fetch (e.g. raw GitHub URL).

    Returns:
        Loaded Manifest instance.

    Raises:
        ImportError: If PyYAML is not installed.
        requests.RequestException: On HTTP errors.
        ValueError: If the manifest format is invalid.
    """
    if not YAML_AVAILABLE:
        raise ImportError(
            "PyYAML is required for manifest loading. "
            "Install with: pip install pyyaml"
        )

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = yaml.safe_load(resp.text) or {}

    return Manifest(nodes=_parse_manifest_yaml(data))


class ReloadableManifest:
    """A manifest that periodically reloads from a URL.

    Drop-in replacement for Manifest — exposes the same interface
    (contains, nodes, __len__) but refreshes in the background.

    On reload failure the previous manifest is kept.
    """

    def __init__(
        self,
        url: str,
        reload_interval: int = DEFAULT_MANIFEST_RELOAD_INTERVAL,
    ) -> None:
        self._url = url
        self._reload_interval = reload_interval

        # Load initial manifest (raises on failure — fail fast at startup)
        self._manifest = load_manifest_from_url(url)
        self._lock = threading.Lock()
        self._last_load = time.time()

        logger.info(
            f"Loaded manifest from {url}: {len(self._manifest)} nodes "
            f"(reload every {reload_interval}s)"
        )

        # Start background reload thread
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._reload_loop, daemon=True)
        self._thread.start()

    @property
    def nodes(self) -> dict[str, ManifestNode]:
        with self._lock:
            return self._manifest.nodes

    def contains(self, node_id: str) -> bool:
        with self._lock:
            return self._manifest.contains(node_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._manifest)

    def _reload_loop(self) -> None:
        """Background loop that reloads the manifest periodically."""
        while not self._stop_event.wait(timeout=self._reload_interval):
            try:
                new_manifest = load_manifest_from_url(self._url)
                with self._lock:
                    old_count = len(self._manifest)
                    self._manifest = new_manifest
                    self._last_load = time.time()

                new_count = len(new_manifest)
                if new_count != old_count:
                    logger.info(
                        f"Manifest reloaded: {old_count} -> {new_count} nodes"
                    )
                else:
                    logger.debug(f"Manifest reloaded: {new_count} nodes (unchanged)")

            except Exception as e:
                logger.warning(f"Manifest reload failed (keeping previous): {e}")

    def stop(self) -> None:
        """Stop the reload thread."""
        self._stop_event.set()


def create_manifest(
    source: str,
    reload_interval: int = DEFAULT_MANIFEST_RELOAD_INTERVAL,
) -> Manifest | ReloadableManifest:
    """Create a manifest from a file path or URL.

    If source starts with http:// or https://, returns a ReloadableManifest
    that refreshes every reload_interval seconds. Otherwise loads from a
    local file (one-shot, no reload).

    Args:
        source: File path or URL.
        reload_interval: Seconds between reloads (URL sources only).

    Returns:
        Manifest or ReloadableManifest.
    """
    if source.startswith("http://") or source.startswith("https://"):
        return ReloadableManifest(source, reload_interval=reload_interval)
    return load_manifest(source)
