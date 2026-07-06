"""Config and options flows for the Polygon Zone component."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN


def _read_geojson(path: str) -> dict[str, Any]:
    """Read and parse a GeoJSON file.

    The blocking file I/O is contained here so the caller can run it via
    ``async_add_executor_job`` and keep the event loop unblocked. Raises
    ``FileNotFoundError`` if the path is missing or not a regular file, and
    ``ValueError`` for malformed JSON.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("GeoJSON root must be a JSON object")
    return data


def _entry_unique_id(user_input: Mapping[str, Any]) -> str:
    """Unique id preventing the same file + tracker pair being configured twice."""
    return f"{user_input['geojson_path']}::{user_input['device_tracker']}"


def _entry_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("name", default=defaults.get("name", vol.UNDEFINED)): str,
            vol.Required(
                "geojson_path", default=defaults.get("geojson_path", vol.UNDEFINED)
            ): str,
            vol.Required(
                "device_tracker", default=defaults.get("device_tracker", vol.UNDEFINED)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="device_tracker"),
            ),
            vol.Optional("tolerance", default=defaults.get("tolerance", 0)): vol.Coerce(float),
        }
    )


class PolyZoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Polygon Zone."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "PolyZoneOptionsFlowHandler":
        return PolyZoneOptionsFlowHandler()

    async def _async_validate_geojson(self, path: str) -> dict[str, str]:
        """Validate the GeoJSON file, returning form errors (empty dict if OK)."""
        if not path:
            return {"base": "file_not_found"}
        try:
            data = await self.hass.async_add_executor_job(_read_geojson, path)
        except FileNotFoundError:
            return {"base": "file_not_found"}
        except (OSError, ValueError):
            return {"base": "invalid_geojson"}

        features = data.get("features")
        if not isinstance(features, list) or not features:
            return {"base": "no_features"}
        if not any(
            ((feature or {}).get("geometry") or {}).get("type")
            in {"Polygon", "MultiPolygon"}
            for feature in features
        ):
            return {"base": "unsupported_geom"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_validate_geojson(user_input.get("geojson_path", ""))
            if not errors:
                await self.async_set_unique_id(_entry_unique_id(user_input))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input["name"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_entry_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow changing the file, tracker, name or tolerance without re-adding."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_validate_geojson(user_input.get("geojson_path", ""))
            if not errors:
                unique_id = _entry_unique_id(user_input)
                for other in self._async_current_entries():
                    if other.entry_id != entry.entry_id and other.unique_id == unique_id:
                        return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    title=user_input["name"],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entry_schema(user_input or entry.data),
            errors=errors,
        )


class PolyZoneOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Polygon Zone."""

    # self.config_entry is provided automatically by the HA flow manager.

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "tolerance",
                        default=self.config_entry.options.get(
                            "tolerance", self.config_entry.data.get("tolerance", 0)
                        ),
                    ): vol.Coerce(float),
                    vol.Optional(
                        "invert",
                        default=self.config_entry.options.get("invert", False),
                    ): bool,
                    vol.Optional(
                        "watch_geojson",
                        default=self.config_entry.options.get("watch_geojson", False),
                    ): bool,
                    vol.Optional(
                        "watch_interval",
                        default=self.config_entry.options.get("watch_interval", 60),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5)),
                }
            ),
        )
