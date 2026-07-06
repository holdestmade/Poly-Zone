"""Geometry helpers: GeoJSON loading, polygon offsetting and edge-distance maths."""
from __future__ import annotations

import json
import math
from typing import Any

from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

from .const import METERS_PER_DEGREE_LAT

# A ring is an ordered list of (longitude, latitude) vertices.
Ring = list[tuple[float, float]]
Edge = tuple[tuple[float, float], tuple[float, float]]


# --- Offsetting ---


def _largest_polygon(geom: Any) -> Polygon | None:
    if geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda g: g.area)
    return None


def offset_polygon(polygon: Ring, offset_meters: float) -> Ring:
    """Offset a polygon by a given distance in metres.

    Positive ``offset_meters`` grows the polygon outward (the "tolerance"
    use case — a buffer ring outside the configured zone that absorbs GPS
    jitter); negative shrinks it inward. The offset is computed in a local
    azimuthal-equidistant projection centred on the polygon's centroid, so
    distances are accurate in metres regardless of latitude.
    """
    if not offset_meters:
        return list(polygon)

    if len(polygon) < 3:
        return []

    centroid_lon = sum(p[0] for p in polygon) / len(polygon)
    centroid_lat = sum(p[1] for p in polygon) / len(polygon)

    proj = (
        f"+proj=aeqd +lat_0={centroid_lat} +lon_0={centroid_lon} "
        "+datum=WGS84 +units=m +no_defs"
    )
    to_metres = Transformer.from_crs("EPSG:4326", proj, always_xy=True).transform
    to_lonlat = Transformer.from_crs(proj, "EPSG:4326", always_xy=True).transform

    geom = shapely_transform(to_metres, Polygon(polygon))
    buffered = geom.buffer(offset_meters, join_style="mitre", mitre_limit=5.0)
    result = _largest_polygon(buffered)
    if result is None:
        return []

    unprojected = shapely_transform(to_lonlat, result)
    coords = list(unprojected.exterior.coords)
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return [(float(x), float(y)) for x, y in coords]


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


def _normalize_ring(ring: list[list[float]]) -> Ring:
    normalized: Ring = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        lon, lat = point[0], point[1]
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            normalized.append((float(lon), float(lat)))
    return normalized


def load_polygons_from_geojson(
    file_path: str,
) -> tuple[list[Ring], list[dict[str, Any]]]:
    """Load polygon rings and per-ring metadata from a GeoJSON file.

    Raises ``OSError`` if the file cannot be read and ``ValueError`` if the
    contents are not valid GeoJSON, so the caller can distinguish transient
    (retry) from permanent (error) failures.
    """
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("GeoJSON root must be a JSON object")
    features = data.get("features") or []
    if not isinstance(features, list):
        raise ValueError("GeoJSON 'features' must be a list")

    rings: list[Ring] = []
    meta: list[dict[str, Any]] = []
    for idx, feat in enumerate(features):
        geometry = (feat or {}).get("geometry") or {}
        props = (feat or {}).get("properties") or {}
        for ring_index, (ring_coords, hole_coords) in enumerate(_coords_to_rings(geometry)):
            normalized = _normalize_ring(ring_coords)
            if len(normalized) >= 3:
                normalized_holes: list[Ring] = []
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


# --- Distance to edges ---


def precompute_edges(polygon: Ring) -> list[Edge]:
    n = len(polygon)
    return [(polygon[i], polygon[(i + 1) % n]) for i in range(n)]


def _point_segment_distance_m(
    lon: float,
    lat: float,
    a: tuple[float, float],
    b: tuple[float, float],
    meters_per_deg_lon: float,
) -> float:
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


def point_edges_distance_m(lon: float, lat: float, edges: list[Edge]) -> float:
    """Minimum distance in metres from a point to any edge.

    Uses a local equirectangular approximation, accurate for the short
    distances this integration deals in.
    """
    meters_per_deg_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(lat))
    return min(_point_segment_distance_m(lon, lat, a, b, meters_per_deg_lon) for a, b in edges)
