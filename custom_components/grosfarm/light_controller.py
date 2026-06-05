"""Локальный controller досветки per lighting-zone.

Считает DLI накопленный за сутки из показаний PPFD/lux/DLI-датчика и
исполняет один из режимов, присланных от cloud:

  off                 — лампа OFF, controller спит
  natural_supplement  — теплица: до 12:00 копим, после полудня экстраполируем
                        ожидаемый ночной дефицит DLI, к закату досвечиваем
  indoor_supplement   — гроубокс без окна: с восхода жжём пока не наберём DLI
  indoor_continuous   — лампа 24/7

Алгоритм local-first: при оффлайне cloud — controller продолжает работать на
последних known mode + target_dli (см. CLAUDE.md «Local-first»).
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ILLUMINANCE_SENSOR,
    CONF_LAMP_PPFD,
    CONF_LAMP_TYPE,
    CONF_LIGHT,
    CONF_LIGHTING_MODE,
    CONF_SENSOR_KIND,
    CONF_TARGET_DLI,
    DEFAULT_LIGHTING_MODE,
    DEFAULT_TARGET_DLI,
    DOMAIN,
    LAMP_TYPE_LUX_TO_PPFD,
    LAMP_TYPE_SUNLIGHT,
    LAMP_TYPE_WARMUP_SECONDS,
    LIGHTING_CALIBRATION_SAMPLE_SECONDS,
    LIGHTING_MODE_INDOOR_CONTINUOUS,
    LIGHTING_MODE_INDOOR_SUPPLEMENT,
    LIGHTING_MODE_NATURAL_SUPPLEMENT,
    LIGHTING_MODE_OFF,
    LIGHTING_TICK_SECONDS,
    SENSOR_KIND_DLI,
    SENSOR_KIND_LUX,
    SENSOR_KIND_PPFD,
)

_LOGGER = logging.getLogger(__name__)

# Астрономический полдень — единая точка решения для natural_supplement.
_DECISION_TIME = time(12, 0)

# Статусы зоны, отправляемые в облако.
STATUS_OK = "ok"
STATUS_SENSOR_UNAVAIL_ACC = "sensor_unavailable_during_accumulation"
STATUS_SENSOR_UNAVAIL_SUP = "sensor_unavailable_during_supplement"
STATUS_LAMP_UNAVAIL = "lamp_unavailable"
STATUS_CALIBRATING = "calibrating"

# Фазы суток для логики при отвале сенсора.
PHASE_ACCUMULATION = "accumulation"  # мерим, лампа не должна гореть
PHASE_SUPPLEMENT = "supplement"  # лампа уже жжёт (или должна) по плану
PHASE_IDLE = "idle"  # mode=off или indoor_continuous — фазой не оперируем

# Минимум между двумя push'ами статуса, если статус не изменился.
_STATUS_PUSH_THROTTLE_SECONDS = 300


class CalibrationError(Exception):
    """Калибровка не смогла завершиться (датчик/лампа недоступны)."""


class GrosfarmLightController:
    """Контроллер досветки для одной lighting-zone."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Сохранить hass+entry; tick не запускать до async_start()."""
        self.hass = hass
        self.entry = entry
        self._unsub_tick: Any = None
        self._unsub_state: Any = None
        # Runtime-state — пересоздаётся при смене суток.
        self._today: date | None = None
        self._dli_accumulated: float = 0.0  # mol/m²
        self._lamp_on_seconds_today: float = 0.0
        self._last_tick_at: datetime | None = None
        self._lamp_was_on: bool = False
        self._lamp_state: bool = False
        self._decision_made_today: bool = False
        self._lamp_start_at: datetime | None = None
        self._lamp_run_until: datetime | None = None
        self._status: str = STATUS_OK
        self._phase: str = PHASE_IDLE
        self._last_pushed_status: str | None = None
        self._last_pushed_at: datetime | None = None
        self._listeners: set[Any] = set()
        # Калибровка и периодический tick оба управляют лампой и статусом. Флаг
        # глушит tick на время async_calibrate, иначе тик погасит лампу/затрёт
        # статус посреди замера и lamp_ppfd получится мусорным.
        self._calibrating: bool = False

    # ---- lifecycle ----

    async def async_start(self) -> None:
        """Запустить периодический tick и подписку на смену state сенсора."""
        sensor_entity = self.entry.data.get(CONF_ILLUMINANCE_SENSOR)
        self._unsub_tick = async_track_time_interval(
            self.hass,
            self._tick_now,
            timedelta(seconds=LIGHTING_TICK_SECONDS),
        )
        if sensor_entity:
            self._unsub_state = async_track_state_change_event(
                self.hass, [sensor_entity], self._on_sensor_change
            )
        # Тик сразу, чтобы не ждать минуту при первом запуске.
        await self._tick_now(dt_util.utcnow())

    async def async_stop(self) -> None:
        """Отписаться от tick и state-изменений."""
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def _on_sensor_change(self, _event: Any) -> None:
        """No-op обработчик — реальная работа в tick. Подписка нужна на будущее."""

    @callback
    def add_listener(self, callback_fn: Callable[[], None]) -> None:
        """Подписать sensor-entity на обновления от controller."""
        self._listeners.add(callback_fn)

    @callback
    def remove_listener(self, callback_fn: Callable[[], None]) -> None:
        """Отписать sensor-entity."""
        self._listeners.discard(callback_fn)

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("listener raised")

    # ---- runtime helpers ----

    def _mode(self) -> str:
        return str(self.entry.data.get(CONF_LIGHTING_MODE, DEFAULT_LIGHTING_MODE))

    def _target_dli(self) -> float:
        return float(self.entry.data.get(CONF_TARGET_DLI, DEFAULT_TARGET_DLI))

    def _switch_entity(self) -> str | None:
        return self.entry.data.get(CONF_LIGHT)

    def _sensor_entity(self) -> str | None:
        return self.entry.data.get(CONF_ILLUMINANCE_SENSOR)

    def _lamp_ppfd(self) -> float:
        return float(self.entry.data.get(CONF_LAMP_PPFD, 0.0))

    def _read_ppfd_now(self) -> float | None:
        """Текущий PPFD в µmol/m²/s; None если сенсор unavailable или kind=dli."""
        raw = self._read_sensor_raw()
        if raw is None:
            return None
        kind = self.entry.data.get(CONF_SENSOR_KIND, SENSOR_KIND_PPFD)
        if kind == SENSOR_KIND_PPFD:
            return raw
        if kind == SENSOR_KIND_LUX:
            lamp_type = self.entry.data.get(CONF_LAMP_TYPE, LAMP_TYPE_SUNLIGHT)
            coeff = LAMP_TYPE_LUX_TO_PPFD.get(lamp_type, 0.015)
            return raw * coeff
        # SENSOR_KIND_DLI: PPFD из него не вытащить, читаем через _read_dli_native.
        return None

    def _read_sensor_raw(self) -> float | None:
        """Сырое значение из state.value; None если sensor unavailable."""
        eid = self._sensor_entity()
        if not eid:
            return None
        st = self.hass.states.get(eid)
        if st is None or st.state in (None, "unknown", "unavailable"):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    def _read_dli_native(self) -> float | None:
        """Если sensor_kind=dli — текущий накопленный DLI прямо из датчика."""
        if self.entry.data.get(CONF_SENSOR_KIND) != SENSOR_KIND_DLI:
            return None
        eid = self._sensor_entity()
        if not eid:
            return None
        st = self.hass.states.get(eid)
        if st is None or st.state in (None, "unknown", "unavailable"):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    # ---- ticking ----

    async def _tick_now(self, now: datetime) -> None:
        """Вызывается по таймеру; делегирует на mode-specific handler."""
        local_now = dt_util.as_local(now)
        if self._calibrating:
            # async_calibrate держит лампу и статус под контролем; пускать mode-
            # логику параллельно нельзя — она погасит лампу и затрёт статус посреди
            # замера. Сдвигаем точку отсчёта, чтобы калибровочное окно (лампа
            # искусственно включена) не попало в накопленный DLI.
            self._last_tick_at = local_now
            return
        self._maybe_roll_over_day(local_now)
        sensor_ok = self._read_and_accumulate(local_now)
        self._track_lamp_on_time(local_now)

        mode = self._mode()
        # Текущая фаза — нужна для решения при unavailable сенсоре.
        self._phase = self._infer_phase(mode, local_now)

        try:
            mode_needs_sensor = mode not in (
                LIGHTING_MODE_OFF,
                LIGHTING_MODE_INDOOR_CONTINUOUS,
            )
            if not sensor_ok and mode_needs_sensor:
                await self._handle_sensor_unavailable(mode, local_now)
            elif mode == LIGHTING_MODE_OFF:
                await self._apply_off()
                self._status = STATUS_OK
            elif mode == LIGHTING_MODE_INDOOR_CONTINUOUS:
                await self._apply_indoor_continuous()
                self._status = STATUS_OK
            elif mode == LIGHTING_MODE_INDOOR_SUPPLEMENT:
                await self._apply_indoor_supplement(local_now)
                if self._status != STATUS_LAMP_UNAVAIL:
                    self._status = STATUS_OK
            elif mode == LIGHTING_MODE_NATURAL_SUPPLEMENT:
                await self._apply_natural_supplement(local_now)
                if self._status != STATUS_LAMP_UNAVAIL:
                    self._status = STATUS_OK
            else:
                _LOGGER.warning("Unknown lighting mode %r — treating as off", mode)
                await self._apply_off()
        except Exception:
            _LOGGER.exception("light_controller tick failed")
        self._last_tick_at = local_now
        self._notify_listeners()
        await self._maybe_push_status(local_now)

    def _read_and_accumulate(self, local_now: datetime) -> bool:
        """Снимает текущее значение датчика, обновляет _dli_accumulated.

        Возвращает True, если датчик отдал валидный замер; False — если unavailable.
        """
        kind = self.entry.data.get(CONF_SENSOR_KIND, SENSOR_KIND_PPFD)
        if kind == SENSOR_KIND_DLI:
            native = self._read_dli_native()
            if native is None:
                return False
            self._dli_accumulated = native
            return True
        ppfd = self._read_ppfd_now()
        if ppfd is None:
            return False
        self._accumulate_dli(ppfd, local_now)
        return True

    def _infer_phase(self, mode: str, local_now: datetime) -> str:
        """Определить фазу для решения при отвале датчика."""
        if mode in (LIGHTING_MODE_OFF, LIGHTING_MODE_INDOOR_CONTINUOUS):
            return PHASE_IDLE
        if mode == LIGHTING_MODE_NATURAL_SUPPLEMENT:
            decision_time = local_now.replace(
                hour=_DECISION_TIME.hour, minute=0, second=0, microsecond=0
            )
            # До полудня — accumulation. После — supplement (если решение принято).
            if local_now < decision_time:
                return PHASE_ACCUMULATION
            if self._decision_made_today and self._lamp_run_until is not None:
                return PHASE_SUPPLEMENT
            return PHASE_ACCUMULATION  # план не построен, по-прежнему ждём
        if mode == LIGHTING_MODE_INDOOR_SUPPLEMENT:
            # Если в текущей сессии лампа уже жгла — мы в supplement.
            return PHASE_SUPPLEMENT if self._lamp_state else PHASE_ACCUMULATION
        return PHASE_IDLE

    async def _handle_sensor_unavailable(self, mode: str, local_now: datetime) -> None:
        """Поведение при отвале сенсора, зависит от фазы.

        - В accumulation: лампу гасим, статус ставим *_during_accumulation.
        - В supplement: продолжаем уже запланированное (natural — план до заката,
          indoor_supplement — до sunset/набора DLI), статус *_during_supplement.
        """
        if self._phase == PHASE_SUPPLEMENT:
            # Дожгём по плану. Натурал-режим: lamp_start_at..lamp_run_until.
            if mode == LIGHTING_MODE_NATURAL_SUPPLEMENT:
                await self._apply_natural_supplement(local_now)
            elif mode == LIGHTING_MODE_INDOOR_SUPPLEMENT:
                # Без сенсора DLI не растёт. Жгём до sunset чтобы не оставить
                # растения голодными в момент когда мы знали что план был активен.
                sunset = self._sunset_local(local_now)
                in_window = sunset is None or local_now < sunset
                await self._ensure_lamp(state=in_window)
            self._status = STATUS_SENSOR_UNAVAIL_SUP
        else:
            await self._ensure_lamp(state=False)
            self._status = STATUS_SENSOR_UNAVAIL_ACC

    def _maybe_roll_over_day(self, local_now: datetime) -> None:
        today = local_now.date()
        if self._today != today:
            self._today = today
            self._dli_accumulated = 0.0
            self._lamp_on_seconds_today = 0.0
            self._decision_made_today = False
            self._lamp_start_at = None
            self._lamp_run_until = None
            self._status = STATUS_OK

    def _accumulate_dli(self, ppfd: float, local_now: datetime) -> None:
        if self._last_tick_at is None:
            self._last_tick_at = local_now
            return
        dt_sec = (local_now - self._last_tick_at).total_seconds()
        if dt_sec <= 0:
            return
        # PPFD µmol/m²/s × t (s) → µmol/m² → /1e6 = mol/m²
        self._dli_accumulated += ppfd * dt_sec / 1_000_000

    def _track_lamp_on_time(self, local_now: datetime) -> None:
        if self._lamp_was_on and self._last_tick_at is not None:
            self._lamp_on_seconds_today += (
                local_now - self._last_tick_at
            ).total_seconds()
        self._lamp_was_on = self._lamp_state

    # ---- mode handlers ----

    async def _apply_off(self) -> None:
        await self._ensure_lamp(state=False)

    async def _apply_indoor_continuous(self) -> None:
        await self._ensure_lamp(state=True)

    async def _apply_indoor_supplement(self, local_now: datetime) -> None:
        """С восхода жжём пока DLI не наберём."""
        sunrise = self._sunrise_local(local_now)
        sunset = self._sunset_local(local_now)
        target = self._target_dli()
        if target <= 0:
            await self._ensure_lamp(state=False)
            return
        # Включаем когда восход прошёл, дефицит ещё есть, до заката.
        in_window = sunrise <= local_now < sunset if sunrise and sunset else True
        deficit = target - self._dli_accumulated
        await self._ensure_lamp(state=in_window and deficit > 0)

    async def _apply_natural_supplement(self, local_now: datetime) -> None:
        """Теплица: до полудня копим, после — экстраполируем и догоняем досветкой.

        Решение принимается **один раз в сутки** в момент 12:00 local-time:
        если экстраполированный natural-DLI (2 × текущий) меньше target — считаем
        дефицит и планируем досветку с расчётным lamp_start_at до заката.
        """
        target = self._target_dli()
        lamp_ppfd = self._lamp_ppfd()
        decision_time = local_now.replace(
            hour=_DECISION_TIME.hour, minute=0, second=0, microsecond=0
        )
        sunset = self._sunset_local(local_now)

        # До полудня — только меряем.
        if local_now < decision_time:
            await self._ensure_lamp(state=False)
            return

        # В полдень — раз в сутки расчёт.
        if not self._decision_made_today:
            self._decision_made_today = True
            extrapolated = self._dli_accumulated * 2
            deficit = max(0.0, target - extrapolated)
            if deficit <= 0 or lamp_ppfd <= 0 or sunset is None:
                self._lamp_run_until = None
                self._lamp_start_at = None
            else:
                seconds_needed = deficit / lamp_ppfd * 1_000_000
                self._lamp_run_until = sunset
                # Если уже не успеваем (start_at в прошлом) — стартуем прямо сейчас.
                self._lamp_start_at = max(
                    sunset - timedelta(seconds=seconds_needed), local_now
                )
            _LOGGER.info(
                "natural_supplement decision: extrapolated=%.2f, deficit=%.2f, "
                "start=%s, until=%s",
                extrapolated,
                target - extrapolated,
                self._lamp_start_at,
                self._lamp_run_until,
            )

        if self._lamp_run_until is None or self._lamp_start_at is None:
            await self._ensure_lamp(state=False)
            return

        if self._lamp_start_at <= local_now < self._lamp_run_until:
            await self._ensure_lamp(state=True)
        else:
            await self._ensure_lamp(state=False)

    # ---- switch interaction ----

    async def _ensure_lamp(self, state: bool) -> None:
        eid = self._switch_entity()
        if not eid:
            return
        desired = "turn_on" if state else "turn_off"
        st = self.hass.states.get(eid)
        if st is None or st.state in ("unknown", "unavailable"):
            self._status = STATUS_LAMP_UNAVAIL
            return
        current_on = str(st.state).lower() in ("on", "true", "1")
        if current_on == state:
            self._lamp_state = state
            return
        try:
            await self.hass.services.async_call(
                "switch", desired, {"entity_id": eid}, blocking=True
            )
            self._lamp_state = state
        except Exception:
            _LOGGER.exception("ensure_lamp failed (%s → %s)", eid, desired)
            self._status = STATUS_LAMP_UNAVAIL

    # ---- sun.sun helpers ----

    def _sunrise_local(self, local_now: datetime) -> datetime | None:
        return self._sun_attr_local(local_now, "next_rising")

    def _sunset_local(self, local_now: datetime) -> datetime | None:
        return self._sun_attr_local(local_now, "next_setting")

    def _sun_attr_local(self, local_now: datetime, attr: str) -> datetime | None:
        """`next_rising` / `next_setting` от sun.sun в local-time для СЕГОДНЯ.

        sun.sun отдаёт UTC ISO время для следующего события — оно может попасть
        на завтра. Для нашего расчёта возьмём сегодняшний event: если next попал
        на завтра — отнимем сутки.
        """
        st = self.hass.states.get("sun.sun")
        if st is None:
            return None
        raw = st.attributes.get(attr)
        if not raw:
            return None
        try:
            evt_utc = dt_util.parse_datetime(raw)
        except (TypeError, ValueError):
            return None
        if evt_utc is None:
            return None
        evt_local = dt_util.as_local(evt_utc)
        # Если событие на завтра — берём вчерашнее эквивалентное (cycle).
        while evt_local.date() > local_now.date():
            evt_local -= timedelta(days=1)
        return evt_local

    # ---- introspection (для сенсоров и status push) ----

    def snapshot(self) -> dict[str, Any]:
        """Скриншот state для статус-сенсоров и push'а в облако."""
        return {
            "mode": self._mode(),
            "target_dli": self._target_dli(),
            "dli_today": round(self._dli_accumulated, 3),
            "lamp_on_seconds_today": int(self._lamp_on_seconds_today),
            "lamp_state": "on" if self._lamp_state else "off",
            "status": self._status,
            "decision_made_today": self._decision_made_today,
            "lamp_start_at": self._lamp_start_at.isoformat()
            if self._lamp_start_at
            else None,
            "lamp_run_until": self._lamp_run_until.isoformat()
            if self._lamp_run_until
            else None,
        }

    # ---- calibration ----

    async def async_calibrate(self) -> dict[str, Any]:
        """Авто-калибровка lamp_ppfd. Возвращает dict с результатом.

        Алгоритм:
          1. baseline = медиана PPFD за `LIGHTING_CALIBRATION_SAMPLE_SECONDS`.
          2. Включить лампу.
          3. Подождать warm-up (зависит от lamp_type, если задан).
          4. with_lamp = медиана PPFD за окно сэмплов.
          5. Записать `lamp_ppfd = with_lamp - baseline`.
          6. Выключить лампу.
        """
        if self._calibrating:
            raise CalibrationError("калибровка уже выполняется")
        if self.entry.data.get(CONF_SENSOR_KIND) == SENSOR_KIND_DLI:
            raise CalibrationError("DLI-native датчик не подходит для калибровки")
        if not self._switch_entity():
            raise CalibrationError("В зоне нет лампы (CONF_LIGHT не задан)")

        lamp_type = self.entry.data.get(CONF_LAMP_TYPE)
        warmup = LAMP_TYPE_WARMUP_SECONDS.get(lamp_type, 60) if lamp_type else 60
        total = warmup + 2 * LIGHTING_CALIBRATION_SAMPLE_SECONDS
        prev_status = self._status
        self._calibrating = True
        self._status = STATUS_CALIBRATING
        _LOGGER.info(
            "calibration started (lamp_type=%s, warmup=%ss, total ~%ss)",
            lamp_type,
            warmup,
            total,
        )

        try:
            baseline = await self._sample_ppfd_median()
            await self._ensure_lamp(state=True)
            await self._sleep(warmup)
            with_lamp = await self._sample_ppfd_median()
            await self._ensure_lamp(state=False)
            lamp_ppfd = max(0.0, with_lamp - baseline)
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_LAMP_PPFD: lamp_ppfd},
            )
            _LOGGER.info(
                "calibration done: baseline=%.2f, with_lamp=%.2f, lamp_ppfd=%.2f",
                baseline,
                with_lamp,
                lamp_ppfd,
            )
            return {
                "baseline_ppfd": baseline,
                "with_lamp_ppfd": with_lamp,
                "lamp_ppfd": lamp_ppfd,
                "warmup_seconds": warmup,
            }
        finally:
            self._status = prev_status
            self._calibrating = False

    async def _sample_ppfd_median(self) -> float:
        """Снять 5 замеров за `LIGHTING_CALIBRATION_SAMPLE_SECONDS` и взять медиану."""
        samples: list[float] = []
        step = LIGHTING_CALIBRATION_SAMPLE_SECONDS / 5
        for _ in range(5):
            v = self._read_ppfd_now()
            if v is not None:
                samples.append(v)
            await self._sleep(step)
        if not samples:
            raise CalibrationError("Сенсор не отдал ни одного валидного замера")
        return statistics.median(samples)

    async def _sleep(self, seconds: float) -> None:
        """Asyncio sleep; вынесено в метод чтобы можно было замокать в тестах."""
        import asyncio  # local — чтобы избежать импорта на module-level

        await asyncio.sleep(seconds)

    # ---- status push ----

    async def _maybe_push_status(self, local_now: datetime) -> None:
        """Pусь статуса в облако: при изменении статуса либо раз в N минут."""
        same_status = self._status == self._last_pushed_status
        fresh = (
            self._last_pushed_at is not None
            and (local_now - self._last_pushed_at).total_seconds()
            < _STATUS_PUSH_THROTTLE_SECONDS
        )
        if same_status and fresh:
            return
        cloud_map = self.hass.data.get(DOMAIN, {}).get("_cloud", {})
        if not cloud_map:
            return
        payload = self.snapshot()
        # snapshot() уже содержит status + dli_today + lamp_state + ...
        for coordinator in cloud_map.values():
            try:
                await coordinator.async_push_zone_status(self.entry.entry_id, payload)
            except Exception:
                _LOGGER.exception("status push to coordinator failed")
        self._last_pushed_status = self._status
        self._last_pushed_at = local_now
