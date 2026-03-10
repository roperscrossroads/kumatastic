# Tuning Guide

## Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `offline_threshold` | 23400s (6.5h) | Time since last sighting before a node is considered DOWN |
| `push_interval` | 600s (10min) | How often the pusher reports status to Kuma |
| `monitor_interval_multiplier` | 6 | Kuma monitor interval = push_interval x this |
| `monitor_retry_multiplier` | 3 | Kuma retry interval = push_interval x this |
| `maxretries` | 6 | Heartbeats in PENDING before Kuma confirms DOWN |
| `neighbor_max_age` | 14400s (4h) | How long to keep NeighborInfo sightings |

## Offline Threshold

This is the most important parameter to get right.

**Too low:** False DOWNs from normal mesh timing variability. Meshtastic nodes transmit position/telemetry every 15-60 minutes, and NeighborInfo broadcasts arrive roughly once per hour. If your threshold is lower than the node's longest normal silence interval, you'll see a "sawtooth" pattern — UP, DOWN, UP, DOWN.

**Too high:** Slow detection of actual failures. A node that dies won't be reported as DOWN for hours.

**Recommended:** 6.5 hours (23400s). This was determined by analyzing 77,715 heartbeat intervals from a production mesh network. At this threshold, the false DOWN rate dropped to near zero while still detecting genuine outages within a reasonable window.

**How to tune for your mesh:** Look at the longest gap between transmissions for your most active nodes. Set the threshold to at least 2x that value.

## Push Interval

Controls how often the pusher sends status updates to Kuma.

**Shorter** = faster UP/DOWN transitions, but more API calls to Kuma. Going below 60 seconds is unlikely to help — mesh nodes don't transmit that frequently.

**Longer** = less load on Kuma, but slower to reflect state changes.

**Recommended:** 10 minutes (600s). This balances responsiveness with API load.

## Monitor Interval Multiplier

The Kuma monitor interval must be significantly longer than the push interval. If they're close, processing delays and network jitter cause the push to arrive slightly late, and Kuma briefly marks the monitor as PENDING — creating a flapping pattern.

**Recommended:** 6x push interval. With a 10-minute push cycle, the Kuma monitor interval is 60 minutes. This gives plenty of room for jitter.

## NeighborInfo Max Age

NeighborInfo packets are broadcast roughly once per hour. A node appearing in another node's neighbor list proves it's alive, but this data goes stale. The `neighbor_max_age` controls how long to keep these sightings.

**Recommended:** 4 hours (14400s). This catches nodes that only appear via NeighborInfo (no direct packets) while not keeping stale data too long.

## Common Patterns and Fixes

### Sawtooth (UP/DOWN/UP/DOWN cycling)

**Cause:** `offline_threshold` is shorter than the node's normal transmission interval.

**Fix:** Increase `offline_threshold`. For most Meshtastic networks, 6.5 hours works well.

### Flapping (brief PENDING states on Kuma)

**Cause:** `monitor_interval_multiplier` too low, or push interval too close to Kuma's check interval.

**Fix:** Increase `monitor_interval_multiplier` to 6 or higher.

### Alert fatigue (too many DOWN notifications)

**Cause:** Threshold too aggressive, or monitoring nodes that aren't reliable.

**Fix:** Increase `offline_threshold` and/or remove intermittent nodes from the manifest. False DOWN is worse than false UP — when everything alerts, nothing alerts.
