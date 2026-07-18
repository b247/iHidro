from __future__ import annotations

import logging
import voluptuous as vol
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import HidroelectricaApiClient
from .const import (
    CONF_ACCOUNT_METADATA,
    CONF_PASSWORD,
    CONF_SELECTED_ACCOUNTS,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    DOMAIN_TOKEN_STORE,
    PLATFORMS,
)
from .coordinator import HidroelectricaCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class HidroelectricaRuntimeData:
    """Structură tipizată pentru datele runtime ale integrării."""

    coordinators: dict[str, HidroelectricaCoordinator] = field(default_factory=dict)
    api_client: HidroelectricaApiClient | None = None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Configurează integrarea globală iHidro HA."""
    _LOGGER.debug("Inițializare globală integrare: %s", DOMAIN)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configurează integrarea pentru o intrare specifică (config entry)."""
    _LOGGER.info(
        "Se configurează integrarea %s (entry_id=%s).",
        DOMAIN,
        entry.entry_id,
    )

    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass, verify_ssl=False)
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    update_interval = entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    # Conturi selectate
    selected_accounts = entry.data.get(CONF_SELECTED_ACCOUNTS, [])
    if not selected_accounts:
        _LOGGER.error(
            "Nu există conturi selectate pentru %s (entry_id=%s).",
            DOMAIN,
            entry.entry_id,
        )
        return False

    _LOGGER.debug(
        "Conturi selectate pentru %s (entry_id=%s): %s, interval=%ss.",
        DOMAIN,
        entry.entry_id,
        selected_accounts,
        update_interval,
    )

    # Un singur client API partajat (un singur cont, un singur token)
    api_client = HidroelectricaApiClient(session, username, password)

    # Injectăm token-ul salvat:
    # 1. hass.data (proaspăt, de la config_flow)
    # 2. config_entry.data (persistent, pentru restart HA)
    token_store = hass.data.get(DOMAIN_TOKEN_STORE, {})
    stored_token = token_store.pop(username.lower(), None)
    if stored_token:
        api_client.inject_token(stored_token)
        _LOGGER.debug(
            "Token injectat din config_flow pentru %s.", username
        )
    elif entry.data.get("token_data"):
        api_client.inject_token(entry.data["token_data"])
        _LOGGER.debug(
            "Token injectat din config_entry.data pentru %s.", username
        )
    else:
        _LOGGER.debug(
            "Niciun token salvat. Se va face login la primul refresh (%s).",
            username,
        )

    # Curățăm store-ul dacă e gol
    if DOMAIN_TOKEN_STORE in hass.data and not hass.data[DOMAIN_TOKEN_STORE]:
        hass.data.pop(DOMAIN_TOKEN_STORE, None)

    # Metadatele conturilor
    account_metadata = entry.data.get(CONF_ACCOUNT_METADATA, {})

    _LOGGER.debug(
        "account_metadata pentru entry_id=%s: %s",
        entry.entry_id,
        {k: {mk: mv for mk, mv in v.items() if mk in ("accountNumber", "pod")}
         for k, v in account_metadata.items()} if account_metadata else "GOL",
    )

    # Fallback: dacă metadata nu conține accountNumber (config entry vechi),
    # obținem conturile din API pentru a completa
    acc_number_map: dict[str, str] = {}
    for uan_key, meta_val in account_metadata.items():
        if meta_val.get("accountNumber"):
            acc_number_map[uan_key] = meta_val["accountNumber"]

    if selected_accounts and not acc_number_map:
        _LOGGER.debug(
            "Metadata nu conține accountNumber. Se obțin conturile din API."
        )
        try:
            await api_client.async_ensure_authenticated()
            fresh_accounts = await api_client.async_fetch_utility_accounts()
            for fa in fresh_accounts:
                fa_uan = fa.get("contractAccountID", "").strip()
                fa_acc = fa.get("accountNumber", "").strip()
                if fa_uan and fa_acc:
                    acc_number_map[fa_uan] = fa_acc
        except Exception as err:
            _LOGGER.warning(
                "Nu s-au putut obține conturile din API pentru fallback: %s", err
            )

    # Creăm câte un coordinator per cont selectat
    coordinators: dict[str, HidroelectricaCoordinator] = {}

    for uan in selected_accounts:
        meta = account_metadata.get(uan, {})
        acc_number = meta.get("accountNumber", "") or acc_number_map.get(uan, "")

        _LOGGER.info(
            "Coordinator UAN=%s: AccountNumber='%s' "
            "(sursa=%s).",
            uan,
            acc_number,
            "metadata" if meta.get("accountNumber") else
            ("api_fallback" if acc_number_map.get(uan) else "GOL!"),
        )

        coordinator = HidroelectricaCoordinator(
            hass,
            api_client=api_client,
            uan=uan,
            account_number=acc_number,
            update_interval=update_interval,
            config_entry=entry,
        )

        try:
            await coordinator.async_config_entry_first_refresh()
        except UpdateFailed as err:
            _LOGGER.error(
                "Prima actualizare eșuată (entry_id=%s, UAN=%s): %s",
                entry.entry_id,
                uan,
                err,
            )
            continue
        except Exception as err:
            _LOGGER.exception(
                "Eroare neașteptată la prima actualizare (entry_id=%s, UAN=%s): %s",
                entry.entry_id,
                uan,
                err,
            )
            continue

        coordinators[uan] = coordinator

    if not coordinators:
        _LOGGER.error(
            "Niciun coordinator inițializat cu succes pentru %s (entry_id=%s).",
            DOMAIN,
            entry.entry_id,
        )
        return False

    _LOGGER.info(
        "%s coordinatoare active din %s conturi selectate (entry_id=%s).",
        len(coordinators),
        len(selected_accounts),
        entry.entry_id,
    )

    # Salvăm datele runtime + entry_id
    hass.data[DOMAIN][entry.entry_id] = entry
    entry.runtime_data = HidroelectricaRuntimeData(
        coordinators=coordinators,
        api_client=api_client,
    )

    # EM index send handler
    async def handle_trimite_index(call):
        """Service call handler."""
        uan = call.data.get("uan")
        index_val = call.data.get("index")
        
        # Access the coordinators from runtime_data (where you saved them earlier)
        # entry.runtime_data is the HidroelectricaRuntimeData object you defined
        coordinator = entry.runtime_data.coordinators.get(uan)
        
        if not coordinator:
            _LOGGER.error("UAN %s nu a fost găsit în această instanță.", uan)
            return

        from .helpers import async_submit_index_logic
        await async_submit_index_logic(hass, coordinator, index_val)

    hass.services.async_register(
        DOMAIN,
        "trimite_index_manual",
        handle_trimite_index,
        schema=vol.Schema({
            vol.Required("uan"): cv.string,
            vol.Required("index"): cv.positive_int,
        }),
    )

    # Încărcăm platformele (sensor + button)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listener pentru modificarea opțiunilor
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _LOGGER.info(
        "Integrarea %s configurată (entry_id=%s, conturi=%s).",
        DOMAIN,
        entry.entry_id,
        list(coordinators.keys()),
    )
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reîncarcă integrarea când opțiunile se schimbă."""
    _LOGGER.info(
        "Opțiunile integrării %s s-au schimbat (entry_id=%s). Se reîncarcă...",
        DOMAIN,
        entry.entry_id,
    )
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> bool:
    """Migrare de la versiuni vechi la versiunea curentă (v3)."""
    _LOGGER.debug(
        "Migrare config entry %s de la versiunea %s.",
        config_entry.entry_id,
        config_entry.version,
    )

    if config_entry.version < 3:
        old_data = dict(config_entry.data)

        new_data = {
            CONF_USERNAME: old_data.get(CONF_USERNAME, old_data.get("username", "")),
            CONF_PASSWORD: old_data.get(CONF_PASSWORD, old_data.get("password", "")),
            CONF_UPDATE_INTERVAL: old_data.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            ),
            "select_all": False,
            CONF_SELECTED_ACCOUNTS: [],
        }

        # Preservă token-ul de autentificare (dacă există)
        if old_data.get("token_data"):
            new_data["token_data"] = old_data["token_data"]

        _LOGGER.info(
            "Migrare entry %s: v%s → v3.",
            config_entry.entry_id,
            config_entry.version,
        )

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options={}, version=3
        )
        return True

    _LOGGER.error(
        "Versiune necunoscută pentru migrare: %s (entry_id=%s).",
        config_entry.version,
        config_entry.entry_id,
    )
    return False
