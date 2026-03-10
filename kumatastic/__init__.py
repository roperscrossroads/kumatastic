"""Kumatastic - Decoupled Meshtastic node monitoring with Uptime Kuma integration.

This package provides a collector/pusher architecture for monitoring Meshtastic
mesh network nodes via Uptime Kuma. The collector handles Meshtastic device
connections and writes sightings to a state store. The pusher reads from the
state store and pushes status to Uptime Kuma instances.

Components:
    - collector: Meshtastic device connection and sighting collection
    - pusher: Uptime Kuma status pushing
    - state: State store abstraction (JSON file, Redis, etc.)
    - config: Configuration loading and validation
    - cli: Command-line interface
"""

__version__ = "0.1.0"
