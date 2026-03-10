"""Configuration loading and validation for Kumatastic.

Handles loading config from YAML files with sensible defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Optional YAML import
try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# Default values
DEFAULT_STATE_PATH = "/var/lib/kumatastic/state.json"
DEFAULT_OFFLINE_THRESHOLD = 23400  # 6.5 hours
DEFAULT_PUSH_INTERVAL = 600  # 10 minutes
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_NEIGHBOR_MAX_AGE = 14400  # 4 hours

# Kuma monitor defaults
DEFAULT_MAXRETRIES = 6
DEFAULT_MONITOR_INTERVAL_MULTIPLIER = 6
DEFAULT_MONITOR_RETRY_MULTIPLIER = 3


@dataclass
class CollectorConfig:
    """Configuration for a collector instance."""

    id: str
    meshtastic: str  # Connection string: "tcp:host:port" or "serial:/dev/ttyUSB0"
    state_path: str = DEFAULT_STATE_PATH
    neighbor_max_age: int = DEFAULT_NEIGHBOR_MAX_AGE
    manifest_path: str = "nodes.yaml"
    pusher_urls: list[str] = field(default_factory=list)
    sighting_token: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectorConfig:
        sighting_token = data.get("sighting_token", "") or os.environ.get(
            "KUMATASTIC_SIGHTING_TOKEN", ""
        )
        return cls(
            id=data.get("id", "collector-default"),
            meshtastic=data.get("meshtastic", ""),
            state_path=data.get("state_path", DEFAULT_STATE_PATH),
            neighbor_max_age=data.get("neighbor_max_age", DEFAULT_NEIGHBOR_MAX_AGE),
            manifest_path=data.get("manifest_path", "nodes.yaml"),
            pusher_urls=data.get("pusher_urls", []),
            sighting_token=sighting_token,
        )


@dataclass
class KumaTarget:
    """Configuration for an Uptime Kuma target instance."""

    name: str
    url: str
    username: str = ""
    password: str = ""
    default_tag: str = "auto"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KumaTarget:
        return cls(
            name=data.get("name", "default"),
            url=data.get("url", ""),
            username=data.get("username", ""),
            password=data.get("password", ""),
            default_tag=data.get("default_tag", "auto"),
        )


@dataclass
class PusherConfig:
    """Configuration for the pusher component."""

    state_path: str = DEFAULT_STATE_PATH
    offline_threshold: int = DEFAULT_OFFLINE_THRESHOLD
    push_interval: int = DEFAULT_PUSH_INTERVAL
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    maxretries: int = DEFAULT_MAXRETRIES
    monitor_interval_multiplier: int = DEFAULT_MONITOR_INTERVAL_MULTIPLIER
    monitor_retry_multiplier: int = DEFAULT_MONITOR_RETRY_MULTIPLIER
    manifest_path: str = "nodes.yaml"
    push_secret: str = ""
    listen: str = ""
    sighting_token: str = ""
    targets: list[KumaTarget] = field(default_factory=list)

    @property
    def distributed_mode(self) -> bool:
        """True when push_secret is configured (distributed push mode)."""
        return bool(self.push_secret)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PusherConfig:
        targets = [
            KumaTarget.from_dict(t) for t in data.get("targets", [])
        ]
        # push_secret: config value, then env var fallback
        push_secret = data.get("push_secret", "") or os.environ.get("KUMATASTIC_SECRET", "")
        sighting_token = data.get("sighting_token", "") or os.environ.get(
            "KUMATASTIC_SIGHTING_TOKEN", ""
        )
        return cls(
            state_path=data.get("state_path", DEFAULT_STATE_PATH),
            offline_threshold=data.get("offline_threshold", DEFAULT_OFFLINE_THRESHOLD),
            push_interval=data.get("push_interval", DEFAULT_PUSH_INTERVAL),
            request_timeout=data.get("request_timeout", DEFAULT_REQUEST_TIMEOUT),
            maxretries=data.get("maxretries", DEFAULT_MAXRETRIES),
            monitor_interval_multiplier=data.get(
                "monitor_interval_multiplier", DEFAULT_MONITOR_INTERVAL_MULTIPLIER
            ),
            monitor_retry_multiplier=data.get(
                "monitor_retry_multiplier", DEFAULT_MONITOR_RETRY_MULTIPLIER
            ),
            manifest_path=data.get("manifest_path", "nodes.yaml"),
            push_secret=push_secret,
            listen=data.get("listen", ""),
            sighting_token=sighting_token,
            targets=targets,
        )


@dataclass
class Config:
    """Complete Kumatastic configuration."""

    collector: CollectorConfig | None = None
    pusher: PusherConfig | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        collector = None
        pusher = None

        if "collector" in data:
            collector = CollectorConfig.from_dict(data["collector"])

        if "pusher" in data:
            pusher = PusherConfig.from_dict(data["pusher"])

        return cls(collector=collector, pusher=pusher)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        """Load configuration from a YAML file.

        Args:
            path: Path to the YAML config file

        Returns:
            Loaded Config instance.

        Raises:
            FileNotFoundError: If the config file doesn't exist.
            ImportError: If PyYAML is not installed.
        """
        if not YAML_AVAILABLE:
            raise ImportError(
                "PyYAML is required for config loading. "
                "Install with: pip install pyyaml"
            )

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)


def find_config_file() -> Path | None:
    """Find the config file in standard locations.

    Searches in order:
    1. ./kumatastic.yaml (current directory)
    2. ~/.config/kumatastic/kumatastic.yaml (user config)
    3. /etc/kumatastic/kumatastic.yaml (system config)

    Returns:
        Path to the first config file found, or None if not found.
    """
    search_paths = [
        Path("kumatastic.yaml"),
        Path.home() / ".config" / "kumatastic" / "kumatastic.yaml",
        Path("/etc/kumatastic/kumatastic.yaml"),
    ]

    for path in search_paths:
        if path.exists():
            return path

    return None


def get_state_path_from_config(config: Config) -> str:
    """Get the state path from config, preferring collector if both exist."""
    if config.collector:
        return config.collector.state_path
    if config.pusher:
        return config.pusher.state_path
    return DEFAULT_STATE_PATH
