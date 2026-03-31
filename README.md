# Polygon Zone

Polygon Zone is a custom Home Assistant integration that creates binary sensors from `Polygon` and `MultiPolygon` features in a GeoJSON file. Each sensor reports whether a tracked device is inside (or outside, when inverted) each configured zone.

## Features

- Supports GeoJSON `Polygon` and `MultiPolygon` geometry.
- Optional tolerance zones using approximate polygon offsetting.
- Fast point-in-polygon evaluation using precomputed bounding boxes and edges.
- Optional GeoJSON file watching with automatic config-entry reload.
- Emits enter/exit events for automations.

## Installation

1. Copy this integration into your Home Assistant custom components folder as:
   `custom_components/poly_zone/`
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and choose **Polygon Zone**.

## Configuration

The initial setup flow asks for:

- **Name**: Friendly device name shown in Home Assistant.
- **GeoJSON path**: Absolute path to a local GeoJSON file.
- **Device tracker**: A `device_tracker.*` entity to monitor.
- **Tolerance (meters)**: Optional expansion distance for a secondary diagnostic sensor.

### Options

After setup, you can tune:

- `tolerance`: Override tolerance distance.
- `invert`: Flip logic to treat outside as active.
- `watch_geojson`: Enable/disable periodic file mtime checks.
- `watch_interval`: Reload polling interval in seconds (minimum 5).

## Events

The integration fires these events:

- `poly_zone_enter`
- `poly_zone_exit`

Event payload includes:

- `entity_id`
- `device_tracker`
- `in_zone`
- `lat`
- `lon`

## Notes on validation and reliability

- The config flow validates that the file exists and that at least one feature has supported geometry.
- GeoJSON coordinates are normalized to numeric longitude/latitude pairs before sensors are created.
- If no valid polygon rings are found, entities are not created and an error is logged.
