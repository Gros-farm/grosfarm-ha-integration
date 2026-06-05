"""Options-flow tests — dispatch on preset_type."""

from __future__ import annotations

from custom_components.grosfarm.const import (
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
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
    PRESET_TYPE_MONITORING,
)
from homeassistant.components.humidifier import HumidifierDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _seed_heating(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.test_temp",
        "20.0",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    hass.states.async_set("switch.test_heater", "off")


def _seed_humid(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.test_humidity",
        "55.0",
        {"unit_of_measurement": "%", "device_class": "humidity"},
    )
    hass.states.async_set("switch.test_humidifier", "off")


async def test_options_heating_partial_merge(hass: HomeAssistant) -> None:
    """Heating options form: partial submit merges with seeded presets."""
    _seed_heating(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="H",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_HEATING,
            CONF_NAME: "H",
            CONF_TARGET_SENSOR: "sensor.test_temp",
            CONF_HEATER: "switch.test_heater",
            CONF_PRESET_TEMPS: dict(DEFAULT_PRESET_TEMPS),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    child_id = entry.data[CONF_CHILD_ENTRY_ID]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "presets"

    # Cloud (simulated) sets the night setpoint.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"sleep_temp": 18.0}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Parent kept the seed AND the new value.
    assert (
        entry.data[CONF_PRESET_TEMPS]["home_temp"] == DEFAULT_PRESET_TEMPS["home_temp"]
    )
    assert entry.data[CONF_PRESET_TEMPS]["sleep_temp"] == 18.0
    # Child got the new option too.
    child = hass.config_entries.async_get_entry(child_id)
    assert child is not None
    assert child.options["sleep_temp"] == 18.0
    assert child.options["home_temp"] == DEFAULT_PRESET_TEMPS["home_temp"]


async def test_options_humidifying_updates_target(hass: HomeAssistant) -> None:
    """Humidifying options form: single target_humidity propagates to child."""
    _seed_humid(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="U",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_HUMIDIFYING,
            CONF_NAME: "U",
            CONF_HUMIDITY_SENSOR: "sensor.test_humidity",
            CONF_HUMIDIFIER: "switch.test_humidifier",
            CONF_HUMIDIFIER_DEVICE_CLASS: HumidifierDeviceClass.HUMIDIFIER,
            CONF_TARGET_HUMIDITY: DEFAULT_TARGET_HUMIDITY,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    child_id = entry.data[CONF_CHILD_ENTRY_ID]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "humidity"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_TARGET_HUMIDITY: 72.0}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entry.data[CONF_TARGET_HUMIDITY] == 72.0
    child = hass.config_entries.async_get_entry(child_id)
    assert child is not None
    assert child.options[CONF_TARGET_HUMIDITY] == 72.0


async def test_options_sensor_only_no_op(hass: HomeAssistant) -> None:
    """Any sensor-only zone (other-indicator OR heating-without-heater) gets
    the same empty options form — there's nothing to configure locally.
    """
    hass.states.async_set("sensor.test_co2", "420", {"device_class": "carbon_dioxide"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="M",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_MONITORING,
            CONF_NAME: "M",
            CONF_SENSOR: "sensor.test_co2",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensor_only"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
