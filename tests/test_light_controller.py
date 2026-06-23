"""Тесты GrosfarmLightController.

Контроллер дёргается вручную через `_tick_now(now)` с заморозкой времени —
быстро и без 60-секундных таймеров pytest-HA.

`sun.sun` мокается на каждый день state-ами с UTC timestamp'ами.
Tz-aware: HA в тестах по умолчанию работает в UTC, поэтому decision_time
(`local_now.replace(hour=12)`) у нас = 12:00 UTC. В тестах берём ровно
такие времена.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.grosfarm.const import (
    CONF_ILLUMINANCE_SENSOR,
    CONF_LAMP_PPFD,
    CONF_LAMP_TYPE,
    CONF_LIGHT,
    CONF_LIGHTING_MODE,
    CONF_NAME,
    CONF_PRESET_TYPE,
    CONF_SENSOR_KIND,
    CONF_TARGET_DLI,
    DOMAIN,
    LAMP_TYPE_LED,
    LIGHTING_MODE_INDOOR_CONTINUOUS,
    LIGHTING_MODE_INDOOR_SUPPLEMENT,
    LIGHTING_MODE_NATURAL_SUPPLEMENT,
    LIGHTING_MODE_OFF,
    PRESET_TYPE_LIGHTING,
    SENSOR_KIND_DLI,
    SENSOR_KIND_LUX,
    SENSOR_KIND_PPFD,
)
from custom_components.grosfarm.light_controller import (
    CalibrationError,
    GrosfarmLightController,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _seed_sun(hass: HomeAssistant, sunrise: datetime, sunset: datetime) -> None:
    hass.states.async_set(
        "sun.sun",
        "above_horizon",
        {
            "next_rising": sunrise.isoformat(),
            "next_setting": sunset.isoformat(),
        },
    )


@pytest.fixture(autouse=True)
async def _force_utc_timezone(hass: HomeAssistant):
    """pytest-HA дефолтит time_zone=US/Pacific — для предсказуемости фиксим UTC."""
    await hass.config.async_set_time_zone("UTC")


def _capture_switch_calls(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Регистрирует turn_on/turn_off на switch-домене и обновляет state в hass."""
    calls: list[tuple[str, str]] = []

    async def _h(call) -> None:
        eid = call.data["entity_id"]
        calls.append((call.service, eid))
        # Меняем state, чтобы controller на следующем тике видел реальную ситуацию.
        hass.states.async_set(eid, "on" if call.service == "turn_on" else "off")

    hass.services.async_register("switch", "turn_on", _h)
    hass.services.async_register("switch", "turn_off", _h)
    return calls


def _make_lighting_entry(
    hass: HomeAssistant,
    *,
    mode: str = LIGHTING_MODE_OFF,
    target_dli: float = 0.0,
    lamp_ppfd: float = 1000.0,
    sensor_kind: str = SENSOR_KIND_PPFD,
    lamp_type: str | None = None,
    sensor_value: str = "0",
    sensor_attrs: dict | None = None,
    light_state: str = "off",
) -> MockConfigEntry:
    hass.states.async_set("sensor.fake_lux", sensor_value, sensor_attrs or {})
    hass.states.async_set("switch.fake_lamp", light_state)
    data = {
        CONF_PRESET_TYPE: PRESET_TYPE_LIGHTING,
        CONF_NAME: "Light",
        CONF_ILLUMINANCE_SENSOR: "sensor.fake_lux",
        CONF_LIGHT: "switch.fake_lamp",
        CONF_SENSOR_KIND: sensor_kind,
        CONF_LIGHTING_MODE: mode,
        CONF_TARGET_DLI: target_dli,
        CONF_LAMP_PPFD: lamp_ppfd,
    }
    if lamp_type:
        data[CONF_LAMP_TYPE] = lamp_type
    entry = MockConfigEntry(domain=DOMAIN, title="Light", data=data)
    entry.add_to_hass(hass)
    return entry


# ---------------------------------------------------------------------------
# Mode off / continuous / supplement
# ---------------------------------------------------------------------------


async def test_mode_off_keeps_lamp_off(hass: HomeAssistant) -> None:
    """В режиме off лампа OFF независимо от target/освещения."""
    entry = _make_lighting_entry(
        hass, mode=LIGHTING_MODE_OFF, target_dli=20.0, sensor_value="0"
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    await controller._tick_now(_utc(2026, 5, 21, 12, 0))
    await hass.async_block_till_done()

    assert "turn_on" not in [c[0] for c in calls]


async def test_mode_indoor_continuous_turns_lamp_on(hass: HomeAssistant) -> None:
    """В режиме indoor_continuous лампа ON всегда."""
    entry = _make_lighting_entry(hass, mode=LIGHTING_MODE_INDOOR_CONTINUOUS)
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    await controller._tick_now(_utc(2026, 5, 21, 12, 0))
    await hass.async_block_till_done()

    assert ("turn_on", "switch.fake_lamp") in calls


async def test_indoor_supplement_on_when_deficit_and_in_window(
    hass: HomeAssistant,
) -> None:
    """indoor_supplement: ON если в окне sunrise..sunset и дефицит DLI."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass, mode=LIGHTING_MODE_INDOOR_SUPPLEMENT, target_dli=10.0
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    await controller._tick_now(_utc(2026, 5, 21, 10, 0))
    await hass.async_block_till_done()

    assert ("turn_on", "switch.fake_lamp") in calls


async def test_indoor_supplement_off_outside_sun_window(
    hass: HomeAssistant,
) -> None:
    """indoor_supplement: OFF ночью (вне sunrise..sunset)."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass, mode=LIGHTING_MODE_INDOOR_SUPPLEMENT, target_dli=10.0
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    await controller._tick_now(_utc(2026, 5, 21, 22, 0))
    await hass.async_block_till_done()

    assert "turn_on" not in [c[0] for c in calls]


# ---------------------------------------------------------------------------
# Natural supplement — decision logic at noon
# ---------------------------------------------------------------------------


async def test_natural_supplement_morning_lamp_off(hass: HomeAssistant) -> None:
    """До полудня controller только мерит, лампа OFF."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_NATURAL_SUPPLEMENT,
        target_dli=20.0,
        sensor_value="1500",  # хороший дневной PPFD
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    await controller._tick_now(_utc(2026, 5, 21, 10, 0))
    await hass.async_block_till_done()

    assert "turn_on" not in [c[0] for c in calls]


async def test_natural_supplement_no_deficit_keeps_lamp_off(
    hass: HomeAssistant,
) -> None:
    """В полдень при достаточной накопленной дозе досветка не нужна."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_NATURAL_SUPPLEMENT,
        target_dli=10.0,
        sensor_value="0",
    )
    controller = GrosfarmLightController(hass, entry)

    # «Накопили» 6 mol/m² за утро (выше половины от 10).
    controller._dli_accumulated = 6.0
    controller._today = _utc(2026, 5, 21, 12, 0).date()
    controller._last_tick_at = _utc(2026, 5, 21, 11, 59)

    calls = _capture_switch_calls(hass)
    await controller._tick_now(_utc(2026, 5, 21, 12, 0))
    await hass.async_block_till_done()

    # Решение принято, дефицита нет → лампа off.
    assert controller._decision_made_today is True
    assert controller._lamp_run_until is None
    assert "turn_on" not in [c[0] for c in calls]


async def test_natural_supplement_with_deficit_plans_lamp(
    hass: HomeAssistant,
) -> None:
    """В полдень при дефиците считается lamp_start_at до заката."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_NATURAL_SUPPLEMENT,
        target_dli=20.0,
        lamp_ppfd=1000.0,  # µmol/m²/s
        sensor_value="0",
    )
    controller = GrosfarmLightController(hass, entry)
    # Накопили 5 → экстраполяция × 2 = 10, дефицит 10 mol/m².
    # 10 / (1000 µmol/s × 1e-6) = 10000 секунд лампы ≈ 2:46:40.
    controller._dli_accumulated = 5.0
    controller._today = _utc(2026, 5, 21, 12, 0).date()
    controller._last_tick_at = _utc(2026, 5, 21, 11, 59)

    calls = _capture_switch_calls(hass)
    await controller._tick_now(_utc(2026, 5, 21, 12, 0))
    await hass.async_block_till_done()

    assert controller._decision_made_today is True
    assert controller._lamp_run_until is not None
    assert controller._lamp_start_at is not None
    # 10 mol/m² при 1000 µmol/m²/s = 10 000 секунд ≈ 2h 46m.
    # sunset 18:00 минус 2h 46m ≈ 15:13.
    assert controller._lamp_start_at.hour == 15
    # В 12:00 лампа ещё не должна гореть — рано.
    assert "turn_on" not in [c[0] for c in calls]


async def test_natural_supplement_lamp_on_during_planned_window(
    hass: HomeAssistant,
) -> None:
    """После расчёта в полдень — в запланированный момент лампа включается."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_NATURAL_SUPPLEMENT,
        target_dli=20.0,
        lamp_ppfd=1000.0,
    )
    controller = GrosfarmLightController(hass, entry)
    # Полдень: принимаем решение при дефиците 10 mol/m² → 10000s до заката.
    controller._dli_accumulated = 5.0
    controller._today = _utc(2026, 5, 21, 12, 0).date()
    controller._last_tick_at = _utc(2026, 5, 21, 11, 59)
    await controller._tick_now(_utc(2026, 5, 21, 12, 0))

    calls = _capture_switch_calls(hass)
    # 16:00 — точно в окне 15:13..18:00.
    await controller._tick_now(_utc(2026, 5, 21, 16, 0))
    await hass.async_block_till_done()
    assert ("turn_on", "switch.fake_lamp") in calls


# ---------------------------------------------------------------------------
# Day rollover + lux conversion + dli native
# ---------------------------------------------------------------------------


async def test_day_rollover_resets_accumulation(hass: HomeAssistant) -> None:
    """Смена суток сбрасывает накопление и решение."""
    entry = _make_lighting_entry(hass, mode=LIGHTING_MODE_OFF)
    _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)
    controller._dli_accumulated = 8.5
    controller._today = _utc(2026, 5, 21, 12, 0).date()
    controller._decision_made_today = True

    await controller._tick_now(_utc(2026, 5, 22, 6, 0))

    assert controller._today == _utc(2026, 5, 22, 6, 0).date()
    assert controller._dli_accumulated == 0.0
    assert controller._decision_made_today is False


async def test_lux_kind_uses_lamp_type_coefficient(hass: HomeAssistant) -> None:
    """kind=lux + lamp_type=LED — PPFD считается через коэффициент 0.015."""
    entry = _make_lighting_entry(
        hass,
        sensor_kind=SENSOR_KIND_LUX,
        lamp_type=LAMP_TYPE_LED,
        sensor_value="10000",  # 10 000 lux
    )
    controller = GrosfarmLightController(hass, entry)
    # 10000 lux × 0.015 = 150 µmol/m²/s.
    assert controller._read_ppfd_now() == pytest.approx(150.0)


async def test_dli_native_overrides_internal_accumulation(
    hass: HomeAssistant,
) -> None:
    """kind=dli — controller читает state напрямую, _dli_accumulated = state."""
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_OFF,
        sensor_kind=SENSOR_KIND_DLI,
        sensor_value="7.3",
    )
    controller = GrosfarmLightController(hass, entry)
    # Заранее «накопили» внутреннее 99 — оно должно быть стёрто native-чтением.
    controller._dli_accumulated = 99.0
    controller._today = _utc(2026, 5, 21, 12, 0).date()

    await controller._tick_now(_utc(2026, 5, 21, 12, 0))

    assert controller._dli_accumulated == pytest.approx(7.3)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


async def test_calibration_writes_lamp_ppfd(hass: HomeAssistant) -> None:
    """Калибровка: baseline + warm-up + with_lamp → lamp_ppfd в entry.data."""
    entry = _make_lighting_entry(
        hass, mode=LIGHTING_MODE_OFF, lamp_type=LAMP_TYPE_LED, sensor_value="50"
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    sample_values = iter(
        [
            # baseline (5 замеров) — 50 µmol/m²/s
            "50",
            "50",
            "50",
            "50",
            "50",
            # with_lamp (5 замеров) — 1050 (лампа +1000)
            "1050",
            "1050",
            "1050",
            "1050",
            "1050",
        ]
    )

    def _next_value(*_a, **_kw) -> None:
        try:
            v = next(sample_values)
        except StopIteration:
            v = "50"
        hass.states.async_set("sensor.fake_lux", v)

    async def _sleep_then_advance(_seconds: float) -> None:
        _next_value()

    with patch.object(
        controller, "_sleep", new=AsyncMock(side_effect=_sleep_then_advance)
    ):
        result = await controller.async_calibrate()

    assert result["lamp_ppfd"] == pytest.approx(1000.0)
    refreshed = hass.config_entries.async_get_entry(entry.entry_id)
    assert refreshed.data[CONF_LAMP_PPFD] == pytest.approx(1000.0)
    # Лампа была включена в процессе и выключена в конце.
    services_called = [s for s, _ in calls]
    assert "turn_on" in services_called
    assert "turn_off" in services_called


async def test_calibration_requires_lamp(hass: HomeAssistant) -> None:
    """Калибровка зоны без CONF_LIGHT — отказ."""
    entry = _make_lighting_entry(hass)
    # Уберём lamp из entry.data.
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != CONF_LIGHT}
    )
    controller = GrosfarmLightController(hass, entry)
    with pytest.raises(CalibrationError):
        await controller.async_calibrate()


async def test_calibration_refuses_dli_sensor_kind(hass: HomeAssistant) -> None:
    """DLI-датчик не выдаёт мгновенного PPFD — калибровать нельзя."""
    entry = _make_lighting_entry(hass, sensor_kind=SENSOR_KIND_DLI)
    controller = GrosfarmLightController(hass, entry)
    with pytest.raises(CalibrationError):
        await controller.async_calibrate()


async def test_tick_suppressed_while_calibrating(hass: HomeAssistant) -> None:
    """Пока идёт калибровка, периодический tick не трогает лампу и не затирает статус.

    Регрессия: tick и async_calibrate оба управляют лампой; без флага _calibrating
    тик в режиме off гасил лампу посреди замера → lamp_ppfd выходил мусорным.
    """
    from custom_components.grosfarm.light_controller import STATUS_CALIBRATING

    # Режим off + лампа сейчас включена: обычный tick вызвал бы turn_off.
    entry = _make_lighting_entry(
        hass, mode=LIGHTING_MODE_OFF, sensor_value="50", light_state="on"
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)
    controller._calibrating = True
    controller._status = STATUS_CALIBRATING

    await controller._tick_now(_utc(2026, 5, 21, 12, 0))

    assert calls == []  # лампа не тронута
    assert controller._status == STATUS_CALIBRATING  # статус калибровки цел


# ---------------------------------------------------------------------------
# Sensor unavailable + status push
# ---------------------------------------------------------------------------


async def test_sensor_unavailable_in_accumulation_turns_lamp_off(
    hass: HomeAssistant,
) -> None:
    """natural_supplement до полудня + sensor=unavailable → лампа OFF, статус acc."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_NATURAL_SUPPLEMENT,
        target_dli=20.0,
        sensor_value="unavailable",
    )
    calls = _capture_switch_calls(hass)
    controller = GrosfarmLightController(hass, entry)

    await controller._tick_now(_utc(2026, 5, 21, 9, 0))
    await hass.async_block_till_done()

    from custom_components.grosfarm.light_controller import (
        STATUS_SENSOR_UNAVAIL_ACC,
    )

    assert controller._status == STATUS_SENSOR_UNAVAIL_ACC
    assert "turn_on" not in [c[0] for c in calls]


async def test_sensor_unavailable_in_supplement_keeps_lamp_on(
    hass: HomeAssistant,
) -> None:
    """В фазе supplement (план построен) sensor отвалил → лампа продолжает жечь."""
    sunrise = _utc(2026, 5, 21, 6, 0)
    sunset = _utc(2026, 5, 21, 18, 0)
    _seed_sun(hass, sunrise, sunset)
    entry = _make_lighting_entry(
        hass,
        mode=LIGHTING_MODE_NATURAL_SUPPLEMENT,
        target_dli=20.0,
        lamp_ppfd=1000.0,
        sensor_value="0",
    )
    controller = GrosfarmLightController(hass, entry)
    # Принимаем решение в полдень — план до заката.
    controller._dli_accumulated = 5.0
    controller._today = _utc(2026, 5, 21, 12, 0).date()
    controller._last_tick_at = _utc(2026, 5, 21, 11, 59)
    await controller._tick_now(_utc(2026, 5, 21, 12, 0))

    # Теперь датчик отваливается.
    hass.states.async_set("sensor.fake_lux", "unavailable")

    calls = _capture_switch_calls(hass)
    # 16:00 — в окне плана 15:13..18:00.
    await controller._tick_now(_utc(2026, 5, 21, 16, 0))
    await hass.async_block_till_done()

    from custom_components.grosfarm.light_controller import (
        STATUS_SENSOR_UNAVAIL_SUP,
    )

    assert controller._status == STATUS_SENSOR_UNAVAIL_SUP
    assert ("turn_on", "switch.fake_lamp") in calls


async def test_status_push_to_cloud_coordinator(hass: HomeAssistant) -> None:
    """Controller дёргает async_push_zone_status у coordinator'а при изменении."""
    entry = _make_lighting_entry(hass, mode=LIGHTING_MODE_OFF)
    controller = GrosfarmLightController(hass, entry)

    # Подсунем фейковый coordinator в hass.data, чтобы controller его нашёл.
    pushed: list[tuple[str, dict]] = []

    class _FakeCoord:
        async def async_push_zone_status(self, entry_id: str, payload: dict) -> None:
            pushed.append((entry_id, payload))

    hass.data.setdefault(DOMAIN, {}).setdefault("_cloud", {})["fake"] = _FakeCoord()

    await controller._tick_now(_utc(2026, 5, 21, 12, 0))
    await hass.async_block_till_done()

    assert len(pushed) == 1
    pushed_entry_id, payload = pushed[0]
    assert pushed_entry_id == entry.entry_id
    assert payload["status"] == "ok"
    assert payload["mode"] == LIGHTING_MODE_OFF


# ---------------------------------------------------------------------------
# light.* лампа (диммируемый свет) — полный белый
# ---------------------------------------------------------------------------


async def test_light_domain_lamp_full_white_on_continuous(
    hass: HomeAssistant,
) -> None:
    """Лампа-light в indoor_continuous включается на полную яркость + белый."""
    hass.states.async_set("sensor.fake_lux", "0", {})
    hass.states.async_set("light.fake_lamp", "off", {"supported_color_modes": ["rgb"]})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Light",
        data={
            CONF_PRESET_TYPE: PRESET_TYPE_LIGHTING,
            CONF_NAME: "Light",
            CONF_ILLUMINANCE_SENSOR: "sensor.fake_lux",
            CONF_LIGHT: "light.fake_lamp",
            CONF_SENSOR_KIND: SENSOR_KIND_PPFD,
            CONF_LIGHTING_MODE: LIGHTING_MODE_INDOOR_CONTINUOUS,
        },
    )
    entry.add_to_hass(hass)

    calls: list[tuple[str, dict]] = []

    async def _h(call) -> None:
        calls.append((call.service, dict(call.data)))
        hass.states.async_set(
            call.data["entity_id"],
            "on" if call.service == "turn_on" else "off",
            {"supported_color_modes": ["rgb"]},
        )

    hass.services.async_register("light", "turn_on", _h)
    hass.services.async_register("light", "turn_off", _h)

    controller = GrosfarmLightController(hass, entry)
    await controller._tick_now(_utc(2026, 5, 21, 12, 0))
    await hass.async_block_till_done()

    on = next(c for c in calls if c[0] == "turn_on")
    assert on[1]["entity_id"] == "light.fake_lamp"
    assert on[1]["brightness_pct"] == 100
    assert on[1]["rgb_color"] == [255, 255, 255]


def test_build_lamp_on_payload_variants() -> None:
    """build_lamp_on_payload подбирает яркость/цвет по supported_color_modes."""
    from types import SimpleNamespace

    from custom_components.grosfarm.light_controller import build_lamp_on_payload

    def _st(modes: list[str]) -> SimpleNamespace:
        return SimpleNamespace(attributes={"supported_color_modes": modes})

    assert build_lamp_on_payload(_st(["rgb"])) == {
        "brightness_pct": 100,
        "rgb_color": [255, 255, 255],
    }
    assert build_lamp_on_payload(_st(["onoff"])) == {}
    assert build_lamp_on_payload(_st(["brightness"])) == {"brightness_pct": 100}
    assert build_lamp_on_payload(_st(["color_temp"])) == {
        "brightness_pct": 100,
        "color_temp_kelvin": 4000,
    }
    assert build_lamp_on_payload(_st(["rgbw"])) == {
        "brightness_pct": 100,
        "rgbw_color": [0, 0, 0, 255],
    }
