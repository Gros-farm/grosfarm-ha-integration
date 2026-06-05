"""Config-flow tests for the Grosfarm integration."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.grosfarm.const import (
    CONF_AREA_ID,
    CONF_CHILD_ENTRY_ID,
    CONF_HEATER,
    CONF_HUMIDIFIER,
    CONF_HUMIDIFIER_DEVICE_CLASS,
    CONF_HUMIDITY_SENSOR,
    CONF_NAME,
    CONF_PRESET_TEMPS,
    CONF_PRESET_TYPE,
    CONF_SENSOR,
    CONF_TARGET_HUMIDITY,
    CONF_TARGET_SENSOR,
    DEFAULT_PRESET_TEMPS,
    DEFAULT_TARGET_HUMIDITY,
    DOMAIN,
    ERROR_HEATER_ALREADY_USED,
    ERROR_HUMIDIFIER_ALREADY_USED,
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
    PRESET_TYPE_MONITORING,
)
from homeassistant.components.humidifier import HumidifierDeviceClass
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar


@pytest.fixture
def stub_spawns() -> Generator[None, None, None]:
    """Stub both spawn helpers — the real spawn is covered in test_init."""
    with (
        patch(
            "custom_components.grosfarm._spawn_generic_thermostat_child",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.grosfarm._spawn_generic_hygrostat_child",
            new_callable=AsyncMock,
        ),
    ):
        yield


@pytest.fixture
def area_id(hass: HomeAssistant) -> str:
    """Create a single test area and return its id."""
    return ar.async_get(hass).async_create("Greenhouse 1").id


def _heating_input(area_id: str, heater: str | None = "switch.heater") -> dict:
    payload = {
        CONF_NAME: "Temperature — Greenhouse 1",
        CONF_AREA_ID: area_id,
        CONF_TARGET_SENSOR: "sensor.greenhouse_temp",
    }
    if heater is not None:
        payload[CONF_HEATER] = heater
    return payload


def _humid_input(area_id: str, humidifier: str | None = "switch.humidifier") -> dict:
    payload = {
        CONF_NAME: "Humidity — Greenhouse 1",
        CONF_AREA_ID: area_id,
        CONF_HUMIDITY_SENSOR: "sensor.greenhouse_humidity",
        CONF_HUMIDIFIER_DEVICE_CLASS: HumidifierDeviceClass.HUMIDIFIER,
    }
    if humidifier is not None:
        payload[CONF_HUMIDIFIER] = humidifier
    return payload


def _monitoring_input(area_id: str) -> dict:
    return {
        CONF_NAME: "CO2 — Greenhouse 1",
        CONF_AREA_ID: area_id,
        CONF_SENSOR: "sensor.greenhouse_co2",
    }


async def _pick_menu(hass: HomeAssistant, choice: str) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": choice}
    )


async def _finish(hass: HomeAssistant, choice: str, payload: dict) -> dict:
    result = await _pick_menu(hass, choice)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == choice
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=payload
    )


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------


async def test_user_step_shows_menu(hass: HomeAssistant, stub_spawns) -> None:
    """First call renders a menu of all preset types (incl. cloud link)."""
    from custom_components.grosfarm.const import (
        PRESET_TYPE_CLOUD,
        PRESET_TYPE_LIGHTING,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        PRESET_TYPE_HEATING,
        PRESET_TYPE_HUMIDIFYING,
        PRESET_TYPE_LIGHTING,
        PRESET_TYPE_MONITORING,
        PRESET_TYPE_CLOUD,
    }


# ---------------------------------------------------------------------------
# Temperature branch
# ---------------------------------------------------------------------------


async def test_temperature_with_heater(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Temperature + heater → control zone with seeded home preset."""
    result = await _finish(hass, PRESET_TYPE_HEATING, _heating_input(area_id))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_PRESET_TYPE] == PRESET_TYPE_HEATING
    assert data[CONF_AREA_ID] == area_id
    assert data[CONF_HEATER] == "switch.heater"
    assert data[CONF_PRESET_TEMPS] == DEFAULT_PRESET_TEMPS


async def test_temperature_sensor_only(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Temperature without heater → telemetry zone, no preset_temps seeded."""
    result = await _finish(
        hass, PRESET_TYPE_HEATING, _heating_input(area_id, heater=None)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_PRESET_TYPE] == PRESET_TYPE_HEATING
    assert data[CONF_AREA_ID] == area_id
    assert CONF_HEATER not in data
    assert CONF_PRESET_TEMPS not in data


async def test_temperature_collision_on_heater(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Same heater used by different temperature zone → form error."""
    await _finish(hass, PRESET_TYPE_HEATING, _heating_input(area_id))
    result = await _finish(
        hass,
        PRESET_TYPE_HEATING,
        {
            **_heating_input(area_id),
            CONF_TARGET_SENSOR: "sensor.other_temp",
            CONF_NAME: "Temperature — Greenhouse 2",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HEATER: ERROR_HEATER_ALREADY_USED}


async def test_temperature_duplicate_sensor_only_is_idempotent(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Two sensor-only zones for the SAME temperature sensor abort the second."""
    await _finish(hass, PRESET_TYPE_HEATING, _heating_input(area_id, heater=None))
    result = await _finish(
        hass, PRESET_TYPE_HEATING, _heating_input(area_id, heater=None)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Humidity branch
# ---------------------------------------------------------------------------


async def test_humidity_with_humidifier(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Humidity + humidifier → control zone with seeded target."""
    result = await _finish(hass, PRESET_TYPE_HUMIDIFYING, _humid_input(area_id))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_PRESET_TYPE] == PRESET_TYPE_HUMIDIFYING
    assert data[CONF_AREA_ID] == area_id
    assert data[CONF_HUMIDIFIER] == "switch.humidifier"
    assert data[CONF_TARGET_HUMIDITY] == DEFAULT_TARGET_HUMIDITY


async def test_humidity_sensor_only(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Humidity without humidifier → telemetry zone, no target_humidity seeded."""
    result = await _finish(
        hass, PRESET_TYPE_HUMIDIFYING, _humid_input(area_id, humidifier=None)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_HUMIDIFIER not in data
    assert CONF_TARGET_HUMIDITY not in data


async def test_humidity_collision_on_humidifier(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Same humidifier used by another humidity zone → form error."""
    await _finish(hass, PRESET_TYPE_HUMIDIFYING, _humid_input(area_id))
    result = await _finish(
        hass,
        PRESET_TYPE_HUMIDIFYING,
        {
            **_humid_input(area_id),
            CONF_HUMIDITY_SENSOR: "sensor.other_humidity",
            CONF_NAME: "Humidity 2",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HUMIDIFIER: ERROR_HUMIDIFIER_ALREADY_USED}


# ---------------------------------------------------------------------------
# Other-indicator branch
# ---------------------------------------------------------------------------


async def test_monitoring_happy(hass: HomeAssistant, stub_spawns, area_id: str) -> None:
    """Other indicator — sensor only, area required."""
    result = await _finish(hass, PRESET_TYPE_MONITORING, _monitoring_input(area_id))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_PRESET_TYPE] == PRESET_TYPE_MONITORING
    assert data[CONF_AREA_ID] == area_id
    assert data[CONF_SENSOR] == "sensor.greenhouse_co2"
    assert CONF_CHILD_ENTRY_ID not in data


async def test_monitoring_unique_id_does_not_collide_with_heating(
    hass: HomeAssistant, stub_spawns, area_id: str
) -> None:
    """Heating and monitoring on the same sensor are independent indicators."""
    await _finish(
        hass,
        PRESET_TYPE_HEATING,
        {
            CONF_NAME: "Temperature",
            CONF_AREA_ID: area_id,
            CONF_TARGET_SENSOR: "sensor.shared",
            CONF_HEATER: "switch.dedicated",
        },
    )
    result = await _finish(
        hass,
        PRESET_TYPE_MONITORING,
        {
            CONF_NAME: "Temperature telemetry",
            CONF_AREA_ID: area_id,
            CONF_SENSOR: "sensor.shared",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
