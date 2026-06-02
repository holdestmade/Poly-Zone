import json
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

try:
    from homeassistant.config_entries import ConfigFlowResult
except ImportError:  # HA < 2024.4
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult  # type: ignore[assignment]

from .const import DOMAIN


def _read_and_validate_geojson(path: str) -> dict[str, Any]:
    """Read and parse a GeoJSON file.

    The blocking file I/O (open + stat) is contained here so the caller can run
    it via ``async_add_executor_job`` and keep the event loop unblocked. Raises
    ``FileNotFoundError`` if the path does not exist and ``ValueError`` for
    malformed JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


class PolyZoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Polygon Zone."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "PolyZoneOptionsFlowHandler":
        return PolyZoneOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            path = user_input.get("geojson_path", "")
            if not path:
                errors["base"] = "file_not_found"
            else:
                # All filesystem access (existence check + read) is offloaded to
                # the executor so the event loop is never blocked on disk I/O.
                try:
                    data = await self.hass.async_add_executor_job(
                        _read_and_validate_geojson, path
                    )
                except FileNotFoundError:
                    errors["base"] = "file_not_found"
                except (OSError, ValueError):
                    errors["base"] = "invalid_geojson"
                else:
                    features = data.get("features")
                    if not isinstance(features, list) or not features:
                        errors["base"] = "no_features"
                    elif not any(
                        ((feature or {}).get("geometry") or {}).get("type")
                        in {"Polygon", "MultiPolygon"}
                        for feature in features
                    ):
                        errors["base"] = "unsupported_geom"

            if not errors:
                # Prevent the same file + tracker pair being configured twice.
                await self.async_set_unique_id(f"{path}::{user_input['device_tracker']}")
                self._abort_if_unique_id_configured()
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

    # self.config_entry is set automatically by the HA flow manager; no __init__ needed.

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
