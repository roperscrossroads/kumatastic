"""Tests for the manifest module."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kumatastic.manifest import (
    Manifest,
    ManifestNode,
    ReloadableManifest,
    _normalize_node_id,
    create_manifest,
    derive_push_token,
    load_manifest,
    load_manifest_from_url,
)


class TestNormalizeNodeId:
    """Tests for node ID normalization."""

    def test_already_normalized(self) -> None:
        assert _normalize_node_id("!abcd1234") == "!abcd1234"

    def test_missing_prefix(self) -> None:
        assert _normalize_node_id("abcd1234") == "!abcd1234"

    def test_uppercase(self) -> None:
        assert _normalize_node_id("!ABCD1234") == "!abcd1234"

    def test_whitespace(self) -> None:
        assert _normalize_node_id("  !abcd1234  ") == "!abcd1234"


class TestDerivePushToken:
    """Tests for derive_push_token()."""

    def test_deterministic(self) -> None:
        """Same secret + node ID always produces the same token."""
        t1 = derive_push_token("mysecret", "!abcd1234")
        t2 = derive_push_token("mysecret", "!abcd1234")
        assert t1 == t2

    def test_normalizes_node_id(self) -> None:
        """Node ID format variations produce the same token."""
        t1 = derive_push_token("secret", "!ABCD1234")
        t2 = derive_push_token("secret", "abcd1234")
        t3 = derive_push_token("secret", "  !abcd1234  ")
        assert t1 == t2 == t3

    def test_different_secrets_differ(self) -> None:
        """Different secrets produce different tokens."""
        t1 = derive_push_token("secret-a", "!abcd1234")
        t2 = derive_push_token("secret-b", "!abcd1234")
        assert t1 != t2

    def test_different_nodes_differ(self) -> None:
        """Different node IDs produce different tokens."""
        t1 = derive_push_token("secret", "!abcd1234")
        t2 = derive_push_token("secret", "!ef567890")
        assert t1 != t2

    def test_token_format(self) -> None:
        """Token has expected format: mesh-<hex_node_id>-<16char_hmac>."""
        token = derive_push_token("secret", "!abcd1234")
        assert token.startswith("mesh-abcd1234-")
        parts = token.split("-")
        assert len(parts) == 3
        assert len(parts[2]) == 16  # 16 hex chars from HMAC


class TestManifest:
    """Tests for Manifest dataclass."""

    def test_contains_exact(self) -> None:
        manifest = Manifest(nodes={
            "!abcd1234": ManifestNode(node_id="!abcd1234", name="Test"),
        })
        assert manifest.contains("!abcd1234")

    def test_contains_normalizes(self) -> None:
        manifest = Manifest(nodes={
            "!abcd1234": ManifestNode(node_id="!abcd1234", name="Test"),
        })
        assert manifest.contains("abcd1234")
        assert manifest.contains("!ABCD1234")

    def test_not_contains(self) -> None:
        manifest = Manifest(nodes={
            "!abcd1234": ManifestNode(node_id="!abcd1234", name="Test"),
        })
        assert not manifest.contains("!99999999")

    def test_len(self) -> None:
        manifest = Manifest(nodes={
            "!a": ManifestNode(node_id="!a", name="A"),
            "!b": ManifestNode(node_id="!b", name="B"),
        })
        assert len(manifest) == 2


class TestLoadManifest:
    """Tests for load_manifest()."""

    def test_load_valid(self) -> None:
        yaml_content = """
nodes:
  "!abcd1234":
    name: "Test Node"
    tags: ["core", "infra"]
  "!ef567890":
    name: "Other Node"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            manifest = load_manifest(path)

            assert len(manifest) == 2
            assert manifest.contains("!abcd1234")
            assert manifest.contains("!ef567890")
            assert manifest.nodes["!abcd1234"].name == "Test Node"
            assert manifest.nodes["!abcd1234"].tags == ["core", "infra"]
            assert manifest.nodes["!ef567890"].tags == ["auto"]  # default

    def test_load_normalizes_ids(self) -> None:
        yaml_content = """
nodes:
  "ABCD1234":
    name: "No prefix, uppercase"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            manifest = load_manifest(path)

            assert manifest.contains("!abcd1234")

    def test_load_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_manifest("/nonexistent/nodes.yaml")

    def test_load_missing_nodes_key(self) -> None:
        yaml_content = "something_else: true\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            with pytest.raises(ValueError, match="nodes"):
                load_manifest(path)

    def test_load_missing_name(self) -> None:
        yaml_content = """
nodes:
  "!abcd1234":
    tags: ["core"]
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            with pytest.raises(ValueError, match="name"):
                load_manifest(path)

    def test_load_invalid_node_entry(self) -> None:
        yaml_content = """
nodes:
  "!abcd1234": "just a string"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            with pytest.raises(ValueError, match="mapping"):
                load_manifest(path)

    def test_load_invalid_tags(self) -> None:
        yaml_content = """
nodes:
  "!abcd1234":
    name: "Test"
    tags: "not a list"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            with pytest.raises(ValueError, match="tags"):
                load_manifest(path)

    def test_load_empty_nodes(self) -> None:
        yaml_content = """
nodes: {}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(yaml_content)

            manifest = load_manifest(path)
            assert len(manifest) == 0


class TestManifestNode:
    """Tests for ManifestNode dataclass."""

    def test_defaults(self) -> None:
        node = ManifestNode(node_id="!abc", name="Test")
        assert node.tags == ["auto"]

    def test_custom_tags(self) -> None:
        node = ManifestNode(node_id="!abc", name="Test", tags=["core"])
        assert node.tags == ["core"]


SAMPLE_YAML = """
nodes:
  "!abcd1234":
    name: "Node Alpha"
    tags: ["core"]
  "!ef567890":
    name: "Node Beta"
"""


class TestLoadManifestFromUrl:
    """Tests for load_manifest_from_url()."""

    def test_load_from_url(self) -> None:
        """Test loading manifest from a URL."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_YAML
        mock_resp.raise_for_status = MagicMock()

        with patch("kumatastic.manifest.requests.get", return_value=mock_resp) as mock_get:
            manifest = load_manifest_from_url("https://example.com/nodes.yaml")

        mock_get.assert_called_once_with("https://example.com/nodes.yaml", timeout=15)
        assert len(manifest) == 2
        assert manifest.contains("!abcd1234")
        assert manifest.nodes["!abcd1234"].name == "Node Alpha"

    def test_load_from_url_http_error(self) -> None:
        """Test that HTTP errors propagate."""
        import requests
        with patch("kumatastic.manifest.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("404")
            with pytest.raises(requests.RequestException):
                load_manifest_from_url("https://example.com/bad")

    def test_load_from_url_invalid_yaml(self) -> None:
        """Test that invalid manifest content raises ValueError."""
        mock_resp = MagicMock()
        mock_resp.text = "not_nodes: true\n"
        mock_resp.raise_for_status = MagicMock()

        with patch("kumatastic.manifest.requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="nodes"):
                load_manifest_from_url("https://example.com/bad.yaml")


class TestReloadableManifest:
    """Tests for ReloadableManifest."""

    def test_initial_load(self) -> None:
        """Test that initial load works and exposes manifest interface."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_YAML
        mock_resp.raise_for_status = MagicMock()

        with patch("kumatastic.manifest.requests.get", return_value=mock_resp):
            rm = ReloadableManifest("https://example.com/nodes.yaml", reload_interval=9999)

        assert len(rm) == 2
        assert rm.contains("!abcd1234")
        assert rm.contains("!ef567890")
        assert "!abcd1234" in rm.nodes
        rm.stop()

    def test_initial_load_failure_raises(self) -> None:
        """Test that initial load failure raises (fail fast at startup)."""
        import requests
        with patch("kumatastic.manifest.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("DNS failure")
            with pytest.raises(requests.RequestException):
                ReloadableManifest("https://bad.example.com/nodes.yaml")

    def test_reload_updates_manifest(self) -> None:
        """Test that background reload picks up changes."""
        yaml_v1 = SAMPLE_YAML
        yaml_v2 = """
nodes:
  "!abcd1234":
    name: "Node Alpha"
  "!ef567890":
    name: "Node Beta"
  "!11111111":
    name: "Node Gamma"
"""
        call_count = 0

        def mock_get(url, timeout=15):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.text = yaml_v1 if call_count == 1 else yaml_v2
            resp.raise_for_status = MagicMock()
            return resp

        with patch("kumatastic.manifest.requests.get", side_effect=mock_get):
            rm = ReloadableManifest("https://example.com/nodes.yaml", reload_interval=0.1)
            assert len(rm) == 2

            # Wait for reload
            time.sleep(0.4)

            assert len(rm) == 3
            assert rm.contains("!11111111")
            rm.stop()

    def test_reload_failure_keeps_previous(self) -> None:
        """Test that reload failure preserves the previous manifest."""
        import requests as req_lib

        call_count = 0

        def mock_get(url, timeout=15):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = MagicMock()
                resp.text = SAMPLE_YAML
                resp.raise_for_status = MagicMock()
                return resp
            raise req_lib.RequestException("Network down")

        with patch("kumatastic.manifest.requests.get", side_effect=mock_get):
            rm = ReloadableManifest("https://example.com/nodes.yaml", reload_interval=0.1)
            assert len(rm) == 2

            # Wait for failed reload
            time.sleep(0.4)

            # Should still have the original manifest
            assert len(rm) == 2
            assert rm.contains("!abcd1234")
            rm.stop()


class TestCreateManifest:
    """Tests for create_manifest() factory."""

    def test_creates_manifest_for_file(self) -> None:
        """Test that file paths return a static Manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nodes.yaml"
            path.write_text(SAMPLE_YAML)

            result = create_manifest(str(path))
            assert isinstance(result, Manifest)
            assert len(result) == 2

    def test_creates_reloadable_for_https_url(self) -> None:
        """Test that HTTPS URLs return a ReloadableManifest."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_YAML
        mock_resp.raise_for_status = MagicMock()

        with patch("kumatastic.manifest.requests.get", return_value=mock_resp):
            result = create_manifest("https://raw.githubusercontent.com/user/repo/main/nodes.yaml")
            assert isinstance(result, ReloadableManifest)
            assert len(result) == 2
            result.stop()

    def test_creates_reloadable_for_http_url(self) -> None:
        """Test that HTTP URLs return a ReloadableManifest."""
        mock_resp = MagicMock()
        mock_resp.text = SAMPLE_YAML
        mock_resp.raise_for_status = MagicMock()

        with patch("kumatastic.manifest.requests.get", return_value=mock_resp):
            result = create_manifest("http://internal.host/nodes.yaml")
            assert isinstance(result, ReloadableManifest)
            result.stop()
