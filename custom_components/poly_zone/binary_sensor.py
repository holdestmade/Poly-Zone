import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, METERS_PER_DEGREE_LAT, RAY_CAST_EPSILON

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
        # Return a copy so callers cannot accidentally mutate the original.
        return list(polygon)

    new_polygon: list[tuple[float, float]] = []
    for i, p2 in enumerate(polygon):
        p1 = polygon[i - 1]
        p3 = polygon[(i + 1) % len(polygon)]

        v1 = (p2[0] - p1[0], p2[1] - p1[1])
        v2 = (p3[0] - p2[0], p3[1] - p2[1])

        mag1 = math.hypot(v1[0], v1[1])
        mag2 = math.hypot(v2[0], v2[1])
        if mag1 == 0 or mag2 == 0:
            _LOGGER.debug("offset_polygon: skipping degenerate vertex at index %d", i)
            continue

        v1 = (v1[0] / mag1, v1[1] / mag1)
        v2 = (v2[0] / mag2, v2[1] / mag2)

        n1 = (-v1[1], v1[0])
        n2 = (-v2[1], v2[0])

        bisector = (n1[0] + n2[0], n1[1] + n2[1])
        mag_b = math.hypot(bisector[0], bisector[1])
        if mag_b == 0:
            _LOGGER.debug("offset_polygon: zero-length bisector at index %d, skipping", i)
            continue
        bisector = (bisector[0] / mag_b, bisector[1] / mag_b)

        dot_product = v1[0] * v2[0] + v1[1] * v2[1]
        angle = math.acos(max(-1.0, min(1.0, dot_product)))
        if angle == 0:
            _LOGGER.debug("offset_polygon: collinear edges at index %d, skipping", i)
            continue

        offset_distance = offset_meters / math.sin(angle / 2)
        meters_per_degree_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(p2[1]))
        if meters_per_degree_lon == 0:
            _LOGGER.debug("offset_polygon: zero lon scale at index %d (polar point?), skipping", i)
            continue

        offset_lon = (offset_distance * bisector[0]) / meters_per_degree_lon
        offset_lat = (offset_distance * bisector[1]) / METERS_PER_DEGREE_LAT

        new_polygon.append((p2[0] + offset_lon, p2[1] + offset_lat))

    if len(new_polygon) < len(polygon):
        _LOGGER.warning(
            "offset_polygon: %d of %d vertices dropped due to degenerate geometry; "
            "tolerance polygon may be inaccurate",
            len(polygon) - len(new_polygon),
            len(polygon),
        )
    return new_polygon


# --- GeoJSON loader ---


def _coords_to_rings(
    geometry: dict[str, Any],
) -> list[tuple[list[list[float]], list[list[list[float]]]]]:
    """Return (exterior_ring_coords, [hole_ring_coords, ...]) for each polygon in geometry."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return []

    if gtype == "Polygon":
        if not coords:
            return []
        exterior = coords[0]
        holes = coords[1:] if len(coords) > 1 else []
        return [(exterior, holes)]
    if gtype == "MultiPolygon":
        result = []
        for poly in coords:
            if poly and isinstance(poly, list):
                exterior = poly[0]
                holes = poly[1:] if len(poly) > 1 else []
                result.append((exterior, holes))
        return result
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
            for ring_index, (ring_coords, hole_coords) in enumerate(_coords_to_rings(geometry)):
                normalized = _normalize_ring(ring_coords)
                if len(normalized) >= 3:
                    normalized_holes: list[list[tuple[float, float]]] = []
                    for h_raw in hole_coords:
                        h = _normalize_ring(h_raw)
                        if len(h) >= 3:
                            normalized_holes.append(h)
                    rings.append(normalized)
                    meta.append(
                        {
                            "feature_index": idx,
                            "ring_index": ring_index,
                            "name": props.get("name") or props.get("title"),
                            "holes": normalized_holes,
                        }
                    )
        return rings, meta
    except (OSError, ValueError) as err:
        _LOGGER.error("Error loading polygon(s) from %s: %s", file_path, err)
        return [], []


# --- Speedups & metrics ---


def _bbox(polygon: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = zip(*polygon)
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
    if bbox is not None:
        x1, y1, x2, y2 = bbox
        if not (x1 <= lon <= x2 and y1 <= lat <= y2):
            return False
    if edges is None:
        edges = _precompute_edges(polygon)

    inside = False
    for (p1_lon, p1_lat), (p2_lon, p2_lat) in edges:
        if (p1_lat > lat) != (p2_lat > lat):
            denom = p2_lat - p1_lat
            if abs(denom) < RAY_CAST_EPSILON:
                continue
            xints = (p2_lon - p1_lon) * (lat - p1_lat) / denom + p1_lon
            if lon <= xints:
                inside = not inside
    return inside


def _point_segment_distance_m(
    lon: float,
    lat: float,
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    meters_per_deg_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(lat))
    ax = (a[0] - lon) * meters_per_deg_lon
    ay = (a[1] - lat) * METERS_PER_DEGREE_LAT
    bx = (b[0] - lon) * meters_per_deg_lon
    by = (b[1] - lat) * METERS_PER_DEGREE_LAT
    abx, aby = (bx - ax), (by - ay)
    ab2 = abx * abx + aby * aby
    if ab2 == 0:
        # Degenerate segment: both endpoints are identical; distance to either point.
        return math.hypot(ax, ay)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    name = entry.data["name"]
    geojson_path = entry.data["geojson_path"]
    device_tracker_entity_id = entry.data["device_tracker"]
    tolerance = float(entry.options.get("tolerance", entry.data.get("tolerance", 0)))
    invert = bool(entry.options.get("invert", False))

    if not hass.states.get(device_tracker_entity_id):
        _LOGGER.warning(
            "Device tracker '%s' is not available yet. "
            "Poly-Zone sensors will update once it reports a state.",
            device_tracker_entity_id,
        )

    rings, meta = await hass.async_add_executor_job(load_polygons_from_geojson, geojson_path)
    if not rings:
        _LOGGER.error("No valid polygons found in %s. Component will not be set up.", geojson_path)
        return

    entities: list[PolyZoneBinarySensor] = []
    for i, ring in enumerate(rings):
        ring = ensure_ccw_winding(ring)
        zone_name = meta[i].get("name") or f"Zone {i + 1}"
        holes = meta[i].get("holes", [])
        base_id = f"zone_{i + 1}"

        label_exact = "Outside Exact Zone" if invert else "Inside Exact Zone"
        label_tol = "Outside Tolerated Zone" if invert else "Inside Tolerated Zone"

        entities.append(
            PolyZoneBinarySensor(
                f"{name} - {zone_name}",
                ring,
                holes,
                zone_name,
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
                        f"{name} - {zone_name}",
                        offset_poly,
                        holes,
                        zone_name,
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
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(
        self,
        name: str,
        polygon: list[tuple[float, float]],
        holes: list[list[tuple[float, float]]],
        zone_name: str,
        device_tracker_entity_id: str,
        entry: ConfigEntry,
        id_suffix: str,
        entity_name: str,
        invert: bool,
        diagnostic: bool = False,
    ) -> None:
        self._polygon = polygon
        self._holes = holes
        self._zone_name = zone_name
        self._device_tracker_entity_id = device_tracker_entity_id
        self._is_on = False
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._distance_m: float | None = None
        self._last_transition: str | None = None
        self._invert = invert

        self._bbox = _bbox(self._polygon)
        self._edges = _precompute_edges(self._polygon)
        self._hole_edges = [_precompute_edges(h) for h in holes]
        self._hole_bboxes = [_bbox(h) for h in holes]

        self._attr_name = entity_name
        self._attr_unique_id = f"{entry.entry_id}_{id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=name,
            manufacturer="Poly-Zone",
        )
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "zone_name": self._zone_name,
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
            self.async_write_ha_state()

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
            point = (self._longitude, self._latitude)
            inside_geo = _point_in_polygon_fast(
                point,
                self._polygon,
                self._edges,
                self._bbox,
            )
            # A point inside a hole is geometrically outside the polygon.
            if inside_geo and self._holes:
                for h_poly, h_edges, h_bbox in zip(
                    self._holes, self._hole_edges, self._hole_bboxes
                ):
                    if _point_in_polygon_fast(point, h_poly, h_edges, h_bbox):
                        inside_geo = False
                        break
            self._is_on = (not inside_geo) if self._invert else inside_geo
            raw_dist = _min_distance_to_edges_m(self._longitude, self._latitude, self._edges)
            # Negative = inside the exterior boundary, positive = outside.
            self._distance_m = -raw_dist if inside_geo else raw_dist
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
