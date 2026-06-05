"""Setup/unload + spawned-child cascade tests for Grosfarm."""

from __future__ import annotations

from typing import Any

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
    SAFE_COLD_TOLERANCE,
    SAFE_DRY_TOLERANCE,
    SAFE_HOT_TOLERANCE,
    SAFE_MIN_CYCLE_SECONDS,
    SAFE_WET_TOLERANCE,
)
from homeassistant.components.generic_hygrostat import DOMAIN as GENERIC_HYGROSTAT
from homeassistant.components.generic_thermostat.const import (
    DOMAIN as GENERIC_THERMOSTAT,
)
from homeassistant.components.humidifier import HumidifierDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _heating_data(*, with_heater: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_PRESET_TYPE: PRESET_TYPE_HEATING,
        CONF_NAME: "Heating Zone",
        CONF_TARGET_SENSOR: "sensor.test_temp",
    }
    if with_heater:
        data[CONF_HEATER] = "switch.test_heater"
        data[CONF_PRESET_TEMPS] = dict(DEFAULT_PRESET_TEMPS)
    return data


def _humidifying_data(*, with_humidifier: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_PRESET_TYPE: PRESET_TYPE_HUMIDIFYING,
        CONF_NAME: "Humid Zone",
        CONF_HUMIDITY_SENSOR: "sensor.test_humidity",
    }
    if with_humidifier:
        data[CONF_HUMIDIFIER] = "switch.test_humidifier"
        data[CONF_HUMIDIFIER_DEVICE_CLASS] = HumidifierDeviceClass.HUMIDIFIER
        data[CONF_TARGET_HUMIDITY] = DEFAULT_TARGET_HUMIDITY
    return data


def _monitoring_data() -> dict[str, Any]:
    return {
        CONF_PRESET_TYPE: PRESET_TYPE_MONITORING,
        CONF_NAME: "CO2 Monitor",
        CONF_SENSOR: "sensor.test_co2",
    }


def _seed_heating_hardware(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.test_temp",
        "20.0",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    hass.states.async_set("switch.test_heater", "off")


def _seed_humidifying_hardware(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.test_humidity",
        "55.0",
        {"unit_of_measurement": "%", "device_class": "humidity"},
    )
    hass.states.async_set("switch.test_humidifier", "off")


# ---------------------------------------------------------------------------
# Heating — full spawn + cascade
# ---------------------------------------------------------------------------


async def test_heating_spawns_thermostat_child(hass: HomeAssistant) -> None:
    """Parent setup spawns a generic_thermostat helper with locked safety values."""
    _seed_heating_hardware(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=_heating_data(), title="Heating Zone")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    child_id = entry.data.get(CONF_CHILD_ENTRY_ID)
    assert child_id is not None
    child = hass.config_entries.async_get_entry(child_id)
    assert child is not None
    assert child.domain == GENERIC_THERMOSTAT

    opts = child.options
    assert opts["cold_tolerance"] == SAFE_COLD_TOLERANCE
    assert opts["hot_tolerance"] == SAFE_HOT_TOLERANCE
    md = opts["min_cycle_duration"]
    assert (
        md["hours"] * 3600 + md["minutes"] * 60 + md["seconds"]
        == SAFE_MIN_CYCLE_SECONDS
    )
    assert opts["target_sensor"] == "sensor.test_temp"
    assert opts["heater"] == "switch.test_heater"
    # Seeded preset propagated; unset presets stay unset on the child.
    for key, value in DEFAULT_PRESET_TEMPS.items():
        assert opts[key] == value
    for key in ("away_temp", "eco_temp", "sleep_temp", "comfort_temp", "activity_temp"):
        assert key not in opts


async def test_heating_remove_cascades(hass: HomeAssistant) -> None:
    """Removing the parent removes the spawned thermostat."""
    _seed_heating_hardware(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=_heating_data(), title="Heating Zone")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    child_id = entry.data[CONF_CHILD_ENTRY_ID]
    assert hass.config_entries.async_get_entry(child_id) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(child_id) is None


async def test_heating_unload_keeps_child(hass: HomeAssistant) -> None:
    """Reload/unload (not remove) must leave the child entry intact."""
    _seed_heating_hardware(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=_heating_data(), title="Heating Zone")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    child_id = entry.data[CONF_CHILD_ENTRY_ID]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.config_entries.async_get_entry(child_id) is not None


# ---------------------------------------------------------------------------
# Humidifying — full spawn
# ---------------------------------------------------------------------------


async def test_humidifying_spawns_hygrostat_child(hass: HomeAssistant) -> None:
    """Parent setup spawns a generic_hygrostat helper with locked safety values."""
    _seed_humidifying_hardware(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=_humidifying_data(), title="Humid Zone")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    child_id = entry.data.get(CONF_CHILD_ENTRY_ID)
    assert child_id is not None
    child = hass.config_entries.async_get_entry(child_id)
    assert child is not None
    assert child.domain == GENERIC_HYGROSTAT

    opts = child.options
    assert opts["dry_tolerance"] == SAFE_DRY_TOLERANCE
    assert opts["wet_tolerance"] == SAFE_WET_TOLERANCE
    md = opts["min_cycle_duration"]
    assert (
        md["hours"] * 3600 + md["minutes"] * 60 + md["seconds"]
        == SAFE_MIN_CYCLE_SECONDS
    )
    assert opts["target_sensor"] == "sensor.test_humidity"
    assert opts["humidifier"] == "switch.test_humidifier"
    assert opts["device_class"] == HumidifierDeviceClass.HUMIDIFIER
    # Seeded target humidity was poked onto the child.
    assert opts[CONF_TARGET_HUMIDITY] == DEFAULT_TARGET_HUMIDITY


async def test_humidifying_remove_cascades(hass: HomeAssistant) -> None:
    """Removing a humidifying parent removes the spawned hygrostat."""
    _seed_humidifying_hardware(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=_humidifying_data(), title="Humid Zone")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    child_id = entry.data[CONF_CHILD_ENTRY_ID]

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(child_id) is None


# ---------------------------------------------------------------------------
# Monitoring — no child
# ---------------------------------------------------------------------------


async def test_monitoring_no_child_helper(hass: HomeAssistant) -> None:
    """Monitoring entries register the sensor but spawn no helper."""
    hass.states.async_set("sensor.test_co2", "420", {"device_class": "carbon_dioxide"})

    entry = MockConfigEntry(domain=DOMAIN, data=_monitoring_data(), title="CO2 Monitor")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert CONF_CHILD_ENTRY_ID not in entry.data


async def test_monitoring_creates_diagnostic_sensor(hass: HomeAssistant) -> None:
    """Monitoring-зона видна в HA: device + прокси-сенсор зеркалит значение."""
    hass.states.async_set(
        "sensor.test_co2",
        "420",
        {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm"},
    )
    entry = MockConfigEntry(domain=DOMAIN, data=_monitoring_data(), title="CO2 Monitor")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Device зоны появился (раньше monitoring не создавал device вовсе).
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.model == "Sensor-only zone"

    # Прокси-сенсор существует, зеркалит значение и описывает конфигурацию.
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_monitored"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 420.0
    assert state.attributes["source_entity_id"] == "sensor.test_co2"
    assert state.attributes["cloud_indicator"] == "co2_concentration"
    assert state.attributes["mode"] == "monitoring (sensor-only)"
    assert state.attributes.get("unit_of_measurement") == "ppm"

    # Зеркало обновляется при изменении исходного датчика.
    hass.states.async_set(
        "sensor.test_co2",
        "555",
        {"device_class": "carbon_dioxide", "unit_of_measurement": "ppm"},
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id).state) == 555.0


async def test_heating_sensor_only_creates_diagnostic_sensor(
    hass: HomeAssistant,
) -> None:
    """Heating без нагревателя (sensor-only) тоже видна: device + прокси-сенсор."""
    _seed_heating_hardware(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data=_heating_data(with_heater=False), title="Температура офис"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert CONF_CHILD_ENTRY_ID not in entry.data  # actuator'а нет — child не спавнится

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.model == "Sensor-only zone"

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_monitored"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 20.0
    assert state.attributes["source_entity_id"] == "sensor.test_temp"
    assert state.attributes["cloud_indicator"] == "air_temperature"
    assert state.attributes["mode"] == "heating (sensor-only)"


async def test_heating_with_heater_has_no_proxy_sensor(hass: HomeAssistant) -> None:
    """Heating С нагревателем: видна через climate от child, прокси-сенсор не нужен."""
    _seed_heating_hardware(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=_heating_data(), title="Heating Zone")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data.get(CONF_CHILD_ENTRY_ID) is not None
    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_monitored"
        )
        is None
    )


# ---------------------------------------------------------------------------
# Lighting — sensor + optional switch, no local helper
# ---------------------------------------------------------------------------


async def test_lighting_no_child_helper(hass: HomeAssistant) -> None:
    """Lighting zone — sensor + optional switch, helper не нужен."""
    from custom_components.grosfarm.const import (
        CONF_ILLUMINANCE_SENSOR,
        CONF_LIGHT,
        PRESET_TYPE_LIGHTING,
    )

    hass.states.async_set("sensor.test_lux", "12500", {"device_class": "illuminance"})
    hass.states.async_set("switch.test_lamp", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Greenhouse light",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_LIGHTING,
            CONF_NAME: "Greenhouse light",
            CONF_ILLUMINANCE_SENSOR: "sensor.test_lux",
            CONF_LIGHT: "switch.test_lamp",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert CONF_CHILD_ENTRY_ID not in entry.data


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


async def test_temperature_sensor_only_no_child(hass: HomeAssistant) -> None:
    """Temperature indicator without a heater spawns no child helper."""
    _seed_heating_hardware(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data=_heating_data(with_heater=False), title="Temp-only"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert CONF_CHILD_ENTRY_ID not in entry.data


async def test_humidity_sensor_only_no_child(hass: HomeAssistant) -> None:
    """Humidity indicator without a humidifier spawns no child helper."""
    _seed_humidifying_hardware(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_humidifying_data(with_humidifier=False),
        title="Humid-only",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert CONF_CHILD_ENTRY_ID not in entry.data


async def test_pre_preset_type_entry_treated_as_heating(hass: HomeAssistant) -> None:
    """An entry created before preset_type was introduced still works (heating)."""
    _seed_heating_hardware(hass)
    legacy = {
        CONF_NAME: "Legacy",
        CONF_TARGET_SENSOR: "sensor.test_temp",
        CONF_HEATER: "switch.test_heater",
        CONF_PRESET_TEMPS: dict(DEFAULT_PRESET_TEMPS),
    }  # no CONF_PRESET_TYPE key
    entry = MockConfigEntry(domain=DOMAIN, data=legacy, title="Legacy")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data.get(CONF_CHILD_ENTRY_ID) is not None
