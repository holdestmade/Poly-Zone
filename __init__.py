"""The Polygon Zone custom component."""
from datetime import timedelta
import os
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Polygon Zone from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Lightweight file watcher (optional)
    watch = entry.options.get("watch_geojson", True)
    interval = int(entry.options.get("watch_interval", 60))
    geojson_path = entry.data.get("geojson_path")

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Clean up any prior watcher for this entry
    _cleanup_watch(hass, entry)

    if watch and geojson_path:
        try:
            last_mtime = os.path.getmtime(geojson_path)
        except OSError:
            last_mtime = None

        def _check_mtime(now):
            nonlocal last_mtime
            try:
                mtime = os.path.getmtime(geojson_path)
            except OSError:
                mtime = None
            if mtime and last_mtime and mtime != last_mtime:
                _LOGGER.info("GeoJSON changed; reloading Polygon Zone for %s", entry.title)
                last_mtime = mtime
                hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
            elif mtime and not last_mtime:
                last_mtime = mtime

        unsub = async_track_time_interval(hass, _check_mtime, timedelta(seconds=max(5, interval)))
        hass.data[DOMAIN][entry.entry_id] = {"unsub": unsub}

    return True

def _cleanup_watch(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data and (unsub := data.get("unsub")):
        try:
            unsub()
        except Exception:  # defensive
            pass
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN].pop(entry.entry_id, None)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _cleanup_watch(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle a reload of the integration from the UI/options change."""
    await hass.config_entries.async_reload(entry.entry_id)
