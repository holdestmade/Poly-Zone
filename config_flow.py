import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
import os
import json

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
            # validate file exists
            path = user_input.get("geojson_path")
            if not os.path.exists(path):
                errors["base"] = "file_not_found"
            else:
                # lightweight GeoJSON validation
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    features = data.get("features")
                    if not features:
                        errors["base"] = "no_features"
                    else:
                        geom = (features[0] or {}).get("geometry") or {}
                        gtype = geom.get("type")
                        if gtype not in ("Polygon", "MultiPolygon"):
                            errors["base"] = "unsupported_geom"
                except Exception:
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
                        default=self.config_entry.options.get("tolerance", self.config_entry.data.get("tolerance", 0)),
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
                    ): vol.Coerce(int),
                }
            ),
        )
