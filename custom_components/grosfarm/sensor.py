"""Sensor-платформа Grosfarm — entities для дашборда.

Для каждой lighting-зоны выставляет 5 сенсоров:
  * `sensor.grosfarm_<zone>_dli_today`           — накопленный DLI, mol/m²
  * `sensor.grosfarm_<zone>_target_dli`          — целевой DLI на сутки
  * `sensor.grosfarm_<zone>_lamp_on_minutes`     — минуты лампы за сегодня
  * `sensor.grosfarm_<zone>_status`              — runtime-статус (ok/...)
  * `sensor.grosfarm_<zone>_mode`                — режим (off/natural/...)

Для cloud-entry — 2 диагностических сенсора:
  * `sensor.grosfarm_cloud_<title>_connected`     — connected/disconnected
  * `sensor.grosfarm_cloud_<title>_setpoints_version` — текущая версия уставок

Для sensor-only зоны (heating без нагревателя, humidifying без увлажнителя,
monitoring) — 1 диагностический сенсор:
  * `sensor.grosfarm_<zone>_monitored` — зеркалит значение исходного датчика,
    в атрибутах: source_entity_id / cloud_indicator / mode. Нужен чтобы зона
    была видна в HA (device + entity), иначе она существует только как config
    entry — actuator'а/helper'а и своих сущностей у неё нет.

Heating/humidifying С actuator — без этого сенсора: у них есть climate/
humidifier entity от child generic_thermostat/hygrostat.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_AREA_ID,
    CONF_CHILD_ENTRY_ID,
    CONF_HUMIDITY_SENSOR,
    CONF_NAME,
    CONF_PRESET_TYPE,
    CONF_SENSOR,
    CONF_TARGET_SENSOR,
    DEVICE_CLASS_TO_INDICATOR,
    DOMAIN,
    PRESET_TYPE_CLOUD,
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
    PRESET_TYPE_LIGHTING,
    PRESET_TYPE_MONITORING,
)
from .coordinator import GrosfarmCoordinator
from .light_controller import GrosfarmLightController

_LOGGER = logging.getLogger(__name__)

_LIGHT_KEY = "_light"
_CLOUD_KEY = "_cloud"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создать сенсоры зависимо от preset_type."""
    preset = entry.data.get(CONF_PRESET_TYPE)
    if preset == PRESET_TYPE_LIGHTING:
        controller = hass.data.get(DOMAIN, {}).get(_LIGHT_KEY, {}).get(entry.entry_id)
        if controller is None:
            _LOGGER.debug(
                "lighting zone %s — controller не запущен (sensor-only zone)",
                entry.entry_id,
            )
            return
        async_add_entities(_lighting_sensors(controller, entry), True)
        _assign_device_area(hass, entry)
    elif preset == PRESET_TYPE_CLOUD:
        coordinator = hass.data.get(DOMAIN, {}).get(_CLOUD_KEY, {}).get(entry.entry_id)
        if coordinator is None:
            return
        async_add_entities(_cloud_sensors(coordinator, entry), True)
    elif preset in (
        PRESET_TYPE_HEATING,
        PRESET_TYPE_HUMIDIFYING,
        PRESET_TYPE_MONITORING,
    ):
        # Sensor-only зона (нет actuator/child) невидима в HA — поднимаем прокси-
        # сенсор, чтобы появились device + entity. Heating/humid С actuator имеют
        # climate/humidifier от child — там прокси не нужен.
        if CONF_CHILD_ENTRY_ID in entry.data:
            return
        source = _zone_source_sensor(entry)
        if not source:
            return
        async_add_entities([GrosfarmSensorOnlyZoneSensor(hass, entry, source)], True)
        _assign_device_area(hass, entry, model="Sensor-only zone")


def _zone_source_sensor(entry: ConfigEntry) -> str | None:
    """Исходный датчик sensor-only зоны (ключ зависит от preset_type)."""
    preset = entry.data.get(CONF_PRESET_TYPE)
    if preset == PRESET_TYPE_HEATING:
        return entry.data.get(CONF_TARGET_SENSOR)
    if preset == PRESET_TYPE_HUMIDIFYING:
        return entry.data.get(CONF_HUMIDITY_SENSOR)
    if preset == PRESET_TYPE_MONITORING:
        return entry.data.get(CONF_SENSOR)
    return None


def _assign_device_area(
    hass: HomeAssistant, entry: ConfigEntry, model: str = "Lighting zone"
) -> None:
    """Привязать device зоны к area из entry.data[CONF_AREA_ID].

    HA сам кладёт entities в карточку area на overview, если device в area.
    Без этой привязки сенсоры в дашборде area-page не появятся.
    """
    area_id = entry.data.get(CONF_AREA_ID)
    if not area_id:
        return
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if device is None:
        # Device создаётся лениво при первом add_entities — могло ещё не успеть.
        # Создадим явно, чтобы сразу выставить area.
        device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Gros.farm",
            model=model,
            name=entry.data.get(CONF_NAME, entry.title),
        )
    if device.area_id != area_id:
        dev_reg.async_update_device(device.id, area_id=area_id)


def _lighting_sensors(
    controller: GrosfarmLightController, entry: ConfigEntry
) -> list[SensorEntity]:
    return [
        DliTodaySensor(controller, entry),
        TargetDliSensor(controller, entry),
        LampOnMinutesSensor(controller, entry),
        LightingStatusSensor(controller, entry),
        LightingModeSensor(controller, entry),
    ]


def _cloud_sensors(
    coordinator: GrosfarmCoordinator, entry: ConfigEntry
) -> list[SensorEntity]:
    return [
        CloudConnectionSensor(coordinator, entry),
        CloudSetpointsVersionSensor(coordinator, entry),
    ]


# ---------------------------------------------------------------------------
# Lighting zone — общий базовый класс
# ---------------------------------------------------------------------------


class _GrosfarmLightingBase(SensorEntity):
    """Базовый класс для всех сенсоров lighting-зоны.

    Subscribe-ит к controller для real-time обновлений, без polling.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, controller: GrosfarmLightController, entry: ConfigEntry) -> None:
        """Сохранить ссылку на controller и подцепить device."""
        self._controller = controller
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_NAME, entry.title),
            manufacturer="Gros.farm",
            model="Lighting zone",
        )

    async def async_added_to_hass(self) -> None:
        """Подписаться на updates от controller."""
        self._controller.add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        """Отписаться."""
        self._controller.remove_listener(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    def _snapshot(self) -> dict[str, Any]:
        return self._controller.snapshot()


class DliTodaySensor(_GrosfarmLightingBase):
    """Сколько mol/m² света зона получила за сегодня."""

    _attr_translation_key = "dli_today"
    _attr_native_unit_of_measurement = "mol/m²"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:sun-clock"

    def __init__(self, controller: GrosfarmLightController, entry: ConfigEntry) -> None:
        """Init с уникальным id."""
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_dli_today"

    @property
    def native_value(self) -> float | None:
        """Накопленная сегодня доза."""
        return self._snapshot().get("dli_today")


class TargetDliSensor(_GrosfarmLightingBase):
    """Целевой DLI на сутки (пришёл из cloud, либо 0 если cloud не настроен)."""

    _attr_translation_key = "target_dli"
    _attr_native_unit_of_measurement = "mol/m²"
    _attr_icon = "mdi:target"

    def __init__(self, controller: GrosfarmLightController, entry: ConfigEntry) -> None:
        """Init с уникальным id."""
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_target_dli"

    @property
    def native_value(self) -> float | None:
        """Целевое значение из cloud."""
        return self._snapshot().get("target_dli")


class LampOnMinutesSensor(_GrosfarmLightingBase):
    """Сколько минут лампа отработала за сегодня."""

    _attr_translation_key = "lamp_on_minutes_today"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:timer-sand"

    def __init__(self, controller: GrosfarmLightController, entry: ConfigEntry) -> None:
        """Init."""
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_lamp_on_minutes"

    @property
    def native_value(self) -> int | None:
        """Минуты включения за сутки."""
        seconds = self._snapshot().get("lamp_on_seconds_today")
        if seconds is None:
            return None
        return int(seconds) // 60


class LightingStatusSensor(_GrosfarmLightingBase):
    """Runtime-статус controller (ok / sensor_unavailable_* / lamp_unavail / ...)."""

    _attr_translation_key = "lighting_status"
    _attr_icon = "mdi:cog-clockwise"

    def __init__(self, controller: GrosfarmLightController, entry: ConfigEntry) -> None:
        """Init."""
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        """Текущий status."""
        return self._snapshot().get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Доп. инфо для дебага: фаза, lamp_state, окно плана."""
        snap = self._snapshot()
        return {
            "lamp_state": snap.get("lamp_state"),
            "decision_made_today": snap.get("decision_made_today"),
            "lamp_start_at": snap.get("lamp_start_at"),
            "lamp_run_until": snap.get("lamp_run_until"),
        }


class LightingModeSensor(_GrosfarmLightingBase):
    """Текущий mode: off / natural / indoor_supplement / indoor_continuous."""

    _attr_translation_key = "lighting_mode"
    _attr_icon = "mdi:auto-mode"

    def __init__(self, controller: GrosfarmLightController, entry: ConfigEntry) -> None:
        """Init."""
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_mode"

    @property
    def native_value(self) -> str | None:
        """Текущий mode."""
        return self._snapshot().get("mode")


# ---------------------------------------------------------------------------
# Cloud entry — diagnostic
# ---------------------------------------------------------------------------


class _GrosfarmCloudBase(SensorEntity):
    """Базовый класс для диагностических сенсоров cloud-entry."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: GrosfarmCoordinator, entry: ConfigEntry) -> None:
        """Сохранить coordinator и привязаться к device."""
        self._coordinator = coordinator
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Gros.farm",
            model="Cloud connection",
        )

    async def async_added_to_hass(self) -> None:
        """Подписка на updates coordinator."""
        self._coordinator.add_listener(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        """Отписка."""
        self._coordinator.remove_listener(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class CloudConnectionSensor(_GrosfarmCloudBase):
    """Индикатор связи для дашборда: connected / autonomous.

    «autonomous» (а не «disconnected») — потому что работа без облака это штатный
    режим: локальные контуры идут своим ходом, coordinator сам переподключится,
    когда облако появится. Красного статуса config-entry при этом нет.
    """

    _attr_translation_key = "cloud_connection"
    _attr_icon = "mdi:cloud-sync-outline"

    def __init__(self, coordinator: GrosfarmCoordinator, entry: ConfigEntry) -> None:
        """Init."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def native_value(self) -> str:
        """Connected при наличии связи с облаком, иначе autonomous (офлайн-режим)."""
        return "connected" if self._coordinator.is_connected else "autonomous"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Доп. инфо: cloud URL."""
        return {"cloud_url": self._coordinator.cloud_url}


class CloudSetpointsVersionSensor(_GrosfarmCloudBase):
    """Версия уставок, последняя принятая из cloud."""

    _attr_translation_key = "cloud_setpoints_version"
    _attr_icon = "mdi:source-branch"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: GrosfarmCoordinator, entry: ConfigEntry) -> None:
        """Init."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_setpoints_version"

    @property
    def native_value(self) -> int:
        """Текущая версия."""
        return self._coordinator.setpoints_version


# ---------------------------------------------------------------------------
# Sensor-only zone — диагностический прокси
# ---------------------------------------------------------------------------


class GrosfarmSensorOnlyZoneSensor(SensorEntity):
    """Диагностический прокси для sensor-only зоны (нет actuator/child).

    Heating без нагревателя, humidifying без увлажнителя и monitoring не спавнят
    helper и не имеют своих сущностей — в HA существуют только как config entry
    (+ секция в облаке). Этот сенсор делает зону видимой: создаёт device и
    зеркалит значение исходного датчика, а в атрибутах показывает источник,
    cloud-indicator и режим. Управления нет — значение уходит в облако
    телеметрией через coordinator.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "monitored_value"
    _attr_should_poll = False

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, source_entity_id: str
    ) -> None:
        """Привязка к device зоны. Метаданные/значение читаются вживую из source."""
        self._entry = entry
        self._source_entity_id = source_entity_id
        self._attr_unique_id = f"{entry.entry_id}_monitored"
        preset = entry.data.get(CONF_PRESET_TYPE, "zone")
        self._mode = f"{preset} (sensor-only)"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_NAME, entry.title),
            manufacturer="Gros.farm",
            model="Sensor-only zone",
        )
        self._unsub: Callable[[], None] | None = None

    def _source_state(self) -> State | None:
        """Текущий стейт исходного датчика (None если ещё не загружен)."""
        return self.hass.states.get(self._source_entity_id)

    @property
    def native_value(self) -> float | str | None:
        """Зеркалит текущее значение исходного датчика (float если число)."""
        st = self._source_state()
        if st is None or st.state in (None, "unknown", "unavailable"):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return st.state

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Единица измерения исходного датчика (вживую)."""
        st = self._source_state()
        return st.attributes.get("unit_of_measurement") if st else None

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """device_class исходного датчика, если валиден для sensor (вживую)."""
        st = self._source_state()
        raw = st.attributes.get("device_class") if st else None
        if raw is None:
            return None
        try:
            return SensorDeviceClass(str(raw))
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Что и как настроено: источник, cloud-indicator, режим."""
        st = self._source_state()
        raw_dc = st.attributes.get("device_class") if st else None
        return {
            "source_entity_id": self._source_entity_id,
            "cloud_indicator": (
                DEVICE_CLASS_TO_INDICATOR.get(str(raw_dc)) if raw_dc else None
            ),
            "mode": self._mode,
        }

    async def async_added_to_hass(self) -> None:
        """Подписка на source + снять текущее значение.

        source мог загрузиться РАНЬШE нас (тогда update_before_add увидел None и
        событие подписки мы бы пропустили) — поэтому пишем стейт сразу после
        подписки. Если source загрузится позже — поймает подписка.
        """
        self._unsub = async_track_state_change_event(
            self.hass, [self._source_entity_id], self._on_source_change
        )
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Отписка."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_source_change(self, event: Any) -> None:
        self.async_write_ha_state()


# Тип для удобства подсказок IDE — экспортируется чтобы интеграция могла
# использовать те же type aliases в тестах.
SensorFactory = Callable[
    [GrosfarmLightController | GrosfarmCoordinator, ConfigEntry], SensorEntity
]
