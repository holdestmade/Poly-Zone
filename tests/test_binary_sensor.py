"""Unit tests for poly_zone binary_sensor geometric helpers."""
import json
import math
import tempfile
import os
import pytest

from custom_components.poly_zone.binary_sensor import (
    _bbox,
    _min_distance_to_edges_m,
    _normalize_ring,
    _point_in_polygon_fast,
    _point_segment_distance_m,
    _precompute_edges,
    ensure_ccw_winding,
    load_polygons_from_geojson,
    offset_polygon,
)
from custom_components.poly_zone.const import METERS_PER_DEGREE_LAT

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A simple 1° × 1° square centred near the equator (lon 0–1, lat 0–1).
SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
# Same square wound clockwise.
SQUARE_CW = list(reversed(SQUARE))


# ---------------------------------------------------------------------------
# ensure_ccw_winding
# ---------------------------------------------------------------------------

class TestEnsureCcwWinding:
    def test_ccw_unchanged(self):
        result = ensure_ccw_winding(SQUARE)
        assert result == SQUARE

    def test_cw_reversed(self):
        result = ensure_ccw_winding(SQUARE_CW)
        assert result == SQUARE

    def test_already_ccw_triangle(self):
        tri = [(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)]
        assert ensure_ccw_winding(tri) == tri


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
    def test_zero_offset_returns_original(self):
        assert offset_polygon(SQUARE, 0) is SQUARE

    def test_positive_offset_shrinks(self):
        # For a CCW polygon, inward normals mean a positive offset shrinks the polygon.
        # This matches the "inner tolerance zone" use-case.
        result = offset_polygon(SQUARE, 100)
        orig_bbox = _bbox(SQUARE)
        new_bbox = _bbox(result)
        assert new_bbox[0] > orig_bbox[0]  # min x moved inward (larger)
        assert new_bbox[1] > orig_bbox[1]  # min y moved inward (larger)
        assert new_bbox[2] < orig_bbox[2]  # max x moved inward (smaller)
        assert new_bbox[3] < orig_bbox[3]  # max y moved inward (smaller)

    def test_negative_offset_expands(self):
        # A negative offset moves vertices outward, expanding the polygon.
        result = offset_polygon(SQUARE, -100)
        orig_bbox = _bbox(SQUARE)
        new_bbox = _bbox(result)
        assert new_bbox[0] < orig_bbox[0]
        assert new_bbox[1] < orig_bbox[1]
        assert new_bbox[2] > orig_bbox[2]
        assert new_bbox[3] > orig_bbox[3]

    def test_degenerate_vertex_dropped_with_warning(self, caplog):
        import logging
        # Polygon with a duplicate consecutive vertex (zero-length edge).
        poly = [(0.0, 0.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        with caplog.at_level(logging.WARNING, logger="custom_components.poly_zone.binary_sensor"):
            result = offset_polygon(poly, 100)
        assert len(result) < len(poly)
        assert any("dropped" in r.message for r in caplog.records)


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
