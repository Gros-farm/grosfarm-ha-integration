"""End-to-end-ish tests for cloud entry + GrosfarmCoordinator.

API клиент и WS-канал замоканы — это не тест сетевой интеграции, а проверка того,
что cloud-entry правильно цепляется к существующим zone-entries: regiстрирует их
в облаке, маппит входящие уставки в `entry.data` родителя и в `entry.options`
ребёнка-helper'а, и шлёт телеметрию в правильном формате.

Реальный сетевой тест прогоняется отдельным скриптом `scripts/smoke_e2e.py`
против работающего `mock-cloud/`.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.grosfarm import _CLOUD_KEY
from custom_components.grosfarm.api import RegistrationResult
from custom_components.grosfarm.const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CHILD_ENTRY_ID,
    CONF_HEATER,
    CONF_HUMIDIFIER,
    CONF_HUMIDIFIER_DEVICE_CLASS,
    CONF_HUMIDITY_SENSOR,
    CONF_LOGIN,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PRESET_TEMPS,
    CONF_PRESET_TYPE,
    CONF_TARGET_HUMIDITY,
    CONF_TARGET_SENSOR,
    DOMAIN,
    PRESET_TYPE_CLOUD,
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
)
from homeassistant.components.humidifier import HumidifierDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_cloud_client() -> Generator[MagicMock, None, None]:
    """Замоканный GrosfarmCloudClient — отдаёт детерминированные section_id."""

    instance = MagicMock(name="GrosfarmCloudClient")
    instance.authenticate = AsyncMock(return_value="fake-token")
    instance.access_token = "fake-token"

    next_section_id = {"counter": 0}

    async def register(**kwargs: Any) -> RegistrationResult:
        sections = kwargs["sections"]
        ids = {}
        for s in sections:
            next_section_id["counter"] += 1
            ids[s["external_id"]] = f"section-{next_section_id['counter']:03d}"
        return RegistrationResult(
            control_unit_id="cu-001",
            section_ids=ids,
            stream_url="ws://fake/stream",
        )

    instance.register_control_unit = AsyncMock(side_effect=register)
    instance.get_setpoints = AsyncMock(
        return_value={"version": 0, "issued_at": None, "sections": []}
    )
    instance.post_measurements = AsyncMock(return_value={"accepted": 0, "rejected": []})

    # Подменяем async_get_clientsession чтобы не создавать реальную aiohttp-сессию
    # (она поднимает фоновый thread, ломающий leak-check pytest-HA).
    with (
        patch(
            "custom_components.grosfarm.coordinator.GrosfarmCloudClient",
            return_value=instance,
        ),
        patch(
            "custom_components.grosfarm.coordinator.async_get_clientsession",
            return_value=MagicMock(name="aiohttp.ClientSession"),
        ),
    ):
        yield instance


@pytest.fixture
def fake_stream() -> Generator[dict[str, Any], None, None]:
    """Подменить GrosfarmStream — захватить колбэки для дальнейшей дёрки."""

    captured: dict[str, Any] = {}

    class _Stream:
        def __init__(self, **kwargs: Any) -> None:
            captured["on_setpoints"] = kwargs["on_setpoints"]
            captured["on_command"] = kwargs["on_command"]
            captured["on_connection_change"] = kwargs.get("on_connection_change")
            captured["telemetry"] = []
            captured["stream"] = self
            # Фейк представляет работающий канал: после start() линк «жив».
            self.is_connected = False

        def start(self) -> None:
            self.is_connected = True
            cb = captured.get("on_connection_change")
            if cb is not None:
                cb(True)

        async def stop(self) -> None:
            self.is_connected = False

        def update_known_version(self, version: int) -> None:
            pass

        async def send_telemetry(self, measurements: list[dict[str, Any]]) -> str:
            captured["telemetry"].append(measurements)
            return "msg-1"

    with patch("custom_components.grosfarm.coordinator.GrosfarmStream", _Stream):
        yield captured


@pytest.fixture
def stub_spawns() -> Generator[None, None, None]:
    """Stub helper spawn — child entries не создаются в этих тестах."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_heating_entry(hass: HomeAssistant, **overrides: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Greenhouse heat",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_HEATING,
            CONF_NAME: "Greenhouse heat",
            CONF_TARGET_SENSOR: "sensor.fake_greenhouse_temp",
            CONF_HEATER: "switch.fake_greenhouse_heater",
            CONF_PRESET_TEMPS: {"home_temp": 22.0},
            **overrides,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _make_humid_entry(hass: HomeAssistant, **overrides: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Greenhouse humid",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_HUMIDIFYING,
            CONF_NAME: "Greenhouse humid",
            CONF_HUMIDITY_SENSOR: "sensor.fake_greenhouse_humid",
            CONF_HUMIDIFIER: "switch.fake_humidifier",
            CONF_HUMIDIFIER_DEVICE_CLASS: HumidifierDeviceClass.HUMIDIFIER,
            CONF_TARGET_HUMIDITY: 60.0,
            **overrides,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _make_cloud_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gros.farm cloud (ha-edge-01)",
        unique_id="cloud::http://mock/::ha-edge-01",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_CLOUD,
            CONF_BASE_URL: "http://mock",
            CONF_LOGIN: "ha-edge-01",
            CONF_PASSWORD: "secret",
            CONF_API_KEY: "demo-key",
        },
    )
    entry.add_to_hass(hass)
    return entry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def _setup_both(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    """Load каждой entry в порядке передачи, дождаться фоновых задач."""
    for entry in entries:
        if entry.state is ConfigEntryState.NOT_LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _stop_all_cloud_coordinators(hass: HomeAssistant) -> None:
    """Корректно выгрузить cloud-entries — без этого фоновые задачи рабочего coordinator
    переживают тест и leak-checker pytest-HA фейлит teardown.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.data.get(CONF_PRESET_TYPE) == PRESET_TYPE_CLOUD
            and entry.state is ConfigEntryState.LOADED
        ):
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cloud_entry_registers_existing_zones(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Cloud-entry поднимается и регистрирует уже существующие zone-entries."""
    heating = _make_heating_entry(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, heating, cloud)

    # Авторизовались и зарегистрировались.
    fake_cloud_client.authenticate.assert_awaited_once()
    fake_cloud_client.register_control_unit.assert_awaited()
    call_kwargs = fake_cloud_client.register_control_unit.await_args.kwargs
    section_external_ids = {s["external_id"] for s in call_kwargs["sections"]}
    assert heating.entry_id in section_external_ids

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    assert heating.entry_id in coordinator._zones_by_entry
    await _stop_all_cloud_coordinators(hass)


async def test_setpoints_push_lands_on_heating_entry(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Push уставок через WS-колбэк меняет entry.data[CONF_PRESET_TEMPS]."""
    heating = _make_heating_entry(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, heating, cloud)

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    section_id = coordinator._zones_by_entry[heating.entry_id].section_id

    # Эмулируем push с облака.
    await fake_stream["on_setpoints"](
        {
            "type": "setpoints.update",
            "version": 5,
            "sections": [
                {
                    "section_id": section_id,
                    "targets": {
                        "air_temperature_day": 24.5,
                        "air_temperature_night": 17.5,
                    },
                }
            ],
        }
    )
    await hass.async_block_till_done()

    updated = hass.config_entries.async_get_entry(heating.entry_id)
    assert updated is not None
    assert updated.data[CONF_PRESET_TEMPS]["home_temp"] == 24.5
    assert updated.data[CONF_PRESET_TEMPS]["sleep_temp"] == 17.5
    await _stop_all_cloud_coordinators(hass)


async def test_setpoints_push_lands_on_humidifying_entry(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Push влажности через WS-колбэк меняет entry.data[CONF_TARGET_HUMIDITY]."""
    humid = _make_humid_entry(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, humid, cloud)

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    section_id = coordinator._zones_by_entry[humid.entry_id].section_id

    await fake_stream["on_setpoints"](
        {
            "type": "setpoints.update",
            "version": 3,
            "sections": [
                {
                    "section_id": section_id,
                    "targets": {"air_humidity_day": 72.0},
                }
            ],
        }
    )
    await hass.async_block_till_done()

    updated = hass.config_entries.async_get_entry(humid.entry_id)
    assert updated is not None
    assert updated.data[CONF_TARGET_HUMIDITY] == 72.0
    await _stop_all_cloud_coordinators(hass)


async def test_telemetry_collected_with_correct_indicator(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Телеметрия снимается с сенсора zone-entry и едет в WS с правильным indicator."""
    heating = _make_heating_entry(hass)
    cloud = _make_cloud_entry(hass)

    # Заводим state у сенсора — coordinator должен его подобрать.
    hass.states.async_set(
        "sensor.fake_greenhouse_temp", "21.7", {"device_class": "temperature"}
    )
    await _setup_both(hass, heating, cloud)

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    await coordinator._send_one_batch()
    await hass.async_block_till_done()

    sent = fake_stream["telemetry"]
    assert sent, "ничего не отправлено"
    measurements = sent[-1]
    assert len(measurements) == 1
    m = measurements[0]
    assert m["indicator"] == "air_temperature"
    assert m["value"] == pytest.approx(21.7)
    assert m["source_type"] == "sensor"
    await _stop_all_cloud_coordinators(hass)


async def test_unload_cloud_entry_stops_coordinator(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Удаление cloud-entry останавливает coordinator и убирает его из hass.data."""
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, cloud)
    assert cloud.entry_id in hass.data[DOMAIN][_CLOUD_KEY]

    assert await hass.config_entries.async_unload(cloud.entry_id)
    await hass.async_block_till_done()
    assert cloud.entry_id not in hass.data[DOMAIN].get(_CLOUD_KEY, {})


async def test_propagate_to_child_when_present(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Push уставок попадает в child.options если parent держит CONF_CHILD_ENTRY_ID."""
    # Фейковый "child" entry. Помечаем как LOADED, чтобы pytest-HA
    # не пытался реально загружать generic_thermostat (там KeyError на пустой data).
    child = MockConfigEntry(
        domain="generic_thermostat",
        title="child",
        data={},
        options={"home_temp": 20.0},
        state=ConfigEntryState.LOADED,
    )
    child.add_to_hass(hass)
    heating = _make_heating_entry(hass, **{CONF_CHILD_ENTRY_ID: child.entry_id})
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, heating, cloud)

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    section_id = coordinator._zones_by_entry[heating.entry_id].section_id

    await fake_stream["on_setpoints"](
        {
            "type": "setpoints.update",
            "version": 7,
            "sections": [
                {
                    "section_id": section_id,
                    "targets": {"air_temperature_day": 23.0},
                }
            ],
        }
    )
    await hass.async_block_till_done()

    refreshed = hass.config_entries.async_get_entry(child.entry_id)
    assert refreshed is not None
    assert refreshed.options.get("home_temp") == 23.0
    await _stop_all_cloud_coordinators(hass)


async def test_lighting_zone_registers_switch_as_device(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Lighting zone с CONF_LIGHT регистрирует лампу как device в облаке."""
    from custom_components.grosfarm.const import (
        CONF_ILLUMINANCE_SENSOR,
        CONF_LIGHT,
        PRESET_TYPE_LIGHTING,
    )

    light = MockConfigEntry(
        domain=DOMAIN,
        title="Greenhouse light",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_LIGHTING,
            CONF_NAME: "Greenhouse light",
            CONF_ILLUMINANCE_SENSOR: "sensor.fake_lux",
            CONF_LIGHT: "switch.fake_lamp",
        },
    )
    light.add_to_hass(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, light, cloud)

    kwargs = fake_cloud_client.register_control_unit.await_args.kwargs
    devices_external_ids = {d["external_id"] for d in kwargs["devices"]}
    assert "switch.fake_lamp" in devices_external_ids
    await _stop_all_cloud_coordinators(hass)


def _register_switch_capture(hass: HomeAssistant) -> list[tuple[str, dict]]:
    """Зарегистрировать пустые turn_on/turn_off на switch-домене, ловить вызовы."""
    calls: list[tuple[str, dict]] = []

    async def _handler(call: Any) -> None:
        calls.append((call.service, dict(call.data)))

    hass.services.async_register("switch", "turn_on", _handler)
    hass.services.async_register("switch", "turn_off", _handler)
    return calls


async def test_command_set_device_state_calls_switch_service(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """command.set_device_state дёргает switch.turn_on/turn_off."""
    from custom_components.grosfarm.const import (
        CONF_ILLUMINANCE_SENSOR,
        CONF_LIGHT,
        PRESET_TYPE_LIGHTING,
    )

    light = MockConfigEntry(
        domain=DOMAIN,
        title="Light",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_LIGHTING,
            CONF_NAME: "Light",
            CONF_ILLUMINANCE_SENSOR: "sensor.fake_lux",
            CONF_LIGHT: "switch.fake_lamp",
        },
    )
    light.add_to_hass(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, light, cloud)

    calls = _register_switch_capture(hass)

    await fake_stream["on_command"](
        {
            "type": "command.set_device_state",
            "command_id": "c1",
            "payload": {"device_external_id": "switch.fake_lamp", "state": "on"},
        }
    )
    await fake_stream["on_command"](
        {
            "type": "command.set_device_state",
            "command_id": "c2",
            "payload": {"device_external_id": "switch.fake_lamp", "state": "off"},
        }
    )
    await hass.async_block_till_done()

    services = [c[0] for c in calls]
    assert "turn_on" in services
    assert "turn_off" in services
    targets = [c[1].get("entity_id") for c in calls]
    assert all(t == "switch.fake_lamp" for t in targets)
    await _stop_all_cloud_coordinators(hass)


async def test_command_set_device_state_refuses_non_switch_domain(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Облако не может дёрнуть произвольный домен (только switch разрешён)."""
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, cloud)
    calls = _register_switch_capture(hass)

    await fake_stream["on_command"](
        {
            "type": "command.set_device_state",
            "command_id": "c3",
            "payload": {"device_external_id": "light.living_room", "state": "on"},
        }
    )
    await hass.async_block_till_done()

    assert calls == []
    await _stop_all_cloud_coordinators(hass)


async def test_connection_sensor_reflects_live_link(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Сенсор связи следует за live WS-линком, а не за bootstrap-флагом.

    Регрессия: `is_connected` залипал на True после первой регистрации, и
    CloudConnectionSensor показывал «connected» даже когда мок выключен. Теперь
    сенсор смотрит на `is_cloud_live` (открыт ли сокет сейчас), а падение линка
    дёргает on_connection_change → сенсор перерисовывается.
    """
    from custom_components.grosfarm.sensor import CloudConnectionSensor

    heating = _make_heating_entry(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, heating, cloud)

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    sensor = CloudConnectionSensor(coordinator, cloud)

    # Линк поднят (fake stream.start выставил is_connected=True) → connected.
    assert coordinator.is_cloud_live is True
    assert sensor.native_value == "connected"

    # Мок выключили: WS отвалился. Bootstrap-флаг не сбрасывается, но live-линка
    # больше нет → индикатор обязан показать autonomous, а не залипнуть.
    stream = fake_stream["stream"]
    stream.is_connected = False
    on_change = fake_stream["on_connection_change"]
    assert on_change is not None
    on_change(False)

    assert coordinator.is_connected is True
    assert coordinator.is_cloud_live is False
    assert sensor.native_value == "autonomous"

    await _stop_all_cloud_coordinators(hass)


async def test_cloud_unreachable_starts_autonomous(
    hass: HomeAssistant,
    stub_spawns,
) -> None:
    """Облако недоступно → cloud-entry грузится в автономном режиме, а не падает.

    Это штатный режим (мок живёт на ноуте оператора). Запись должна быть LOADED,
    is_connected — False, сенсор связи — «autonomous». Подключение догоняется в
    фоне (_connect_retry_loop), регистрация при недоступном облаке не случается.
    """
    from custom_components.grosfarm.api import GrosfarmAPIError
    from custom_components.grosfarm.sensor import CloudConnectionSensor

    failing = MagicMock(name="GrosfarmCloudClient")
    failing.authenticate = AsyncMock(side_effect=GrosfarmAPIError("cloud unreachable"))
    failing.access_token = None

    with (
        patch(
            "custom_components.grosfarm.coordinator.GrosfarmCloudClient",
            return_value=failing,
        ),
        patch(
            "custom_components.grosfarm.coordinator.async_get_clientsession",
            return_value=MagicMock(name="aiohttp.ClientSession"),
        ),
    ):
        cloud = _make_cloud_entry(hass)
        assert await hass.config_entries.async_setup(cloud.entry_id)
        await hass.async_block_till_done()

        # Запись загружена (НЕ SETUP_ERROR / SETUP_RETRY), но связи нет.
        assert cloud.state is ConfigEntryState.LOADED
        coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
        assert coordinator.is_connected is False
        # При недоступном облаке регистрация не вызывается.
        failing.register_control_unit.assert_not_called()
        # Сенсор связи показывает автономный режим.
        sensor = CloudConnectionSensor(coordinator, cloud)
        assert sensor.native_value == "autonomous"

        await _stop_all_cloud_coordinators(hass)


async def test_cloud_auth_rejected_is_setup_error(
    hass: HomeAssistant,
    stub_spawns,
) -> None:
    """Неверные креды (облако доступно, но отвергло) → реальная ошибка setup.

    В отличие от недоступного облака, это проблема конфигурации, которую должен
    решить пользователь — запись уходит в SETUP_ERROR (+ reauth), а не в офлайн.
    """
    from custom_components.grosfarm.api import GrosfarmAuthError

    rejecting = MagicMock(name="GrosfarmCloudClient")
    rejecting.authenticate = AsyncMock(
        side_effect=GrosfarmAuthError("неверные креды или api_key")
    )
    rejecting.access_token = None

    with (
        patch(
            "custom_components.grosfarm.coordinator.GrosfarmCloudClient",
            return_value=rejecting,
        ),
        patch(
            "custom_components.grosfarm.coordinator.async_get_clientsession",
            return_value=MagicMock(name="aiohttp.ClientSession"),
        ),
    ):
        cloud = _make_cloud_entry(hass)
        assert not await hass.config_entries.async_setup(cloud.entry_id)
        await hass.async_block_till_done()

        assert cloud.state is ConfigEntryState.SETUP_ERROR


async def test_reregister_refetches_setpoints(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """После перерегистрации coordinator подтягивает актуальные уставки.

    Регрессия: свежедобавленная зона раньше висела на дефолтах, пока облако само
    не запушит апдейт. _fetch_and_apply_setpoints закрывает дыру — проверяем, что
    снятый HTTP-snapshot применяется к зоне.
    """
    heating = _make_heating_entry(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, heating, cloud)

    coordinator = hass.data[DOMAIN][_CLOUD_KEY][cloud.entry_id]
    section_id = coordinator._zones_by_entry[heating.entry_id].section_id

    fake_cloud_client.get_setpoints = AsyncMock(
        return_value={
            "version": 9,
            "sections": [
                {
                    "section_id": section_id,
                    "targets": {"air_temperature_day": 25.5},
                }
            ],
        }
    )
    await coordinator._fetch_and_apply_setpoints()
    await hass.async_block_till_done()

    updated = hass.config_entries.async_get_entry(heating.entry_id)
    assert updated is not None
    assert updated.data[CONF_PRESET_TEMPS]["home_temp"] == 25.5
    await _stop_all_cloud_coordinators(hass)


async def test_unknown_device_class_sensor_not_registered(
    hass: HomeAssistant,
    stub_spawns,
    fake_cloud_client: MagicMock,
    fake_stream: dict[str, Any],
) -> None:
    """Monitoring-зона с нераспознанным device_class не регистрируется как сенсор.

    Регрессия: раньше индикатор подменялся на "air_temperature", что врало про
    суть замера. Теперь сенсор не попадает в sensors[], но секция — заводится.
    """
    from custom_components.grosfarm.const import CONF_SENSOR, PRESET_TYPE_MONITORING

    hass.states.async_set("sensor.fake_ph", "6.2", {"device_class": "ph"})
    mon = MockConfigEntry(
        domain=DOMAIN,
        title="pH probe",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_MONITORING,
            CONF_NAME: "pH probe",
            CONF_SENSOR: "sensor.fake_ph",
        },
    )
    mon.add_to_hass(hass)
    cloud = _make_cloud_entry(hass)
    await _setup_both(hass, mon, cloud)

    kwargs = fake_cloud_client.register_control_unit.await_args.kwargs
    sensor_ids = {s["external_id"] for s in kwargs["sensors"]}
    assert "sensor.fake_ph" not in sensor_ids
    section_ids = {s["external_id"] for s in kwargs["sections"]}
    assert mon.entry_id in section_ids
    await _stop_all_cloud_coordinators(hass)
