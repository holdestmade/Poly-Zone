"""The Polygon Zone custom component."""
from datetime import timedelta
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Polygon Zone from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # File watcher is opt-in (default off) to avoid unexpected I/O overhead.
    watch = entry.options.get("watch_geojson", False)
    interval = max(5, int(entry.options.get("watch_interval", 60)))
    geojson_path = entry.data.get("geojson_path")

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Clean up any prior watcher for this entry before (re-)creating one.
    _cleanup_watch(hass, entry)

    if watch and geojson_path:
        try:
            last_mtime = await hass.async_add_executor_job(os.path.getmtime, geojson_path)
        except OSError:
            last_mtime = None

        _reloading = False

        async def _check_mtime(_now) -> None:
            nonlocal last_mtime, _reloading
            if _reloading:
                return
            try:
                mtime = await hass.async_add_executor_job(os.path.getmtime, geojson_path)
            except OSError:
                mtime = None
            # Reload when the file's mtime changes, or when it (re)appears after
            # having been missing at setup time (last_mtime is None).
            if mtime and mtime != last_mtime:
                appeared = last_mtime is None
                last_mtime = mtime
                _reloading = True
                _LOGGER.info(
                    "GeoJSON %s; reloading Polygon Zone for %s",
                    "appeared" if appeared else "changed",
                    entry.title,
                )
                hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

        unsub = async_track_time_interval(hass, _check_mtime, timedelta(seconds=interval))
        hass.data[DOMAIN][entry.entry_id] = {"unsub": unsub}

    return True


def _cleanup_watch(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data and (unsub := data.get("unsub")):
        try:
            unsub()
        except TypeError:
            _LOGGER.debug("Watcher unsubscribe callback was invalid for %s", entry.entry_id)
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _cleanup_watch(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle a reload of the integration from the UI/options change."""
    await hass.config_entries.async_reload(entry.entry_id)
