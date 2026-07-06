"""Binary sensors reporting whether a device tracker is inside polygon zones."""
from __future__ import annotations

import logging
from typing import Any

import shapely
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import PolyZoneConfigEntry
from .const import DOMAIN
from .geometry import Ring, offset_polygon, point_edges_distance_m, precompute_edges

_LOGGER = logging.getLogger(__name__)


def _build_tolerance_geometry(
    ring: Ring, holes: list[Ring], tolerance: float
) -> tuple[Ring, list[Ring]]:
    """Grow the exterior ring outward and shrink its holes inward by ``tolerance`` metres."""
    offset_poly = offset_polygon(ring, tolerance)
    if not offset_poly:
        return [], []
    # The tolerated zone grows outward, so its holes (voids) shrink inward by
    # the same distance; holes that collapse are dropped.
    tol_holes: list[Ring] = []
    for hole in holes:
        shrunk = offset_polygon(hole, -tolerance)
        if len(shrunk) >= 3:
            tol_holes.append(shrunk)
    return offset_poly, tol_holes


def _build_shape(ring: Ring, holes: list[Ring], zone_name: str) -> BaseGeometry:
    """Build a prepared shapely geometry for fast point containment tests.

    Self-intersecting rings (easy to draw by hand) are repaired so GEOS
    predicates stay well-defined.
    """
    shape: BaseGeometry = Polygon(ring, holes or None)
    if not shape.is_valid:
        repaired = make_valid(shape)
        if repaired.geom_type == "GeometryCollection":
            polygons = [g for g in repaired.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            repaired = unary_union(polygons) if polygons else shape
        if not repaired.is_empty:
            shape = repaired
        _LOGGER.warning(
            "Polygon for zone '%s' is self-intersecting or otherwise invalid; "
            "it was repaired automatically",
            zone_name,
        )
    shapely.prepare(shape)
    return shape


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


# --- Platform setup ---


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PolyZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    name = entry.data["name"]
    device_tracker_entity_id = entry.data["device_tracker"]
    tolerance = float(entry.options.get("tolerance", entry.data.get("tolerance", 0)))
    invert = bool(entry.options.get("invert", False))

    if not hass.states.get(device_tracker_entity_id):
        _LOGGER.warning(
            "Device tracker '%s' is not available yet. "
            "Poly-Zone sensors will update once it reports a state.",
            device_tracker_entity_id,
        )

    label_exact = "Outside Exact Zone" if invert else "Inside Exact Zone"
    label_tol = "Outside Tolerated Zone" if invert else "Inside Tolerated Zone"

    entities: list[PolyZoneBinarySensor] = []
    for index, (ring, zone_meta, zone_id) in enumerate(
        zip(runtime.rings, runtime.meta, runtime.zone_ids)
    ):
        zone_name = zone_meta.get("name") or f"Zone {index + 1}"
        holes: list[Ring] = zone_meta.get("holes", [])
        device_name = f"{name} - {zone_name}"

        entities.append(
            PolyZoneBinarySensor(
                device_name,
                ring,
                holes,
                zone_name,
                device_tracker_entity_id,
                entry,
                zone_id,
                "exact",
                label_exact,
                invert,
            )
        )

        if tolerance > 0:
            # pyproj/shapely offsetting is CPU-bound; keep it off the event loop.
            offset_poly, tol_holes = await hass.async_add_executor_job(
                _build_tolerance_geometry, ring, holes, tolerance
            )
            if offset_poly:
                entities.append(
                    PolyZoneBinarySensor(
                        device_name,
                        offset_poly,
                        tol_holes,
                        zone_name,
                        device_tracker_entity_id,
                        entry,
                        zone_id,
                        "tolerance",
                        label_tol,
                        invert,
                        diagnostic=True,
                    )
                )

    async_add_entities(entities)

    last_coords: tuple[Any, Any] | None = None

    # A single state-change listener feeds every zone sensor, rather than each
    # entity subscribing independently to the same device tracker.
    @callback
    def _handle_state_change(event: Event[EventStateChangedData]) -> None:
        nonlocal last_coords
        new_state = event.data["new_state"]
        if new_state is None:
            return
        coords = (
            new_state.attributes.get("latitude"),
            new_state.attributes.get("longitude"),
        )
        # Skip attribute-only updates (battery, source type, ...) where the
        # position has not changed.
        if coords == last_coords:
            return
        last_coords = coords
        for entity in entities:
            entity.update_from_shared_state(new_state)

    entry.async_on_unload(
        async_track_state_change_event(
            hass, device_tracker_entity_id, _handle_state_change
        )
    )


# --- Entity ---


class PolyZoneBinarySensor(BinarySensorEntity):
    """Representation of a Polygon Zone binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_should_poll = False

    def __init__(
        self,
        device_name: str,
        polygon: Ring,
        holes: list[Ring],
        zone_name: str,
        device_tracker_entity_id: str,
        entry: PolyZoneConfigEntry,
        zone_id: str,
        kind: str,
        entity_name: str,
        invert: bool,
        diagnostic: bool = False,
    ) -> None:
        self._zone_name = zone_name
        self._device_tracker_entity_id = device_tracker_entity_id
        self._is_on = False
        self._inside_geo = False
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._distance_m: float | None = None
        self._last_transition: str | None = None
        self._invert = invert

        self._shape = _build_shape(polygon, holes, zone_name)
        # All boundary edges (exterior + holes) feed the distance attribute.
        edges = precompute_edges(polygon)
        for hole in holes:
            edges.extend(precompute_edges(hole))
        self._edges = edges

        self._attr_name = entity_name
        self._attr_unique_id = f"{entry.entry_id}_{zone_id}_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{zone_id}")},
            name=device_name,
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
        # State changes are dispatched centrally by the platform's single
        # listener; here we only seed the initial state on add.
        if initial_state := self.hass.states.get(self._device_tracker_entity_id):
            self._update_state(initial_state)
            self.async_write_ha_state()

    @callback
    def update_from_shared_state(self, state: State) -> None:
        """Recompute and publish state from a tracker update (shared listener)."""
        if self.hass is None or self.entity_id is None:
            # Not fully added yet; async_added_to_hass will seed the state.
            return
        self._update_state(state)
        self.async_write_ha_state()

    def _update_state(self, state: State) -> None:
        self._latitude = _as_float(state.attributes.get("latitude"))
        self._longitude = _as_float(state.attributes.get("longitude"))

        prev = self._is_on
        if self._latitude is not None and self._longitude is not None:
            inside_geo = bool(shapely.contains_xy(self._shape, self._longitude, self._latitude))
            self._inside_geo = inside_geo
            self._is_on = (not inside_geo) if self._invert else inside_geo
            # Signed distance (m) to the nearest boundary (exterior or hole):
            # negative when geometrically inside the zone, positive outside.
            raw_dist = point_edges_distance_m(self._longitude, self._latitude, self._edges)
            self._distance_m = -raw_dist if inside_geo else raw_dist
        else:
            self._inside_geo = False
            self._is_on = False
            self._distance_m = None

        if prev != self._is_on:
            self._last_transition = dt_util.utcnow().isoformat()
            evt = f"{DOMAIN}_{'enter' if self._is_on else 'exit'}"
            self.hass.bus.async_fire(
                evt,
                {
                    "entity_id": self.entity_id,
                    "zone_name": self._zone_name,
                    "device_tracker": self._device_tracker_entity_id,
                    "in_zone": self._is_on,
                    "lat": self._latitude,
                    "lon": self._longitude,
                },
            )
