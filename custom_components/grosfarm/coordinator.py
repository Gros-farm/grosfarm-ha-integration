"""Cloud-sync coordinator для Grosfarm-интеграции.

Singleton per cloud-entry: держит долгоживущий WebSocket-канал, регистрирует все
zone-entries (heating/humidifying/monitoring) в облаке как секции, маппит входящие
уставки на HA-сущности и шлёт телеметрию.

Cloud mental model — секции с расписанием параметров (см. `mock-cloud/PROTOCOL.md`).
HA mental model — config entries с типом `preset_type`. Маппинг живёт здесь, в
`_PARAMETER_TO_PRESET` и `_DEVICE_CLASS_TO_INDICATOR`. Расширяется добавлением строк
при появлении новых presets (свет, полив, вентиляция).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_CLASS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    GrosfarmAPIError,
    GrosfarmAuthError,
    GrosfarmCloudClient,
    GrosfarmStream,
    RegistrationResult,
)
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CHILD_ENTRY_ID,
    CONF_HUMIDITY_SENSOR,
    CONF_ILLUMINANCE_SENSOR,
    CONF_LIGHT,
    CONF_LIGHTING_MODE,
    CONF_LOGIN,
    CONF_MAC_ADDRESS,
    CONF_PASSWORD,
    CONF_PRESET_TEMPS,
    CONF_PRESET_TYPE,
    CONF_SENSOR,
    CONF_TARGET_DLI,
    CONF_TARGET_HUMIDITY,
    CONF_TARGET_SENSOR,
    DEVICE_CLASS_TO_INDICATOR,
    DOMAIN,
    PRESET_TYPE_CLOUD,
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
    PRESET_TYPE_LIGHTING,
    PRESET_TYPE_MONITORING,
    RECONNECT_BACKOFF_SECONDS,
    TELEMETRY_PUSH_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Маппинги cloud ↔ HA (тонкий слой)
# ---------------------------------------------------------------------------

# Cloud parameter → (HA preset_type, что обновляется в entry.data).
# Heating: ключ внутри dict CONF_PRESET_TEMPS. Humidifying/Lighting: ключ entry.data.
_PARAMETER_TO_PRESET: dict[str, tuple[str, str]] = {
    "air_temperature_day": (PRESET_TYPE_HEATING, "home_temp"),
    "air_temperature_night": (PRESET_TYPE_HEATING, "sleep_temp"),
    "air_humidity_day": (PRESET_TYPE_HUMIDIFYING, CONF_TARGET_HUMIDITY),
    "dli_target": (PRESET_TYPE_LIGHTING, CONF_TARGET_DLI),
}


def _zone_sensor_entity_id(entry: ConfigEntry) -> str | None:
    """Достать единственный sensor entity для данного preset_type."""
    preset = entry.data.get(CONF_PRESET_TYPE)
    if preset == PRESET_TYPE_HEATING:
        return entry.data.get(CONF_TARGET_SENSOR)
    if preset == PRESET_TYPE_HUMIDIFYING:
        return entry.data.get(CONF_HUMIDITY_SENSOR)
    if preset == PRESET_TYPE_MONITORING:
        return entry.data.get(CONF_SENSOR)
    if preset == PRESET_TYPE_LIGHTING:
        return entry.data.get(CONF_ILLUMINANCE_SENSOR)
    return None


def _zone_actuator_entity_id(entry: ConfigEntry) -> str | None:
    """Управляемый switch у зоны (для команд cloud-side). Только lighting сейчас."""
    if entry.data.get(CONF_PRESET_TYPE) == PRESET_TYPE_LIGHTING:
        return entry.data.get(CONF_LIGHT)
    return None


# ---------------------------------------------------------------------------
# Координатор
# ---------------------------------------------------------------------------


@dataclass
class _LinkedZone:
    """Запись о zone-entry, который cloud видит как секцию."""

    entry_id: str
    section_id: str
    preset_type: str
    sensor_entity_id: str | None


class GrosfarmCoordinator:
    """Долгоживущий cloud-канал на одну облачную учётку."""

    def __init__(self, hass: HomeAssistant, cloud_entry: ConfigEntry) -> None:
        """Сохранить hass и cloud-entry; ничего не подключать до async_start()."""
        self.hass = hass
        self._entry = cloud_entry
        self._client: GrosfarmCloudClient | None = None
        self._stream: GrosfarmStream | None = None
        self._registration: RegistrationResult | None = None
        self._zones_by_section: dict[str, _LinkedZone] = {}
        self._zones_by_entry: dict[str, _LinkedZone] = {}
        self._setpoints_version: int = 0
        self._telemetry_task: asyncio.Task[None] | None = None
        self._reregister_pending: asyncio.Event = asyncio.Event()
        self._reregister_task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._listeners: set[Any] = set()

    @property
    def is_connected(self) -> bool:
        """Bootstrapped = authenticated + registered хотя бы раз.

        Внутренний флаг для retry/reregister-петель: True означает «мы уже
        прошли bootstrap, дальше живём на WS-реконнекте». НЕ отражает текущее
        состояние линка — для индикатора связи в UI используй `is_cloud_live`.
        """
        return self._registration is not None and self._client is not None

    @property
    def is_cloud_live(self) -> bool:
        """Реальная live-связь с облаком прямо сейчас: WS-канал открыт.

        В отличие от `is_connected` (одноразовый bootstrap-флаг, который не
        сбрасывается), это отражает текущее состояние сокета. После падения
        мока линк отваливается и здесь становится False — сенсор связи покажет
        «autonomous», а не залипнет на «connected».
        """
        return self._stream is not None and self._stream.is_connected

    @property
    def setpoints_version(self) -> int:
        """Текущая версия уставок (отслеживается coordinator'ом)."""
        return self._setpoints_version

    @property
    def cloud_url(self) -> str:
        """URL облака для отображения в diag."""
        return str(self._entry.data.get(CONF_BASE_URL, ""))

    @callback
    def add_listener(self, callback_fn: Callable[[], None]) -> None:
        """Подписать sensor-entity на обновления (registration / setpoints)."""
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

    @callback
    def _on_stream_connection_change(self, connected: bool) -> None:
        """WS-линк поднялся/отвалился — перерисовать сенсоры связи.

        Без этого CloudConnectionSensor не узнал бы о падении линка и продолжил
        бы показывать прежнее значение до следующего setpoints-push'а.
        """
        _LOGGER.debug("Cloud WS link %s", "up" if connected else "down")
        self._notify_listeners()

    # ---- lifecycle ----

    async def async_start(self) -> None:
        """Запустить cloud-канал.

        Облако доступно → authenticate → register → snapshot → WS → loops.
        Облако недоступно → старт в АВТОНОМНОМ режиме: запись грузится, сенсоры
        поднимаются (CloudConnectionSensor покажет «autonomous»), подключение
        догоняем в фоне `_connect_retry_loop`. Облако-офлайн — штатный режим
        (мок живёт на ноуте оператора и большую часть времени недоступен), это
        не ошибка. А вот неверные креды (GrosfarmAuthError) — реальная ошибка
        конфигурации: пробрасываем в setup → ConfigEntryAuthFailed → reauth.
        """
        session = async_get_clientsession(self.hass)
        self._client = GrosfarmCloudClient(
            session,
            self._entry.data[CONF_BASE_URL],
            self._entry.data[CONF_LOGIN],
            self._entry.data[CONF_PASSWORD],
            self._entry.data[CONF_API_KEY],
        )
        try:
            await self._async_connect()
        except GrosfarmAuthError:
            raise  # → ConfigEntryAuthFailed в __init__.async_setup_entry
        except GrosfarmAPIError as exc:
            _LOGGER.warning(
                "Облако недоступно (%s) — старт в автономном режиме, "
                "подключение догоним в фоне",
                exc,
            )
            self._connect_task = asyncio.create_task(
                self._connect_retry_loop(), name="grosfarm_connect"
            )

        self._reregister_task = asyncio.create_task(
            self._reregister_loop(), name="grosfarm_reregister"
        )

    async def _async_connect(self) -> None:
        """Единичная попытка подключения: auth → register → snapshot → WS → telemetry.

        Бросает GrosfarmAuthError (отвергнутые креды) или GrosfarmAPIError (облако
        недоступно), если bootstrap не прошёл. При успехе is_connected → True.
        """
        assert self._client is not None
        await self._client.authenticate()
        await self._register_or_reregister()

        # Подтянем уставки HTTP-фоллбэком — WS-update прилетит вторым копированием.
        await self._fetch_and_apply_setpoints()

        self._stream = GrosfarmStream(
            session=async_get_clientsession(self.hass),
            url=self._registration.stream_url,  # type: ignore[union-attr]
            access_token_provider=self._refresh_token,
            current_version=self._setpoints_version,
            on_setpoints=self._on_setpoints_update,
            on_command=self._on_command,
            backoff_seconds=RECONNECT_BACKOFF_SECONDS,
            on_connection_change=self._on_stream_connection_change,
        )
        self._stream.start()
        if self._telemetry_task is None or self._telemetry_task.done():
            self._telemetry_task = asyncio.create_task(
                self._telemetry_loop(), name="grosfarm_telemetry"
            )
        self._notify_listeners()  # CloudConnectionSensor → connected

    async def _connect_retry_loop(self) -> None:
        """Фоновое переподключение в автономном режиме (backoff до первого успеха)."""
        backoff = RECONNECT_BACKOFF_SECONDS
        attempt = 0
        try:
            while not self.is_connected:
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
                try:
                    await self._async_connect()
                except GrosfarmAuthError:
                    # Облако ответило, но отвергло креды — это ошибка конфигурации,
                    # дальше пытаться бессмысленно. Запись уже загружена в
                    # автономном режиме (не валим её): логируем и выходим из петли.
                    # Пользователю нужно поправить login/password/api_key и
                    # перезагрузить запись.
                    _LOGGER.exception(
                        "Облако отвергло креды — остаёмся в автономном режиме"
                    )
                    return
                except GrosfarmAPIError as exc:
                    _LOGGER.debug("Облако всё ещё недоступно: %s", exc)
                else:
                    _LOGGER.info("Облако подключено после автономного старта")
                    return
        except asyncio.CancelledError:
            return

    async def async_stop(self) -> None:
        """Прибить фоновые задачи и закрыть WS."""
        for task in (self._telemetry_task, self._reregister_task, self._connect_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._telemetry_task = None
        self._reregister_task = None
        self._connect_task = None
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    @callback
    def request_reregister(self) -> None:
        """Внешний триггер: состав zone-entries изменился — перерегистрироваться."""
        self._reregister_pending.set()

    async def async_push_zone_status(
        self, entry_id: str, payload: dict[str, Any]
    ) -> None:
        """Pусь runtime-статус зоны в облако.

        Вызывается light-controller'ом при изменении status. Если зона ещё не
        зарегистрирована в облаке (controller подзагрузился до cloud-coordinator'а),
        тихо игнорируем.
        """
        zone = self._zones_by_entry.get(entry_id)
        if zone is None or self._client is None or self._registration is None:
            return
        body = {"section_id": zone.section_id, **payload}
        try:
            await self._client.post_zone_status(
                self._registration.control_unit_id, body
            )
        except GrosfarmAuthError:
            if self._client is not None:
                try:
                    await self._client.authenticate()
                    await self._client.post_zone_status(
                        self._registration.control_unit_id, body
                    )
                except GrosfarmAPIError:
                    _LOGGER.exception("status push re-auth failed")
        except GrosfarmAPIError as exc:
            _LOGGER.warning("status push failed: %s", exc)

    # ---- registration ----

    def _mac_address(self) -> str:
        """Псевдо-MAC из entry_id — стабильный per cloud-entry."""
        if CONF_MAC_ADDRESS in self._entry.data:
            return str(self._entry.data[CONF_MAC_ADDRESS])
        hexs = self._entry.entry_id.replace("-", "")[:12].ljust(12, "0")
        return ":".join(hexs[i : i + 2].upper() for i in range(0, 12, 2))

    def _entity_friendly_name(self, entity_id: str) -> str | None:
        """Берём friendly_name из state — это то что юзер видит в HA UI."""
        st = self.hass.states.get(entity_id)
        if st is None:
            return None
        name = st.attributes.get("friendly_name") or st.name
        return str(name) if name else None

    def _collect_zones(self) -> list[ConfigEntry]:
        return [
            e
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != self._entry.entry_id
            and e.data.get(CONF_PRESET_TYPE) != PRESET_TYPE_CLOUD
        ]

    async def _register_or_reregister(self) -> None:
        assert self._client is not None
        zones = self._collect_zones()
        sections = [{"external_id": z.entry_id, "name": z.title} for z in zones]
        # Подсунем сенсоры — informational, мок проверяет только секции.
        sensors: list[dict[str, Any]] = []
        devices: list[dict[str, Any]] = []
        for z in zones:
            sensor_id = _zone_sensor_entity_id(z)
            indicator = self._indicator_for_zone(z)
            if sensor_id and indicator:
                sensors.append(
                    {
                        "external_id": sensor_id,
                        "section_external_id": z.entry_id,
                        "indicators": [indicator],
                        "name": self._entity_friendly_name(sensor_id),
                    }
                )
            elif sensor_id:
                # Нераспознанный device_class (EC, pH, …) — у облака нет такого
                # индикатора. Раньше подменяли на "air_temperature", что врало про
                # суть замера; теперь сенсор просто не регистрируем (секция всё
                # равно заводится, телеметрия таких зон тоже скипается).
                _LOGGER.debug(
                    "Зона %s: cloud-индикатор для %s не определён — "
                    "сенсор не регистрируем",
                    z.entry_id,
                    sensor_id,
                )
            actuator_id = _zone_actuator_entity_id(z)
            if actuator_id:
                # Cloud адресует устройства по external_id. Используем entity_id —
                # он уникален в HA и стабилен между перезагрузками.
                devices.append(
                    {
                        "external_id": actuator_id,
                        "type": "control",
                        "elements": ["power"],
                        "name": self._entity_friendly_name(actuator_id),
                    }
                )

        self._registration = await self._client.register_control_unit(
            mac_address=self._mac_address(),
            name=f"HA edge {self._entry.entry_id[:8]}",
            firmware_version="ha-0.3.0",
            sections=sections,
            sensors=sensors,
            devices=devices,
        )

        new_zones: dict[str, _LinkedZone] = {}
        for z in zones:
            section_id = self._registration.section_ids.get(z.entry_id)
            if not section_id:
                continue
            new_zones[z.entry_id] = _LinkedZone(
                entry_id=z.entry_id,
                section_id=section_id,
                preset_type=z.data.get(CONF_PRESET_TYPE, PRESET_TYPE_MONITORING),
                sensor_entity_id=_zone_sensor_entity_id(z),
            )
        self._zones_by_entry = new_zones
        self._zones_by_section = {z.section_id: z for z in new_zones.values()}
        _LOGGER.info(
            "Cloud зарегистрирован cu=%s, зон=%d",
            self._registration.control_unit_id,
            len(new_zones),
        )
        self._notify_listeners()

    async def _fetch_and_apply_setpoints(self) -> None:
        """Снять snapshot уставок HTTP'ом и применить.

        Тихо проглатывает недоступность облака (GrosfarmAPIError) — это фоллбэк к
        WS-push'у, не критичный путь. Вызывается при bootstrap'е и после каждой
        перерегистрации: свежедобавленная зона иначе осталась бы на дефолтах до
        следующего самостоятельного push'а от облака.
        """
        if self._client is None or self._registration is None:
            return
        try:
            snapshot = await self._client.get_setpoints(
                self._registration.control_unit_id
            )
        except GrosfarmAPIError as exc:
            _LOGGER.warning("Setpoints fetch failed: %s", exc)
            return
        self._apply_setpoints(snapshot)

    async def _reregister_loop(self) -> None:
        try:
            while True:
                await self._reregister_pending.wait()
                self._reregister_pending.clear()
                # Дебаунс: подождём 1 секунду на случай каскадных setup'ов.
                await asyncio.sleep(1.0)
                if not self.is_connected:
                    # Офлайн: переподключение вместе с актуальным составом зон
                    # сделает _connect_retry_loop — здесь ничего не делаем.
                    continue
                reregistered = False
                try:
                    await self._register_or_reregister()
                    reregistered = True
                except GrosfarmAuthError:
                    _LOGGER.warning("Re-register: token expired, re-auth")
                    assert self._client is not None
                    try:
                        await self._client.authenticate()
                        await self._register_or_reregister()
                        reregistered = True
                    except GrosfarmAPIError as exc:
                        _LOGGER.warning("Re-register re-auth failed: %s", exc)
                except GrosfarmAPIError as exc:
                    _LOGGER.warning("Re-register failed: %s", exc)
                if reregistered:
                    # Состав зон сменился — подтянем актуальные уставки, чтобы
                    # свежедобавленная зона не висела на дефолтах до следующего
                    # push'а от облака.
                    await self._fetch_and_apply_setpoints()
        except asyncio.CancelledError:
            return

    # ---- inbound: setpoints + commands ----

    async def _on_setpoints_update(self, msg: dict[str, Any]) -> None:
        self._apply_setpoints(msg)

    async def _on_command(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        _LOGGER.info("Cloud command %s: %s", msg_type, msg.get("command_id"))
        is_refresh = (
            msg_type == "command.refresh_setpoints"
            and self._client is not None
            and self._registration is not None
        )
        if is_refresh:
            try:
                snapshot = await self._client.get_setpoints(  # type: ignore[union-attr]
                    self._registration.control_unit_id  # type: ignore[union-attr]
                )
                self._apply_setpoints(snapshot)
            except GrosfarmAPIError:
                _LOGGER.exception("refresh_setpoints failed")
        elif msg_type == "command.set_device_state":
            await self._apply_device_command(msg.get("payload") or {})

    async def _apply_device_command(self, payload: dict[str, Any]) -> None:
        """Облако дёрнуло актуатор (например, лампу досветки).

        Payload: {"device_external_id": "switch.lamp", "state": "on"|"off"}.
        Маппим в `switch.turn_on` / `switch.turn_off`. Если entity_id не из
        domain `switch` — отказываемся: не лезем в свет/выключатели произвольно.
        """
        entity_id = payload.get("device_external_id")
        state = str(payload.get("state", "")).lower()
        if not entity_id or state not in ("on", "off"):
            _LOGGER.warning("set_device_state ignored: %s", payload)
            return
        if not entity_id.startswith("switch."):
            _LOGGER.warning(
                "set_device_state for non-switch domain ignored: %s", entity_id
            )
            return
        service = "turn_on" if state == "on" else "turn_off"
        try:
            await self.hass.services.async_call(
                "switch", service, {"entity_id": entity_id}, blocking=True
            )
            _LOGGER.info("set_device_state %s → %s", entity_id, service)
        except Exception:
            _LOGGER.exception("set_device_state failed for %s", entity_id)

    def _apply_setpoints(self, payload: dict[str, Any]) -> None:
        version = int(payload.get("version", 0))
        if version < self._setpoints_version:
            return
        self._setpoints_version = version
        if self._stream is not None:
            self._stream.update_known_version(version)
        for section in payload.get("sections", []):
            section_id = section.get("section_id")
            zone = self._zones_by_section.get(section_id)
            if zone is None:
                continue
            targets = section.get("targets", {}) or {}
            self._apply_zone_targets(zone, targets)
            if zone.preset_type == PRESET_TYPE_LIGHTING:
                # Cloud может прислать mode отдельно от numeric-параметров.
                mode = section.get("lighting_mode")
                if mode:
                    self._apply_zone_mode(zone, str(mode))
        self._notify_listeners()

    def _apply_zone_targets(self, zone: _LinkedZone, targets: dict[str, float]) -> None:
        entry = self.hass.config_entries.async_get_entry(zone.entry_id)
        if entry is None:
            return
        if zone.preset_type == PRESET_TYPE_HEATING:
            self._apply_heating(entry, targets)
        elif zone.preset_type == PRESET_TYPE_HUMIDIFYING:
            self._apply_humidifying(entry, targets)
        elif zone.preset_type == PRESET_TYPE_LIGHTING:
            self._apply_lighting(entry, targets)
        # monitoring: cloud не пушит уставки, only telemetry.

    def _apply_heating(self, entry: ConfigEntry, targets: dict[str, float]) -> None:
        preset_temps = dict(entry.data.get(CONF_PRESET_TEMPS, {}))
        changed: dict[str, float] = {}
        for parameter, value in targets.items():
            mapping = _PARAMETER_TO_PRESET.get(parameter)
            if mapping is None or mapping[0] != PRESET_TYPE_HEATING:
                continue
            preset_key = mapping[1]
            preset_temps[preset_key] = float(value)
            changed[preset_key] = float(value)
        if not changed:
            return
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PRESET_TEMPS: preset_temps}
        )
        self._propagate_to_child(entry, changed)
        _LOGGER.info(
            "Уставки heating %s ← %s (v=%d)",
            entry.title,
            changed,
            self._setpoints_version,
        )

    def _apply_humidifying(self, entry: ConfigEntry, targets: dict[str, float]) -> None:
        new_value: float | None = None
        for parameter, value in targets.items():
            mapping = _PARAMETER_TO_PRESET.get(parameter)
            if mapping is None or mapping[0] != PRESET_TYPE_HUMIDIFYING:
                continue
            new_value = float(value)
        if new_value is None:
            return
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TARGET_HUMIDITY: new_value}
        )
        self._propagate_to_child(entry, {CONF_TARGET_HUMIDITY: new_value})
        _LOGGER.info(
            "Уставка humidifying %s ← %s%% (v=%d)",
            entry.title,
            new_value,
            self._setpoints_version,
        )

    def _apply_lighting(self, entry: ConfigEntry, targets: dict[str, float]) -> None:
        new_dli: float | None = None
        for parameter, value in targets.items():
            mapping = _PARAMETER_TO_PRESET.get(parameter)
            if mapping is None or mapping[0] != PRESET_TYPE_LIGHTING:
                continue
            new_dli = float(value)
        if new_dli is None:
            return
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TARGET_DLI: new_dli}
        )
        _LOGGER.info(
            "Lighting target_dli %s ← %.2f mol/m² (v=%d)",
            entry.title,
            new_dli,
            self._setpoints_version,
        )

    def _apply_zone_mode(self, zone: _LinkedZone, mode: str) -> None:
        entry = self.hass.config_entries.async_get_entry(zone.entry_id)
        if entry is None:
            return
        if entry.data.get(CONF_LIGHTING_MODE) == mode:
            return
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LIGHTING_MODE: mode}
        )
        _LOGGER.info("Lighting mode %s ← %s", entry.title, mode)

    def _propagate_to_child(self, parent: ConfigEntry, changed: dict[str, Any]) -> None:
        child_id = parent.data.get(CONF_CHILD_ENTRY_ID)
        if child_id is None:
            return
        child = self.hass.config_entries.async_get_entry(child_id)
        if child is None:
            return
        self.hass.config_entries.async_update_entry(
            child, options={**child.options, **changed}
        )

    # ---- outbound: telemetry ----

    async def _telemetry_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TELEMETRY_PUSH_INTERVAL_SECONDS)
                try:
                    await self._send_one_batch()
                except GrosfarmAuthError:
                    _LOGGER.warning("Telemetry: token expired, re-auth")
                    if self._client is not None:
                        try:
                            await self._client.authenticate()
                        except GrosfarmAPIError:
                            _LOGGER.exception("re-auth failed")
                except Exception as exc:
                    _LOGGER.warning("Telemetry batch failed: %s", exc)
        except asyncio.CancelledError:
            return

    async def _send_one_batch(self) -> None:
        if self._registration is None or self._client is None:
            return
        measurements = self._collect_measurements()
        if not measurements:
            return
        if self._stream is not None:
            sent = await self._stream.send_telemetry(measurements)
            if sent is not None:
                return
        await self._client.post_measurements(
            self._registration.control_unit_id, measurements
        )

    def _collect_measurements(self) -> list[dict[str, Any]]:
        now_iso = datetime.now(UTC).isoformat()
        result: list[dict[str, Any]] = []
        for zone in self._zones_by_entry.values():
            entity_id = zone.sensor_entity_id
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (None, "unknown", "unavailable"):
                continue
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                continue
            indicator = self._indicator_for_state(state) or self._indicator_for_zone_id(
                zone.entry_id
            )
            if not indicator:
                continue
            result.append(
                {
                    "section_id": zone.section_id,
                    "indicator": indicator,
                    "value": value,
                    "measured_at": now_iso,
                    "source_type": "sensor",
                    "sensor_external_id": entity_id,
                }
            )
        return result

    def _indicator_for_zone(self, entry: ConfigEntry) -> str | None:
        preset = entry.data.get(CONF_PRESET_TYPE)
        if preset == PRESET_TYPE_HEATING:
            return "air_temperature"
        if preset == PRESET_TYPE_HUMIDIFYING:
            return "air_humidity"
        if preset == PRESET_TYPE_LIGHTING:
            return "illuminance"
        sensor = _zone_sensor_entity_id(entry)
        if sensor:
            state = self.hass.states.get(sensor)
            if state is not None:
                return self._indicator_for_state(state)
        return None

    def _indicator_for_zone_id(self, entry_id: str) -> str | None:
        entry = self.hass.config_entries.async_get_entry(entry_id)
        return self._indicator_for_zone(entry) if entry else None

    def _indicator_for_state(self, state: Any) -> str | None:
        device_class = state.attributes.get(CONF_DEVICE_CLASS)
        if device_class is None:
            return None
        return DEVICE_CLASS_TO_INDICATOR.get(str(device_class))

    async def _refresh_token(self) -> str:
        if self._client is None:
            raise GrosfarmAPIError("client not initialised")
        if self._client.access_token is None:
            await self._client.authenticate()
        return self._client.access_token  # type: ignore[return-value]


_ = uuid  # placeholder для будущей миграции entry-схемы
