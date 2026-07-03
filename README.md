# Polygon Zone

Polygon Zone is a custom Home Assistant integration that creates binary sensors from `Polygon` and `MultiPolygon` features in a GeoJSON file. Each sensor reports whether a tracked device is inside (or outside, when inverted) each configured zone.

## Features

- Supports GeoJSON `Polygon` and `MultiPolygon` geometry, including holes.
- Optional tolerance zones using accurate metre-based polygon offsetting.
- Fast point-in-polygon evaluation using prepared Shapely geometries.
- Optional GeoJSON file watching with automatic config-entry reload.
- Emits enter/exit events for automations.
- Each zone appears as its own device; zones named via a `name` (or `title`) feature property keep stable entity IDs even if the file is reordered.

## Requirements

- Home Assistant 2026.1.0 or newer.

## Installation

1. Copy this integration into your Home Assistant custom components folder as:
   `custom_components/poly_zone/`
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and choose **Polygon Zone**.

## Configuration

The initial setup flow asks for:

- **Name**: Friendly device name shown in Home Assistant.
- **GeoJSON path**: Absolute filesystem path to a local GeoJSON file that Home Assistant can read. For a file stored at `config/www/home_poly_zone.json` inside your HA config directory, enter `/config/www/home_poly_zone.json` (HA OS / Container / Supervised). On Core venv installs, use the real path, e.g. `/home/homeassistant/.homeassistant/www/home_poly_zone.json`. This is a filesystem path, not a URL like `/local/...`. The file extension does not matter as long as the contents are valid GeoJSON.
- **Device tracker**: A `device_tracker.*` entity to monitor.
- **Tolerance (meters)**: Optional expansion distance for a secondary diagnostic sensor.

### Options

After setup, you can tune:

- `tolerance`: Override tolerance distance.
- `invert`: Flip logic to treat outside as active.
- `watch_geojson`: Enable/disable periodic file change checks (mtime + size).
- `watch_interval`: Reload polling interval in seconds (minimum 5).

### Reconfigure

The name, GeoJSON path, device tracker and tolerance can be changed later
without removing the integration: open the entry's menu in
**Settings → Devices & Services** and choose **Reconfigure**.

## Events

The integration fires these events:

- `poly_zone_enter`
- `poly_zone_exit`

Event payload includes:

- `entity_id`
- `zone_name`
- `device_tracker`
- `in_zone`
- `lat`
- `lon`

## Notes on validation and reliability

- The config flow validates that the file exists and that at least one feature has supported geometry.
- GeoJSON coordinates are normalized to numeric longitude/latitude pairs before sensors are created.
- If the GeoJSON file cannot be read at startup (for example a network share that is not mounted yet), setup is retried automatically; a malformed file or one with no valid polygons puts the entry in an error state with the reason shown in the UI.
- Self-intersecting polygons are repaired automatically (a warning is logged).
- Polygons crossing the antimeridian (180° longitude) are not supported.

## Upgrading from 0.7.x

- Each zone now gets its own device (previously all zones shared one device whose name was arbitrary). After upgrading, an empty leftover device may remain; it can be deleted from the device page.
- Zones with a `name` (or `title`) property now derive their entity unique IDs from that name instead of their position in the file. Entities for *named* zones will therefore be created fresh once (history under the old entity IDs is not carried over); unnamed zones keep their previous IDs.
