import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# --- Helpers: winding/offset ---


def ensure_ccw_winding(polygon: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ensure polygon winding is counter-clockwise (area > 0)."""
    area = 0.0
    for i, p1 in enumerate(polygon):
        p2 = polygon[(i + 1) % len(polygon)]
        area += (p1[0] * p2[1]) - (p2[0] * p1[1])
    if area < 0:
        _LOGGER.debug("Polygon has clockwise winding, reversing.")
        return polygon[::-1]
    return polygon


def offset_polygon(
    polygon: list[tuple[float, float]], offset_meters: float
) -> list[tuple[float, float]]:
    """Offset a polygon by a given number of meters (approximate)."""
    if not offset_meters:
        return polygon

    new_polygon: list[tuple[float, float]] = []
    meters_per_degree_lat = 111320.0
    for i, p2 in enumerate(polygon):
        p1 = polygon[i - 1]
        p3 = polygon[(i + 1) % len(polygon)]

        v1 = (p2[0] - p1[0], p2[1] - p1[1])
        v2 = (p3[0] - p2[0], p3[1] - p2[1])

        mag1 = math.hypot(v1[0], v1[1])
        mag2 = math.hypot(v2[0], v2[1])
        if mag1 == 0 or mag2 == 0:
            continue

        v1 = (v1[0] / mag1, v1[1] / mag1)
        v2 = (v2[0] / mag2, v2[1] / mag2)

        n1 = (-v1[1], v1[0])
        n2 = (-v2[1], v2[0])

        bisector = (n1[0] + n2[0], n1[1] + n2[1])
        mag_b = math.hypot(bisector[0], bisector[1])
        if mag_b == 0:
            continue
        bisector = (bisector[0] / mag_b, bisector[1] / mag_b)

        dot_product = v1[0] * v2[0] + v1[1] * v2[1]
        angle = math.acos(max(-1.0, min(1.0, dot_product)))
        if angle == 0:
            continue

        offset_distance = offset_meters / math.sin(angle / 2)
        meters_per_degree_lon = meters_per_degree_lat * math.cos(math.radians(p2[1]))
        if meters_per_degree_lon == 0:
            continue

        offset_lon = (offset_distance * bisector[0]) / meters_per_degree_lon
        offset_lat = (offset_distance * bisector[1]) / meters_per_degree_lat

        new_polygon.append((p2[0] + offset_lon, p2[1] + offset_lat))

    return new_polygon


# --- GeoJSON loader ---


def _coords_to_rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return []

    if gtype == "Polygon":
        return [coords[0]] if coords else []
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly and isinstance(poly, list)]
    return []


def _normalize_ring(ring: list[list[float]]) -> list[tuple[float, float]]:
    normalized: list[tuple[float, float]] = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        lon, lat = point[0], point[1]
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            normalized.append((float(lon), float(lat)))
    return normalized


def load_polygons_from_geojson(
    file_path: str,
) -> tuple[list[list[tuple[float, float]]], list[dict[str, Any]]]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        features = data.get("features", [])
        rings: list[list[tuple[float, float]]] = []
        meta: list[dict[str, Any]] = []
        for idx, feat in enumerate(features):
            geometry = (feat or {}).get("geometry") or {}
            props = (feat or {}).get("properties") or {}
            for ring_index, ring in enumerate(_coords_to_rings(geometry)):
                normalized = _normalize_ring(ring)
                if len(normalized) >= 3:
                    rings.append(normalized)
                    meta.append(
                        {
                            "feature_index": idx,
                            "ring_index": ring_index,
                            "name": props.get("name") or props.get("title"),
                        }
                    )
        return rings, meta
    except (OSError, ValueError) as err:
        _LOGGER.error("Error loading polygon(s) from %s: %s", file_path, err)
        return [], []


# --- Speedups & metrics ---


def _bbox(polygon: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _precompute_edges(
    polygon: list[tuple[float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    n = len(polygon)
    return [(polygon[i], polygon[(i + 1) % n]) for i in range(n)]


def _point_in_polygon_fast(
    point: tuple[float, float],
    polygon: list[tuple[float, float]],
    edges: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> bool:
    lon, lat = point
    if bbox:
        x1, y1, x2, y2 = bbox
        if not (x1 <= lon <= x2 and y1 <= lat <= y2):
            return False
    if edges is None:
        edges = _precompute_edges(polygon)

    inside = False
    for (p1_lon, p1_lat), (p2_lon, p2_lat) in edges:
        if (p1_lat > lat) != (p2_lat > lat):
            xints = (p2_lon - p1_lon) * (lat - p1_lat) / (p2_lat - p1_lat + 1e-18) + p1_lon
            if lon <= xints:
                inside = not inside
    return inside


def _point_segment_distance_m(
    lon: float,
    lat: float,
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = meters_per_deg_lat * math.cos(math.radians(lat))
    ax = (a[0] - lon) * meters_per_deg_lon
    ay = (a[1] - lat) * meters_per_deg_lat
    bx = (b[0] - lon) * meters_per_deg_lon
    by = (b[1] - lat) * meters_per_deg_lat
    abx, aby = (bx - ax), (by - ay)
    ab2 = abx * abx + aby * aby or 1e-12
    t = max(0.0, min(1.0, -(ax * abx + ay * aby) / ab2))
    px, py = ax + t * abx, ay + t * aby
    return math.hypot(px, py)


def _min_distance_to_edges_m(
    lon: float,
    lat: float,
    edges: list[tuple[tuple[float, float], tuple[float, float]]],
) -> float:
    return min(_point_segment_distance_m(lon, lat, a, b) for a, b in edges)


# --- Platform setup ---


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    name = entry.data["name"]
    geojson_path = entry.data["geojson_path"]
    device_tracker_entity_id = entry.data["device_tracker"]
    tolerance = float(entry.options.get("tolerance", entry.data.get("tolerance", 0)))
    invert = bool(entry.options.get("invert", False))

    rings, meta = await hass.async_add_executor_job(load_polygons_from_geojson, geojson_path)
    if not rings:
        _LOGGER.error("No valid polygons found. Component will not be set up.")
        return

    entities: list[PolyZoneBinarySensor] = []
    for i, ring in enumerate(rings):
        ring = ensure_ccw_winding(ring)
        display = meta[i].get("name") or f"Zone {i + 1}"
        base_id = f"zone_{i + 1}"

        label_exact = "Outside Exact Zone" if invert else "Inside Exact Zone"
        label_tol = "Outside Tolerated Zone" if invert else "Inside Tolerated Zone"

        entities.append(
            PolyZoneBinarySensor(
                f"{name} - {display}",
                ring,
                device_tracker_entity_id,
                entry,
                f"{base_id}_exact",
                label_exact,
                invert,
            )
        )

        if tolerance > 0:
            offset_poly = offset_polygon(ring, tolerance)
            if offset_poly:
                entities.append(
                    PolyZoneBinarySensor(
                        f"{name} - {display}",
                        offset_poly,
                        device_tracker_entity_id,
                        entry,
                        f"{base_id}_tolerance",
                        label_tol,
                        invert,
                        diagnostic=True,
                    )
                )

    async_add_entities(entities)


# --- Entity ---


class PolyZoneBinarySensor(BinarySensorEntity):
    """Representation of a Polygon Zone binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = "occupancy"

    def __init__(
        self,
        name: str,
        polygon: list[tuple[float, float]],
        device_tracker_entity_id: str,
        entry: ConfigEntry,
        id_suffix: str,
        entity_name: str,
        invert: bool,
        diagnostic: bool = False,
    ) -> None:
        self._polygon = polygon
        self._device_tracker_entity_id = device_tracker_entity_id
        self._is_on = False
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._distance_m: float | None = None
        self._last_transition: str | None = None
        self._invert = invert

        self._bbox = _bbox(self._polygon)
        self._edges = _precompute_edges(self._polygon)

        self._attr_name = entity_name
        self._attr_unique_id = f"{entry.entry_id}_{id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=name,
            manufacturer="Home Assistant",
        )
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_tracker": self._device_tracker_entity_id,
            "latitude": self._latitude,
            "longitude": self._longitude,
            "distance_to_edge_m": self._distance_m,
            "last_transition": self._last_transition,
            "invert": self._invert,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._device_tracker_entity_id, self._handle_state_change
            )
        )
        if initial_state := self.hass.states.get(self._device_tracker_entity_id):
            self._update_state(initial_state)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        if new_state := event.data.get("new_state"):
            self._update_state(new_state)
            self.async_write_ha_state()

    def _update_state(self, state: Any) -> None:
        latitude = state.attributes.get("latitude")
        longitude = state.attributes.get("longitude")
        self._latitude = float(latitude) if isinstance(latitude, (int, float)) else None
        self._longitude = float(longitude) if isinstance(longitude, (int, float)) else None

        prev = self._is_on
        if self._latitude is not None and self._longitude is not None:
            inside = _point_in_polygon_fast(
                (self._longitude, self._latitude),
                self._polygon,
                self._edges,
                self._bbox,
            )
            self._is_on = (not inside) if self._invert else inside
            self._distance_m = _min_distance_to_edges_m(
                self._longitude, self._latitude, self._edges
            )
        else:
            self._is_on = False
            self._distance_m = None

        if prev != self._is_on:
            self._last_transition = datetime.now(timezone.utc).isoformat()
            evt = f"{DOMAIN}_{'enter' if self._is_on else 'exit'}"
            self.hass.bus.async_fire(
                evt,
                {
                    "entity_id": self.entity_id,
                    "device_tracker": self._device_tracker_entity_id,
                    "in_zone": self._is_on,
                    "lat": self._latitude,
                    "lon": self._longitude,
                },
            )
