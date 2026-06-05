"""The Grosfarm integration.

Our parent config entry owns one zone. On setup, if no child helper has
been wired yet, we programmatically spawn the appropriate helper
(`generic_thermostat` for heating, `generic_hygrostat` for humidifying)
pre-populated with the user's choices plus our hardware-safety defaults.
Monitoring zones have no child — the sensor is just recorded for future
cloud telemetry. On removal, any child is cascade-removed.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.generic_hygrostat import (
    CONF_DEVICE_CLASS as HG_CONF_DEVICE_CLASS,
)
from homeassistant.components.generic_hygrostat import (
    CONF_DRY_TOLERANCE as HG_CONF_DRY_TOLERANCE,
)
from homeassistant.components.generic_hygrostat import (
    CONF_HUMIDIFIER as HG_CONF_HUMIDIFIER,
)
from homeassistant.components.generic_hygrostat import (
    CONF_MIN_DUR as HG_CONF_MIN_DUR,
)
from homeassistant.components.generic_hygrostat import (
    CONF_SENSOR as HG_CONF_SENSOR,
)
from homeassistant.components.generic_hygrostat import (
    CONF_WET_TOLERANCE as HG_CONF_WET_TOLERANCE,
)
from homeassistant.components.generic_hygrostat import (
    DOMAIN as GENERIC_HYGROSTAT_DOMAIN,
)
from homeassistant.components.generic_thermostat.const import (
    CONF_AC_MODE,
    CONF_COLD_TOLERANCE,
    CONF_HOT_TOLERANCE,
    CONF_MIN_DUR,
)
from homeassistant.components.generic_thermostat.const import (
    CONF_HEATER as GT_CONF_HEATER,
)
from homeassistant.components.generic_thermostat.const import (
    CONF_SENSOR as GT_CONF_SENSOR,
)
from homeassistant.components.generic_thermostat.const import (
    DOMAIN as GENERIC_THERMOSTAT_DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_NAME as HA_CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from .api import GrosfarmAPIError, GrosfarmAuthError
from .const import (
    CONF_CHILD_ENTRY_ID,
    CONF_HEATER,
    CONF_HUMIDIFIER,
    CONF_HUMIDIFIER_DEVICE_CLASS,
    CONF_HUMIDITY_SENSOR,
    CONF_LIGHT,
    CONF_PRESET_TEMPS,
    CONF_PRESET_TYPE,
    CONF_TARGET_HUMIDITY,
    CONF_TARGET_SENSOR,
    DOMAIN,
    PLATFORMS,
    PRESET_TYPE_CLOUD,
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
    PRESET_TYPE_LIGHTING,
    PRESET_TYPE_MONITORING,
    SAFE_COLD_TOLERANCE,
    SAFE_DRY_TOLERANCE,
    SAFE_HOT_TOLERANCE,
    SAFE_MIN_CYCLE_SECONDS,
    SAFE_WET_TOLERANCE,
)
from .coordinator import GrosfarmCoordinator
from .light_controller import GrosfarmLightController

_LOGGER = logging.getLogger(__name__)

_CLOUD_KEY = "_cloud"  # ключ для GrosfarmCoordinator-инстансов в hass.data[DOMAIN]
_LIGHT_KEY = "_light"  # ключ для GrosfarmLightController-инстансов в hass.data[DOMAIN]
_SERVICE_REGISTERED_KEY = "_services_registered"

SERVICE_CALIBRATE_LAMP = "calibrate_lamp"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Grosfarm config entry."""
    _LOGGER.debug("Setting up Grosfarm entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}

    preset_type = entry.data.get(CONF_PRESET_TYPE, PRESET_TYPE_HEATING)

    if preset_type == PRESET_TYPE_CLOUD:
        coordinator = GrosfarmCoordinator(hass, entry)
        try:
            await coordinator.async_start()
        except GrosfarmAuthError as exc:
            # Неверные креды/api_key — реальная ошибка конфигурации (≠ недоступное
            # облако, которое coordinator уводит в автономный режим). Используем
            # ConfigEntryError, а не ConfigEntryAuthFailed: reauth-шага в
            # config_flow пока нет, а AuthFailed автоматически инициирует
            # несуществующий шаг `reauth` → UnknownStep-краш.
            from homeassistant.exceptions import ConfigEntryError

            raise ConfigEntryError(str(exc)) from exc
        except GrosfarmAPIError as exc:
            # Защитный фоллбэк: штатно async_start уводит недоступное облако в
            # автономный режим и сюда не доходит, но если GrosfarmAPIError всё же
            # всплывёт — пусть HA ретраит, а не падает в SETUP_ERROR.
            from homeassistant.exceptions import ConfigEntryNotReady

            raise ConfigEntryNotReady(str(exc)) from exc
        hass.data[DOMAIN].setdefault(_CLOUD_KEY, {})[entry.entry_id] = coordinator
        # Cloud-entry тоже forward'ит SENSOR — для диагностических entities.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    # Сначала поднимаем helper/controller для зоны, потом forward'им SENSOR-
    # платформу: sensor.py ищет controller в hass.data, и порядок здесь важен.
    if CONF_CHILD_ENTRY_ID not in entry.data:
        await _maybe_spawn_zone_helper(hass, entry, preset_type)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _notify_cloud_topology_changed(hass)
    return True


async def _maybe_spawn_zone_helper(
    hass: HomeAssistant, entry: ConfigEntry, preset_type: str
) -> None:
    """Развилка по preset_type на установку зон-помощников.

    Heating/humidifying — spawn helper если есть actuator.
    Monitoring/lighting — нет helper'а (sensor-only или cloud-driven switch).
    """
    if preset_type == PRESET_TYPE_HEATING:
        if entry.data.get(CONF_HEATER):
            await _spawn_generic_thermostat_child(hass, entry)
        else:
            _LOGGER.debug(
                "Temperature zone %s has no heater — sensor-only", entry.entry_id
            )
    elif preset_type == PRESET_TYPE_HUMIDIFYING:
        if entry.data.get(CONF_HUMIDIFIER):
            await _spawn_generic_hygrostat_child(hass, entry)
        else:
            _LOGGER.debug(
                "Humidity zone %s has no humidifier — sensor-only", entry.entry_id
            )
    elif preset_type == PRESET_TYPE_MONITORING:
        _LOGGER.debug(
            "Other-indicator zone %s — sensor only, no control loop",
            entry.entry_id,
        )
    elif preset_type == PRESET_TYPE_LIGHTING:
        # Локальный controller считает DLI и решает когда жечь по mode
        # (off/natural_supplement/indoor_supplement/indoor_continuous) от cloud.
        # Поднимаем для ЛЮБОЙ lighting-zone: без CONF_LIGHT controller просто
        # копит DLI и отправляет телеметрию + статус, _ensure_lamp работает
        # как no-op. Sensor-only зоны тоже получают дашборд-entities.
        controller = GrosfarmLightController(hass, entry)
        await controller.async_start()
        hass.data[DOMAIN].setdefault(_LIGHT_KEY, {})[entry.entry_id] = controller
        if entry.data.get(CONF_LIGHT):
            _ensure_lamp_service_registered(hass)
        _LOGGER.info(
            "Lighting controller started for zone %s (lamp=%s)",
            entry.entry_id,
            entry.data.get(CONF_LIGHT) or "none",
        )
    else:
        _LOGGER.warning(
            "Unknown preset_type %r on entry %s — no helper spawned",
            preset_type,
            entry.entry_id,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Grosfarm config entry. The child helper survives reloads."""
    preset_type = entry.data.get(CONF_PRESET_TYPE)
    if preset_type == PRESET_TYPE_CLOUD:
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        cloud_map = hass.data.get(DOMAIN, {}).get(_CLOUD_KEY, {})
        coordinator = cloud_map.pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True

    light_map = hass.data.get(DOMAIN, {}).get(_LIGHT_KEY, {})
    light_controller = light_map.pop(entry.entry_id, None)
    if light_controller is not None:
        await light_controller.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _notify_cloud_topology_changed(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cascade-remove the spawned child helper, if any (monitoring has none)."""
    if entry.data.get(CONF_PRESET_TYPE) == PRESET_TYPE_CLOUD:
        return  # cloud entries don't spawn helpers
    child_id = entry.data.get(CONF_CHILD_ENTRY_ID)
    if not child_id:
        return
    if hass.config_entries.async_get_entry(child_id) is None:
        _LOGGER.debug("Child %s already gone (manual removal?)", child_id)
        return
    _LOGGER.debug("Cascade-removing child entry %s", child_id)
    await hass.config_entries.async_remove(child_id)


def _notify_cloud_topology_changed(hass: HomeAssistant) -> None:
    """Триггер re-registration всех cloud-coordinator'ов после смены состава зон."""
    cloud_map = hass.data.get(DOMAIN, {}).get(_CLOUD_KEY, {})
    for coordinator in cloud_map.values():
        coordinator.request_reregister()


def _ensure_lamp_service_registered(hass: HomeAssistant) -> None:
    """Регистрирует сервис grosfarm.calibrate_lamp один раз на DOMAIN."""
    if hass.data[DOMAIN].get(_SERVICE_REGISTERED_KEY):
        return

    import voluptuous as vol

    async def _handler(call: Any) -> None:
        entry_id = call.data.get("entry_id")
        light_map = hass.data.get(DOMAIN, {}).get(_LIGHT_KEY, {})
        controller = light_map.get(entry_id)
        if controller is None:
            _LOGGER.warning("calibrate_lamp: zone %s not found", entry_id)
            return
        try:
            result = await controller.async_calibrate()
            _LOGGER.info("calibrate_lamp result for %s: %s", entry_id, result)
        except Exception:
            _LOGGER.exception("calibrate_lamp failed for %s", entry_id)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE_LAMP,
        _handler,
        schema=vol.Schema({vol.Required("entry_id"): str}),
    )
    hass.data[DOMAIN][_SERVICE_REGISTERED_KEY] = True


# ---------------------------------------------------------------------------
# Programmatic spawn — heating
# ---------------------------------------------------------------------------


async def _spawn_generic_thermostat_child(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Drive generic_thermostat's SchemaConfigFlowHandler programmatically.

    Two-step flow:
      1. `user` step — hardware + locked-down safety values.
      2. `presets` step — per-preset target temperatures from our parent.

    Stores the resulting child entry id on our parent's `data`.
    """
    presets: dict[str, float] = entry.data[CONF_PRESET_TEMPS]

    result = await hass.config_entries.flow.async_init(
        GENERIC_THERMOSTAT_DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            HA_CONF_NAME: entry.title,
            CONF_AC_MODE: False,
            GT_CONF_SENSOR: entry.data[CONF_TARGET_SENSOR],
            GT_CONF_HEATER: entry.data[CONF_HEATER],
            CONF_COLD_TOLERANCE: SAFE_COLD_TOLERANCE,
            CONF_HOT_TOLERANCE: SAFE_HOT_TOLERANCE,
            CONF_MIN_DUR: _min_cycle_dict(),
        },
    )
    if result["type"] is not FlowResultType.FORM:
        msg = f"generic_thermostat flow returned unexpected step: {result}"
        raise HomeAssistantError(msg)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=presets
    )
    _record_child(hass, entry, result, "generic_thermostat")


# ---------------------------------------------------------------------------
# Programmatic spawn — humidifying
# ---------------------------------------------------------------------------


async def _spawn_generic_hygrostat_child(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Drive generic_hygrostat's SchemaConfigFlowHandler programmatically.

    Single-step flow — generic_hygrostat doesn't expose per-preset
    setpoints (only `target_humidity` / `away_humidity`). The cloud
    pushes the current phase's target via OptionsFlow once it lands.
    """
    result = await hass.config_entries.flow.async_init(
        GENERIC_HYGROSTAT_DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            HA_CONF_NAME: entry.title,
            HG_CONF_DEVICE_CLASS: entry.data[CONF_HUMIDIFIER_DEVICE_CLASS],
            HG_CONF_SENSOR: entry.data[CONF_HUMIDITY_SENSOR],
            HG_CONF_HUMIDIFIER: entry.data[CONF_HUMIDIFIER],
            HG_CONF_DRY_TOLERANCE: SAFE_DRY_TOLERANCE,
            HG_CONF_WET_TOLERANCE: SAFE_WET_TOLERANCE,
            HG_CONF_MIN_DUR: _min_cycle_dict(),
        },
    )
    _record_child(hass, entry, result, "generic_hygrostat")

    # Seed the initial target humidity onto the child via update_entry —
    # generic_hygrostat's options schema doesn't include target_humidity in
    # its config_flow, but it reads it from entry.options at runtime.
    child_id = entry.data[CONF_CHILD_ENTRY_ID]
    child = hass.config_entries.async_get_entry(child_id)
    if child is not None:
        hass.config_entries.async_update_entry(
            child,
            options={
                **child.options,
                CONF_TARGET_HUMIDITY: entry.data[CONF_TARGET_HUMIDITY],
            },
        )


# ---------------------------------------------------------------------------
# Shared spawn-side helpers
# ---------------------------------------------------------------------------


def _min_cycle_dict() -> dict[str, int]:
    """Render the locked min_cycle_duration as the dict shape HA expects."""
    return {
        "hours": 0,
        "minutes": SAFE_MIN_CYCLE_SECONDS // 60,
        "seconds": SAFE_MIN_CYCLE_SECONDS % 60,
    }


def _record_child(
    hass: HomeAssistant,
    entry: ConfigEntry,
    result: ConfigFlowResult,
    helper_name: str,
) -> None:
    """Persist the spawned child entry's id on our parent entry's data."""
    if result["type"] is not FlowResultType.CREATE_ENTRY:
        msg = f"{helper_name} flow failed to create entry: {result}"
        raise HomeAssistantError(msg)

    child_entry: ConfigEntry = result["result"]
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CHILD_ENTRY_ID: child_entry.entry_id}
    )
    _LOGGER.info(
        "Spawned %s helper %s for Grosfarm entry %s",
        helper_name,
        child_entry.entry_id,
        entry.entry_id,
    )
