"""The Polygon Zone custom component."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import slugify

from .const import DOMAIN
from .geometry import Ring, load_polygons_from_geojson

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


@dataclass
class PolyZoneRuntimeData:
    """Polygon data loaded from the configured GeoJSON file."""

    rings: list[Ring]
    meta: list[dict[str, Any]]
    zone_ids: list[str]


type PolyZoneConfigEntry = ConfigEntry[PolyZoneRuntimeData]


def _zone_ids(meta: list[dict[str, Any]]) -> list[str]:
    """Return a stable, unique id slug per zone.

    Named features get a slug of their name so unique_ids survive reordering
    of the GeoJSON file; unnamed features fall back to their index.
    """
    ids: list[str] = []
    used: set[str] = set()
    for index, zone_meta in enumerate(meta):
        base = slugify(zone_meta.get("name") or "") or f"zone_{index + 1}"
        zone_id = base
        suffix = 2
        while zone_id in used:
            zone_id = f"{base}_{suffix}"
            suffix += 1
        used.add(zone_id)
        ids.append(zone_id)
    return ids


def _file_signature(path: str) -> tuple[float, int] | None:
    """Return (mtime, size) for path, or None if it cannot be stat'ed."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


async def async_setup_entry(hass: HomeAssistant, entry: PolyZoneConfigEntry) -> bool:
    """Set up Polygon Zone from a config entry."""
    geojson_path: str = entry.data["geojson_path"]

    try:
        rings, meta = await hass.async_add_executor_job(
            load_polygons_from_geojson, geojson_path
        )
    except OSError as err:
        # The file may live on storage that is not mounted yet; retry with backoff.
        raise ConfigEntryNotReady(f"Cannot read GeoJSON file {geojson_path}: {err}") from err
    except ValueError as err:
        raise ConfigEntryError(f"Invalid GeoJSON in {geojson_path}: {err}") from err
    if not rings:
        raise ConfigEntryError(f"No valid polygons found in {geojson_path}")

    entry.runtime_data = PolyZoneRuntimeData(rings=rings, meta=meta, zone_ids=_zone_ids(meta))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # File watcher is opt-in (default off) to avoid unexpected I/O overhead.
    if entry.options.get("watch_geojson", False):
        interval = max(5, int(entry.options.get("watch_interval", 60)))
        last_signature = await hass.async_add_executor_job(_file_signature, geojson_path)
        reload_scheduled = False

        async def _check_file(_now: Any) -> None:
            nonlocal last_signature, reload_scheduled
            if reload_scheduled:
                return
            signature = await hass.async_add_executor_job(_file_signature, geojson_path)
            # Reload when the file's mtime or size changes, or when it (re)appears
            # after having been missing at setup time (last_signature is None).
            if signature is not None and signature != last_signature:
                appeared = last_signature is None
                last_signature = signature
                reload_scheduled = True
                _LOGGER.info(
                    "GeoJSON %s; reloading Polygon Zone for %s",
                    "appeared" if appeared else "changed",
                    entry.title,
                )
                hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

        entry.async_on_unload(
            async_track_time_interval(hass, _check_file, timedelta(seconds=interval))
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PolyZoneConfigEntry) -> bool:
    """Unload a config entry; the file watcher is torn down via entry.async_on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: PolyZoneConfigEntry) -> None:
    """Handle a reload of the integration from the UI/options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PolyZoneConfigEntry, device: DeviceEntry
) -> bool:
    """Allow removing devices for zones that no longer exist in the GeoJSON file."""
    current = {(DOMAIN, f"{entry.entry_id}_{zone_id}") for zone_id in entry.runtime_data.zone_ids}
    return not any(identifier in current for identifier in device.identifiers)
