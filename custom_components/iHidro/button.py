from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HidroelectricaCoordinator
from .helpers import async_submit_index_logic, safe_get # <--- Logic is imported here

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configurează butoanele pentru iHidro HA."""
    entities: list[ButtonEntity] = []

    for uan, coordinator in config_entry.runtime_data.coordinators.items():
        data = coordinator.data or {}
        mrh = data.get("meter_read_history")
        is_prosumer = False
        if mrh and isinstance(mrh, dict):
            mrh_data = safe_get(mrh, "result", "Data", default=[])
            if isinstance(mrh_data, list):
                is_prosumer = any(
                    r.get("Registers") == "1.8.0_P" for r in mrh_data
                )

        if is_prosumer:
            continue

        entities.append(TrimiteIndexButton(coordinator))

    async_add_entities(entities)


class TrimiteIndexButton(CoordinatorEntity[HidroelectricaCoordinator], ButtonEntity):
    """Buton care citește din input_number și folosește logica partajată."""

    _attr_has_entity_name = False

    def __init__(self, coordinator: HidroelectricaCoordinator) -> None:
        super().__init__(coordinator)
        self._uan = coordinator.uan

        self._attr_name = "Trimite index energie electrică"
        self._attr_icon = "mdi:send-circle"
        self._attr_translation_key = "trimite_index_energie_electrica"
        self._attr_unique_id = f"{DOMAIN}_trimite_index_{self._uan}"
        
        # FIX: Setăm entity_id direct, nu prin property, pentru a evita AttributeError
        self.entity_id = f"button.{DOMAIN}_{self._uan}_trimite_index_energie_electrica"

        # Entitatea input_number din care citim valoarea indexului
        self._input_number_entity = f"input_number.{DOMAIN}_{self._uan}_index_energie_electrica"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._uan)},
            name=f"iHidro HA ({self._uan})",
            manufacturer="FOSS HACS",
            model="iHidro HA",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Execută trimiterea folosind helper-ul partajat."""
        # 1. Citim valoarea din input_number helper
        input_state = self.hass.states.get(self._input_number_entity)
        if not input_state:
            _LOGGER.error("Helper-ul %s nu a fost găsit.", self._input_number_entity)
            return

        try:
            # 2. Convertim valoarea
            index_value = int(float(input_state.state))
            
            _LOGGER.debug("Buton apăsat: trimit index %s pentru UAN %s", index_value, self._uan)

            # 3. APELĂM LOGICA PARTAJATĂ (Aici se folosește importul)
            await async_submit_index_logic(self.hass, self.coordinator, index_value)

        except (TypeError, ValueError) as err:
            _LOGGER.error("Valoare invalidă în %s: %s", self._input_number_entity, err)
        except Exception:
            _LOGGER.exception("Eroare neașteptată la butonul de trimitere index.")
