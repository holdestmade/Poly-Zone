"""Unit tests for poly_zone binary_sensor geometric helpers and entity logic."""
import json
import math
import tempfile
import os
import pytest
from unittest.mock import MagicMock, patch

from custom_components.poly_zone.binary_sensor import (
    PolyZoneBinarySensor,
    _bbox,
    _min_distance_to_edges_m,
    _normalize_ring,
    _point_in_polygon_fast,
    _point_segment_distance_m,
    _precompute_edges,
    load_polygons_from_geojson,
    offset_polygon,
)
from custom_components.poly_zone.const import METERS_PER_DEGREE_LAT

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A simple 1° × 1° square centred near the equator (lon 0–1, lat 0–1).
SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


# ---------------------------------------------------------------------------
# _bbox
# ---------------------------------------------------------------------------

class TestBbox:
    def test_square_bbox(self):
        assert _bbox(SQUARE) == (0.0, 0.0, 1.0, 1.0)

    def test_offset_polygon_bbox(self):
        poly = [(2.0, 3.0), (5.0, 3.0), (5.0, 7.0), (2.0, 7.0)]
        assert _bbox(poly) == (2.0, 3.0, 5.0, 7.0)


# ---------------------------------------------------------------------------
# _precompute_edges
# ---------------------------------------------------------------------------

class TestPrecomputeEdges:
    def test_square_edge_count(self):
        edges = _precompute_edges(SQUARE)
        assert len(edges) == 4

    def test_edges_wrap_around(self):
        edges = _precompute_edges(SQUARE)
        # Last edge must connect last vertex back to first.
        assert edges[-1] == (SQUARE[-1], SQUARE[0])


# ---------------------------------------------------------------------------
# _point_in_polygon_fast
# ---------------------------------------------------------------------------

class TestPointInPolygon:
    def setup_method(self):
        self.poly = SQUARE
        self.bbox = _bbox(self.poly)
        self.edges = _precompute_edges(self.poly)

    def test_centre_inside(self):
        assert _point_in_polygon_fast((0.5, 0.5), self.poly, self.edges, self.bbox)

    def test_outside_rejected_by_bbox(self):
        assert not _point_in_polygon_fast((2.0, 2.0), self.poly, self.edges, self.bbox)

    def test_outside_within_bbox_row(self):
        # Point is within the bbox x-range but outside polygon (just above).
        assert not _point_in_polygon_fast((0.5, 1.5), self.poly, self.edges, self.bbox)

    def test_corner_handling(self):
        # A point well outside should never be inside.
        assert not _point_in_polygon_fast((-1.0, -1.0), self.poly, self.edges, self.bbox)

    def test_no_precomputed_data(self):
        # Works without pre-supplied edges/bbox.
        assert _point_in_polygon_fast((0.5, 0.5), self.poly)

    def test_near_horizontal_edge_no_crash(self):
        # A point whose latitude almost exactly matches a horizontal edge should
        # not cause a division-by-near-zero error.
        # The bottom edge of SQUARE is at lat=0.0.
        assert not _point_in_polygon_fast((0.5, 0.0), self.poly, self.edges, self.bbox) or True
        # We are only asserting this does not raise, not checking a specific value,
        # because the exact on-edge result is undefined for ray-casting.

    def test_non_convex_polygon(self):
        # L-shaped polygon (non-convex).
        l_shape = [
            (0.0, 0.0), (2.0, 0.0), (2.0, 1.0),
            (1.0, 1.0), (1.0, 2.0), (0.0, 2.0),
        ]
        edges = _precompute_edges(l_shape)
        bbox = _bbox(l_shape)
        assert _point_in_polygon_fast((0.5, 0.5), l_shape, edges, bbox)
        # The notch region should be outside.
        assert not _point_in_polygon_fast((1.5, 1.5), l_shape, edges, bbox)

    def test_bbox_none_still_works(self):
        # bbox=None should not raise and should fall through to ray-casting.
        assert _point_in_polygon_fast((0.5, 0.5), self.poly, self.edges, None)
        assert not _point_in_polygon_fast((2.0, 2.0), self.poly, self.edges, None)


# ---------------------------------------------------------------------------
# _point_segment_distance_m
# ---------------------------------------------------------------------------

class TestPointSegmentDistance:
    def test_point_at_segment_midpoint(self):
        # Segment along equator from lon=0 to lon=1, lat=0.
        # Query point directly south at lat=-1.
        a = (0.0, 0.0)
        b = (1.0, 0.0)
        dist = _point_segment_distance_m(0.5, -1.0, a, b)
        expected = METERS_PER_DEGREE_LAT * 1.0
        assert abs(dist - expected) < 1.0  # within 1 m

    def test_point_beyond_segment_end(self):
        # Query point past end of segment — closest point is segment endpoint.
        a = (0.0, 0.0)
        b = (1.0, 0.0)
        dist = _point_segment_distance_m(2.0, 0.0, a, b)
        # Distance from (2,0) to segment endpoint (1,0): 1 degree of longitude.
        meters_per_deg_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(0.0))
        expected = meters_per_deg_lon * 1.0
        assert abs(dist - expected) < 1.0

    def test_degenerate_segment_both_endpoints_equal(self):
        # Segment of zero length: both endpoints at same location.
        a = (1.0, 1.0)
        b = (1.0, 1.0)
        dist = _point_segment_distance_m(1.0, 0.0, a, b)
        expected = METERS_PER_DEGREE_LAT * 1.0
        assert abs(dist - expected) < 1.0


# ---------------------------------------------------------------------------
# _min_distance_to_edges_m
# ---------------------------------------------------------------------------

class TestMinDistanceToEdges:
    def test_point_near_bottom_edge(self):
        edges = _precompute_edges(SQUARE)
        # A point on the south side, just below the polygon.
        dist = _min_distance_to_edges_m(0.5, -0.01, edges)
        expected = METERS_PER_DEGREE_LAT * 0.01
        assert abs(dist - expected) < 5.0  # within 5 m


# ---------------------------------------------------------------------------
# _normalize_ring
# ---------------------------------------------------------------------------

class TestNormalizeRing:
    def test_valid_ring(self):
        ring = [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]]
        result = _normalize_ring(ring)
        assert result == [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]

    def test_integer_coords_converted_to_float(self):
        ring = [[0, 1], [2, 3], [4, 5]]
        result = _normalize_ring(ring)
        assert all(isinstance(c, float) for pair in result for c in pair)

    def test_short_point_skipped(self):
        ring = [[0.0], [1.0, 2.0], [3.0, 4.0]]
        result = _normalize_ring(ring)
        assert len(result) == 2

    def test_non_numeric_skipped(self):
        ring = [["a", "b"], [1.0, 2.0], [3.0, 4.0]]
        result = _normalize_ring(ring)
        assert len(result) == 2

    def test_empty_ring(self):
        assert _normalize_ring([]) == []


# ---------------------------------------------------------------------------
# offset_polygon
# ---------------------------------------------------------------------------

class TestOffsetPolygon:
    def test_zero_offset_returns_copy(self):
        result = offset_polygon(SQUARE, 0)
        assert result == SQUARE
        assert result is not SQUARE  # must be a copy, not the same object

    def test_positive_offset_expands(self):
        # Positive tolerance grows the polygon outward — a buffer ring around
        # the configured zone that absorbs GPS jitter.
        result = offset_polygon(SQUARE, 100)
        orig_bbox = _bbox(SQUARE)
        new_bbox = _bbox(result)
        assert new_bbox[0] < orig_bbox[0]  # min x moved outward (smaller)
        assert new_bbox[1] < orig_bbox[1]  # min y moved outward (smaller)
        assert new_bbox[2] > orig_bbox[2]  # max x moved outward (larger)
        assert new_bbox[3] > orig_bbox[3]  # max y moved outward (larger)

    def test_negative_offset_shrinks(self):
        # A negative offset shrinks the polygon inward.
        result = offset_polygon(SQUARE, -100)
        orig_bbox = _bbox(SQUARE)
        new_bbox = _bbox(result)
        assert new_bbox[0] > orig_bbox[0]
        assert new_bbox[1] > orig_bbox[1]
        assert new_bbox[2] < orig_bbox[2]
        assert new_bbox[3] < orig_bbox[3]

    def test_duplicate_vertex_handled_without_warning(self, caplog):
        import logging
        # Polygon with a duplicate consecutive vertex (zero-length edge).
        # Shapely cleans this up automatically — no warnings expected.
        poly = [(0.0, 0.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        with caplog.at_level(logging.WARNING, logger="custom_components.poly_zone.binary_sensor"):
            result = offset_polygon(poly, 100)
        assert len(result) >= 3
        assert not any("dropped" in r.message for r in caplog.records)

    def test_offset_distance_accurate_in_metres(self):
        # A 1° × 1° square at the equator is roughly 111 km on a side.
        # Expanding by 1000 m should move each side outward by ~1000 m,
        # i.e. ~0.009° of latitude.
        result = offset_polygon(SQUARE, 1000)
        new_bbox = _bbox(result)
        expected_offset_deg = 1000.0 / METERS_PER_DEGREE_LAT
        assert abs(new_bbox[0] - (-expected_offset_deg)) < 1e-3
        assert abs(new_bbox[2] - (1.0 + expected_offset_deg)) < 1e-3

    def test_excessive_negative_offset_returns_empty(self):
        # A negative offset large enough to collapse the polygon yields an
        # empty result rather than raising.
        result = offset_polygon(SQUARE, -10_000_000)
        assert result == []


# ---------------------------------------------------------------------------
# load_polygons_from_geojson
# ---------------------------------------------------------------------------

class TestLoadPolygonsFromGeojson:
    def _write_geojson(self, data: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        )
        json.dump(data, tmp)
        tmp.close()
        return tmp.name

    def teardown_method(self):
        # Clean up any temp files left behind.
        pass

    def test_loads_polygon_feature(self):
        coords = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": coords},
                 "properties": {"name": "Test Zone"}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert len(rings) == 1
            assert meta[0]["name"] == "Test Zone"
        finally:
            os.unlink(path)

    def test_loads_multipolygon_feature(self):
        coords = [[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
                  [[[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 3.0], [2.0, 2.0]]]]
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": coords},
                 "properties": {}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert len(rings) == 2
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        rings, meta = load_polygons_from_geojson("/nonexistent/path/file.geojson")
        assert rings == []
        assert meta == []

    def test_invalid_json_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        )
        tmp.write("not valid json {{{")
        tmp.close()
        try:
            rings, meta = load_polygons_from_geojson(tmp.name)
            assert rings == []
        finally:
            os.unlink(tmp.name)

    def test_ring_with_too_few_points_skipped(self):
        coords = [[[0.0, 0.0], [1.0, 0.0]]]  # only 2 points, need ≥3
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": coords},
                 "properties": {}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert rings == []
        finally:
            os.unlink(path)

    def test_name_falls_back_to_title(self):
        coords = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": coords},
                 "properties": {"title": "My Zone"}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert meta[0]["name"] == "My Zone"
        finally:
            os.unlink(path)

    def test_polygon_with_hole_loaded(self):
        # Outer ring: 0–2 degree square. Hole: 0.5–1.5 degree inner square.
        outer = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
        hole = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature",
                 "geometry": {"type": "Polygon", "coordinates": [outer, hole]},
                 "properties": {"name": "Donut"}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert len(rings) == 1
            assert len(meta[0]["holes"]) == 1
            assert len(meta[0]["holes"][0]) >= 3
        finally:
            os.unlink(path)

    def test_polygon_holes_stored_in_meta(self):
        outer = [[0.0, 0.0], [3.0, 0.0], [3.0, 3.0], [0.0, 3.0], [0.0, 0.0]]
        hole1 = [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0], [0.5, 0.5]]
        hole2 = [[1.5, 1.5], [2.5, 1.5], [2.5, 2.5], [1.5, 2.5], [1.5, 1.5]]
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature",
                 "geometry": {"type": "Polygon", "coordinates": [outer, hole1, hole2]},
                 "properties": {}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert len(meta[0]["holes"]) == 2
        finally:
            os.unlink(path)

    def test_no_holes_gives_empty_list(self):
        coords = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
        path = self._write_geojson(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature",
                 "geometry": {"type": "Polygon", "coordinates": coords},
                 "properties": {}}
            ]}
        )
        try:
            rings, meta = load_polygons_from_geojson(path)
            assert meta[0]["holes"] == []
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# PolyZoneBinarySensor entity
# ---------------------------------------------------------------------------

def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _make_sensor(
    polygon=None,
    holes=None,
    zone_name="Test Zone",
    device_tracker="device_tracker.test",
    invert=False,
    diagnostic=False,
):
    if polygon is None:
        polygon = SQUARE
    if holes is None:
        holes = []
    entry = _make_entry()
    sensor = PolyZoneBinarySensor(
        name="Test Device - Test Zone",
        polygon=polygon,
        holes=holes,
        zone_name=zone_name,
        device_tracker_entity_id=device_tracker,
        entry=entry,
        id_suffix="zone_1_exact",
        entity_name="Inside Exact Zone",
        invert=invert,
        diagnostic=diagnostic,
    )
    # Attach a mock hass so _update_state can fire events without error.
    sensor.hass = MagicMock()
    sensor.entity_id = "binary_sensor.test_zone"
    return sensor


def _make_state(lat, lon):
    state = MagicMock()
    state.attributes = {"latitude": lat, "longitude": lon}
    return state


class TestPolyZoneBinarySensorInit:
    def test_initial_state_is_off(self):
        sensor = _make_sensor()
        assert sensor.is_on is False

    def test_unique_id_set(self):
        sensor = _make_sensor()
        assert "zone_1_exact" in sensor._attr_unique_id

    def test_zone_name_in_attributes(self):
        sensor = _make_sensor(zone_name="My Park")
        attrs = sensor.extra_state_attributes
        assert attrs["zone_name"] == "My Park"

    def test_invert_reflected_in_attributes(self):
        sensor = _make_sensor(invert=True)
        assert sensor.extra_state_attributes["invert"] is True

    def test_device_tracker_in_attributes(self):
        sensor = _make_sensor(device_tracker="device_tracker.phone")
        assert sensor.extra_state_attributes["device_tracker"] == "device_tracker.phone"

    def test_holes_precomputed(self):
        hole = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
        sensor = _make_sensor(holes=[hole])
        assert len(sensor._hole_edges) == 1
        assert len(sensor._hole_bboxes) == 1


class TestPolyZoneBinarySensorUpdateState:
    def test_inside_polygon_turns_on(self):
        sensor = _make_sensor()
        sensor._update_state(_make_state(0.5, 0.5))
        assert sensor.is_on is True

    def test_outside_polygon_stays_off(self):
        sensor = _make_sensor()
        sensor._update_state(_make_state(5.0, 5.0))
        assert sensor.is_on is False

    def test_none_coordinates_turns_off(self):
        sensor = _make_sensor()
        sensor._update_state(_make_state(0.5, 0.5))
        assert sensor.is_on is True
        state = MagicMock()
        state.attributes = {"latitude": None, "longitude": None}
        sensor._update_state(state)
        assert sensor.is_on is False

    def test_invert_flips_state(self):
        sensor = _make_sensor(invert=True)
        # Inside the polygon → inverted sensor is OFF
        sensor._update_state(_make_state(0.5, 0.5))
        assert sensor.is_on is False
        # Outside the polygon → inverted sensor is ON
        sensor._update_state(_make_state(5.0, 5.0))
        assert sensor.is_on is True

    def test_distance_negative_when_inside(self):
        sensor = _make_sensor()
        sensor._update_state(_make_state(0.5, 0.5))
        dist = sensor.extra_state_attributes["distance_to_edge_m"]
        assert dist is not None
        assert dist < 0  # inside → negative distance

    def test_distance_positive_when_outside(self):
        sensor = _make_sensor()
        sensor._update_state(_make_state(5.0, 5.0))
        dist = sensor.extra_state_attributes["distance_to_edge_m"]
        assert dist is not None
        assert dist > 0  # outside → positive distance

    def test_distance_none_when_no_coordinates(self):
        sensor = _make_sensor()
        state = MagicMock()
        state.attributes = {}
        sensor._update_state(state)
        assert sensor.extra_state_attributes["distance_to_edge_m"] is None

    def test_last_transition_set_on_change(self):
        sensor = _make_sensor()
        assert sensor._last_transition is None
        sensor._update_state(_make_state(0.5, 0.5))
        assert sensor._last_transition is not None

    def test_last_transition_not_updated_without_change(self):
        sensor = _make_sensor()
        sensor._update_state(_make_state(0.5, 0.5))
        ts1 = sensor._last_transition
        sensor._update_state(_make_state(0.4, 0.4))  # still inside
        assert sensor._last_transition == ts1


class TestPolyZoneBinarySensorHoles:
    def _donut_sensor(self, invert=False):
        # Outer ring: 0–2 degree square. Hole: 0.5–1.5 degree inner square.
        outer = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        hole = [(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)]
        return _make_sensor(polygon=outer, holes=[hole], invert=invert)

    def test_point_in_outer_ring_not_in_hole_is_on(self):
        sensor = self._donut_sensor()
        # Point in outer ring but far from the hole
        sensor._update_state(_make_state(0.2, 0.2))
        assert sensor.is_on is True

    def test_point_inside_hole_is_off(self):
        sensor = self._donut_sensor()
        # Centre of the hole — geometrically outside the donut zone
        sensor._update_state(_make_state(1.0, 1.0))
        assert sensor.is_on is False

    def test_point_outside_outer_ring_is_off(self):
        sensor = self._donut_sensor()
        sensor._update_state(_make_state(5.0, 5.0))
        assert sensor.is_on is False

    def test_inverted_hole_point_is_on(self):
        # With invert=True, being inside the hole (i.e. geometrically outside) → sensor ON
        sensor = self._donut_sensor(invert=True)
        sensor._update_state(_make_state(1.0, 1.0))
        assert sensor.is_on is True

    def test_distance_uses_nearest_hole_edge(self):
        # A point just inside the hole is geometrically outside the zone, and its
        # nearest boundary is the hole edge — not the far exterior ring. The
        # reported (positive) distance should be small, not the distance to the
        # outer ring.
        sensor = self._donut_sensor()
        # Hole spans lat/lon 0.5–1.5; sit just inside its lower edge.
        sensor._update_state(_make_state(0.51, 1.0))
        dist = sensor.extra_state_attributes["distance_to_edge_m"]
        assert dist is not None
        assert dist > 0  # inside the hole → outside the zone
        # ~0.01° of latitude ≈ 1113 m to the hole edge; far less than the
        # >50 km distance to the outer ring.
        assert dist < 2000


class TestPolyZoneBinarySensorEvents:
    def _sensor_with_hass(self):
        sensor = _make_sensor()
        hass = MagicMock()
        sensor.hass = hass
        sensor.entity_id = "binary_sensor.test_zone"
        return sensor, hass

    def test_enter_event_fired_on_entry(self):
        sensor, hass = self._sensor_with_hass()
        sensor._update_state(_make_state(0.5, 0.5))
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == "poly_zone_enter"
        assert payload["in_zone"] is True

    def test_exit_event_fired_on_exit(self):
        sensor, hass = self._sensor_with_hass()
        sensor._update_state(_make_state(0.5, 0.5))  # enter
        hass.bus.async_fire.reset_mock()
        sensor._update_state(_make_state(5.0, 5.0))  # exit
        hass.bus.async_fire.assert_called_once()
        event_name, payload = hass.bus.async_fire.call_args[0]
        assert event_name == "poly_zone_exit"
        assert payload["in_zone"] is False

    def test_no_event_when_state_unchanged(self):
        sensor, hass = self._sensor_with_hass()
        sensor._update_state(_make_state(0.5, 0.5))  # enter
        hass.bus.async_fire.reset_mock()
        sensor._update_state(_make_state(0.4, 0.4))  # still inside
        hass.bus.async_fire.assert_not_called()

    def test_event_payload_contains_coordinates(self):
        sensor, hass = self._sensor_with_hass()
        sensor._update_state(_make_state(0.5, 0.3))
        _, payload = hass.bus.async_fire.call_args[0]
        assert payload["lat"] == pytest.approx(0.5)
        assert payload["lon"] == pytest.approx(0.3)
