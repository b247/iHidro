"""Platforma Button pentru iHidro HA — Trimitere autocitire."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HidroelectricaCoordinator
from .helpers import build_usage_entity, safe_get

def _extract_list(data: Any, list_key: str) -> list:
    """Extrage o listă dintr-un răspuns API care poate fi dict sau list."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get(list_key, []) or []
    return []

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Setup buttons (same logic as before to check for prosumers)."""
    entities: list[ButtonEntity] = []

    for uan, coordinator in config_entry.runtime_data.coordinators.items():
        # Keep your prosumer check here
        data = coordinator.data or {}
        mrh = data.get("meter_read_history")
        is_prosumer = False
        if mrh and isinstance(mrh, dict):
            mrh_data = safe_get(mrh, "result", "Data", default=[])
            if isinstance(mrh_data, list):
                is_prosumer = any(r.get("Registers") == "1.8.0_P" for r in mrh_data)

        if is_prosumer:
            continue

        entities.append(TrimiteIndexButton(coordinator, config_entry))

    async_add_entities(entities)

class TrimiteIndexButton(CoordinatorEntity[HidroelectricaCoordinator], ButtonEntity):
    """Button that reads from input_number and calls the shared logic."""

    _attr_has_entity_name = False

    def __init__(self, coordinator: HidroelectricaCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._uan = coordinator.uan
        self._attr_name = "Trimite index energie electrică"
        self._attr_unique_id = f"{DOMAIN}_trimite_index_{self._uan}"
        self._custom_entity_id = f"button.{DOMAIN}_{self._uan}_trimite_index_energie_electrica"
        self._input_number_entity = f"input_number.{DOMAIN}_{self._uan}_index_energie_electrica"

    @property
    def entity_id(self) -> str:
        return self._custom_entity_id

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._uan)},
            name=f"iHidro HA ({self._uan})",
            manufacturer="FOSS HACS",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Just get the value and pass it to the helper logic."""
        input_state = self.hass.states.get(self._input_number_entity)
        if not input_state:
            _LOGGER.error("Helper entity %s not found", self._input_number_entity)
            return

        try:
            # We convert it to a string/int here to be safe
            index_value = int(float(input_state.state))
            
            # CALL THE SHARED LOGIC
            await async_submit_index_logic(self.hass, self.coordinator, index_value)
            
        except (ValueError, TypeError) as err:
            _LOGGER.error("Invalid value in %s: %s", self._input_number_entity, err)
