import json
import os

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN


class PolyZoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Polygon Zone."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PolyZoneOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            path = user_input.get("geojson_path", "")
            if not path or not os.path.isfile(path):
                errors["base"] = "file_not_found"
            else:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    features = data.get("features")
                    if not isinstance(features, list) or not features:
                        errors["base"] = "no_features"
                    else:
                        has_supported_geometry = False
                        for feature in features:
                            geometry = (feature or {}).get("geometry") or {}
                            if geometry.get("type") in {"Polygon", "MultiPolygon"}:
                                has_supported_geometry = True
                                break
                        if not has_supported_geometry:
                            errors["base"] = "unsupported_geom"
                except (OSError, ValueError):
                    errors["base"] = "invalid_geojson"

            if not errors:
                return self.async_create_entry(title=user_input["name"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("geojson_path"): str,
                    vol.Required("device_tracker"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="device_tracker"),
                    ),
                    vol.Optional("tolerance", default=0): vol.Coerce(float),
                }
            ),
            errors=errors,
        )


class PolyZoneOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Polygon Zone."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
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
                        default=self.config_entry.options.get("watch_geojson", True),
                    ): bool,
                    vol.Optional(
                        "watch_interval",
                        default=self.config_entry.options.get("watch_interval", 60),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5)),
                }
            ),
        )
