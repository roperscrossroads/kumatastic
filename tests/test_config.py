"""Tests for the config module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kumatastic.config import (
    CollectorConfig,
    Config,
    KumaTarget,
    PusherConfig,
    DEFAULT_OFFLINE_THRESHOLD,
    DEFAULT_PUSH_INTERVAL,
    DEFAULT_STATE_PATH,
    find_config_file,
    get_state_path_from_config,
)


class TestCollectorConfig:
    """Tests for CollectorConfig."""

    def test_from_dict(self) -> None:
        """Test creating CollectorConfig from dict."""
        data = {
            "id": "collector-1",
            "meshtastic": "tcp:192.168.1.100:4403",
            "state_path": "/tmp/state.json",
        }
        config = CollectorConfig.from_dict(data)

        assert config.id == "collector-1"
        assert config.meshtastic == "tcp:192.168.1.100:4403"
        assert config.state_path == "/tmp/state.json"

    def test_from_dict_defaults(self) -> None:
        """Test CollectorConfig defaults."""
        data = {
            "id": "collector-1",
            "meshtastic": "tcp:host",
        }
        config = CollectorConfig.from_dict(data)

        assert config.state_path == DEFAULT_STATE_PATH
        assert config.neighbor_max_age == 14400
        assert config.manifest_path == "nodes.yaml"

    def test_from_dict_manifest_path(self) -> None:
        """Test custom manifest_path."""
        data = {
            "id": "collector-1",
            "meshtastic": "tcp:host",
            "manifest_path": "/etc/kumatastic/nodes.yaml",
        }
        config = CollectorConfig.from_dict(data)
        assert config.manifest_path == "/etc/kumatastic/nodes.yaml"

    def test_from_dict_pusher_urls(self) -> None:
        """Test pusher_urls field."""
        data = {
            "id": "collector-1",
            "meshtastic": "tcp:host",
            "pusher_urls": ["http://localhost:9100", "https://remote:9100"],
        }
        config = CollectorConfig.from_dict(data)
        assert config.pusher_urls == ["http://localhost:9100", "https://remote:9100"]

    def test_from_dict_pusher_urls_default(self) -> None:
        """Test pusher_urls defaults to empty list."""
        config = CollectorConfig.from_dict({"id": "c", "meshtastic": "tcp:host"})
        assert config.pusher_urls == []

    def test_from_dict_sighting_token(self) -> None:
        """Test sighting_token field."""
        data = {
            "id": "collector-1",
            "meshtastic": "tcp:host",
            "sighting_token": "my-token",
        }
        config = CollectorConfig.from_dict(data)
        assert config.sighting_token == "my-token"

    def test_sighting_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test sighting_token falls back to KUMATASTIC_SIGHTING_TOKEN env var."""
        monkeypatch.setenv("KUMATASTIC_SIGHTING_TOKEN", "env-token")
        config = CollectorConfig.from_dict({"id": "c", "meshtastic": "tcp:host"})
        assert config.sighting_token == "env-token"

    def test_sighting_token_config_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config sighting_token takes precedence over env var."""
        monkeypatch.setenv("KUMATASTIC_SIGHTING_TOKEN", "env-token")
        data = {"id": "c", "meshtastic": "tcp:host", "sighting_token": "config-token"}
        config = CollectorConfig.from_dict(data)
        assert config.sighting_token == "config-token"


class TestKumaTarget:
    """Tests for KumaTarget."""

    def test_from_dict(self) -> None:
        """Test creating KumaTarget from dict."""
        data = {
            "name": "internal",
            "url": "http://kuma:3001",
            "username": "admin",
            "password": "secret",
            "default_tag": "stable",
        }
        target = KumaTarget.from_dict(data)

        assert target.name == "internal"
        assert target.url == "http://kuma:3001"
        assert target.username == "admin"
        assert target.password == "secret"
        assert target.default_tag == "stable"

    def test_from_dict_defaults(self) -> None:
        """Test KumaTarget defaults."""
        data = {
            "name": "default",
            "url": "http://kuma:3001",
        }
        target = KumaTarget.from_dict(data)

        assert target.default_tag == "auto"


class TestPusherConfig:
    """Tests for PusherConfig."""

    def test_from_dict(self) -> None:
        """Test creating PusherConfig from dict."""
        data = {
            "state_path": "/tmp/state.json",
            "offline_threshold": 7200,
            "push_interval": 300,
            "targets": [
                {"name": "t1", "url": "http://k1:3001"},
                {"name": "t2", "url": "http://k2:3001"},
            ],
        }
        config = PusherConfig.from_dict(data)

        assert config.state_path == "/tmp/state.json"
        assert config.offline_threshold == 7200
        assert config.push_interval == 300
        assert len(config.targets) == 2
        assert config.targets[0].name == "t1"
        assert config.targets[1].name == "t2"

    def test_from_dict_defaults(self) -> None:
        """Test PusherConfig defaults."""
        data = {}
        config = PusherConfig.from_dict(data)

        assert config.state_path == DEFAULT_STATE_PATH
        assert config.offline_threshold == DEFAULT_OFFLINE_THRESHOLD
        assert config.push_interval == DEFAULT_PUSH_INTERVAL
        assert config.manifest_path == "nodes.yaml"
        assert config.targets == []

    def test_from_dict_manifest_path(self) -> None:
        """Test custom manifest_path."""
        data = {"manifest_path": "/etc/kumatastic/nodes.yaml"}
        config = PusherConfig.from_dict(data)
        assert config.manifest_path == "/etc/kumatastic/nodes.yaml"

    def test_from_dict_listen(self) -> None:
        """Test listen field."""
        data = {"listen": "0.0.0.0:9100"}
        config = PusherConfig.from_dict(data)
        assert config.listen == "0.0.0.0:9100"

    def test_from_dict_listen_default(self) -> None:
        """Test listen defaults to empty string."""
        config = PusherConfig.from_dict({})
        assert config.listen == ""

    def test_from_dict_sighting_token(self) -> None:
        """Test sighting_token field."""
        data = {"sighting_token": "my-token"}
        config = PusherConfig.from_dict(data)
        assert config.sighting_token == "my-token"

    def test_sighting_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test pusher sighting_token falls back to env var."""
        monkeypatch.setenv("KUMATASTIC_SIGHTING_TOKEN", "env-token")
        config = PusherConfig.from_dict({})
        assert config.sighting_token == "env-token"


class TestPusherConfigPushSecret:
    """Tests for PusherConfig push_secret and distributed_mode."""

    def test_push_secret_from_dict(self) -> None:
        """Test push_secret loaded from config dict."""
        data = {"push_secret": "my-shared-secret"}
        config = PusherConfig.from_dict(data)
        assert config.push_secret == "my-shared-secret"
        assert config.distributed_mode is True

    def test_push_secret_default_empty(self) -> None:
        """Test push_secret defaults to empty string."""
        config = PusherConfig.from_dict({})
        assert config.push_secret == ""
        assert config.distributed_mode is False

    def test_push_secret_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test push_secret falls back to KUMATASTIC_SECRET env var."""
        monkeypatch.setenv("KUMATASTIC_SECRET", "env-secret")
        config = PusherConfig.from_dict({})
        assert config.push_secret == "env-secret"
        assert config.distributed_mode is True

    def test_push_secret_config_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config value takes precedence over env var."""
        monkeypatch.setenv("KUMATASTIC_SECRET", "env-secret")
        data = {"push_secret": "config-secret"}
        config = PusherConfig.from_dict(data)
        assert config.push_secret == "config-secret"


class TestConfig:
    """Tests for Config."""

    def test_from_dict_empty(self) -> None:
        """Test creating Config from empty dict."""
        config = Config.from_dict({})
        assert config.collector is None
        assert config.pusher is None

    def test_from_dict_full(self) -> None:
        """Test creating Config from full dict."""
        data = {
            "collector": {
                "id": "c1",
                "meshtastic": "tcp:host",
            },
            "pusher": {
                "targets": [{"name": "t1", "url": "http://k:3001"}],
            },
        }
        config = Config.from_dict(data)

        assert config.collector is not None
        assert config.collector.id == "c1"
        assert config.pusher is not None
        assert len(config.pusher.targets) == 1

    def test_load_from_file(self) -> None:
        """Test loading config from YAML file."""
        yaml_content = """
collector:
  id: file-collector
  meshtastic: tcp:192.168.1.1:4403

pusher:
  offline_threshold: 7200
  targets:
    - name: internal
      url: http://kuma:3001
      username: admin
      password: secret
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(yaml_content)

            config = Config.load(path)

            assert config.collector is not None
            assert config.collector.id == "file-collector"
            assert config.collector.meshtastic == "tcp:192.168.1.1:4403"

            assert config.pusher is not None
            assert config.pusher.offline_threshold == 7200
            assert len(config.pusher.targets) == 1
            assert config.pusher.targets[0].name == "internal"

    def test_load_file_not_found(self) -> None:
        """Test loading non-existent config file raises error."""
        with pytest.raises(FileNotFoundError):
            Config.load("/nonexistent/path/config.yaml")


class TestFindConfigFile:
    """Tests for find_config_file()."""

    def test_finds_config_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test finding kumatastic.yaml in the current directory."""
        config_file = tmp_path / "kumatastic.yaml"
        config_file.write_text("collector:\n  id: test\n")
        monkeypatch.chdir(tmp_path)

        result = find_config_file()
        assert result is not None
        assert result.name == "kumatastic.yaml"

    def test_returns_none_when_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test returns None when no config file exists anywhere."""
        monkeypatch.chdir(tmp_path)
        # Override home to avoid finding real user config
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))

        result = find_config_file()
        assert result is None

    def test_finds_user_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test finding config in ~/.config/kumatastic/."""
        # Ensure no cwd config
        monkeypatch.chdir(tmp_path)
        # Set up user config dir
        user_config = tmp_path / ".config" / "kumatastic"
        user_config.mkdir(parents=True)
        (user_config / "kumatastic.yaml").write_text("pusher: {}\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = find_config_file()
        assert result is not None
        assert "config" in str(result)


class TestGetStatePath:
    """Tests for get_state_path_from_config."""

    def test_prefers_collector(self) -> None:
        """Test that collector path is preferred."""
        config = Config(
            collector=CollectorConfig(
                id="c1",
                meshtastic="tcp:host",
                state_path="/collector/path",
            ),
            pusher=PusherConfig(state_path="/pusher/path"),
        )
        assert get_state_path_from_config(config) == "/collector/path"

    def test_falls_back_to_pusher(self) -> None:
        """Test fallback to pusher path."""
        config = Config(
            pusher=PusherConfig(state_path="/pusher/path"),
        )
        assert get_state_path_from_config(config) == "/pusher/path"

    def test_default_when_empty(self) -> None:
        """Test default path when config is empty."""
        config = Config()
        assert get_state_path_from_config(config) == DEFAULT_STATE_PATH
