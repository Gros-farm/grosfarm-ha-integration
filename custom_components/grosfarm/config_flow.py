"""Config + options flow for the Grosfarm integration.

User picks an **indicator type** (temperature / humidity / other) on the
menu step, then fills one form. The control device (heater/humidifier)
is optional — if it's left blank we still create the zone and register
the sensor for telemetry, just without spawning a `generic_thermostat`
or `generic_hygrostat` helper.

Internal note: the on-disk `preset_type` values are still the original
`heating` / `humidifying` / `monitoring` strings, preserved so entries
created before this refactor keep working unmodified. Only UI labels
changed.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.humidifier import HumidifierDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import PERCENTAGE, Platform, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_API_KEY,
    CONF_AREA_ID,
    CONF_BASE_URL,
    CONF_CHILD_ENTRY_ID,
    CONF_HEATER,
    CONF_HUMIDIFIER,
    CONF_HUMIDIFIER_DEVICE_CLASS,
    CONF_HUMIDITY_SENSOR,
    CONF_ILLUMINANCE_SENSOR,
    CONF_LAMP_TYPE,
    CONF_LIGHT,
    CONF_LOGIN,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PRESET_TEMPS,
    CONF_PRESET_TYPE,
    CONF_SENSOR,
    CONF_SENSOR_KIND,
    CONF_TARGET_HUMIDITY,
    CONF_TARGET_SENSOR,
    DEFAULT_BASE_URL,
    DEFAULT_PRESET_TEMPS,
    DEFAULT_TARGET_HUMIDITY,
    DOMAIN,
    ERROR_HEATER_ALREADY_USED,
    ERROR_HUMIDIFIER_ALREADY_USED,
    ERROR_LIGHT_ALREADY_USED,
    LAMP_TYPE_LED,
    LAMP_TYPE_LUX_TO_PPFD,
    PRESET_KEYS,
    PRESET_TYPE_CLOUD,
    PRESET_TYPE_HEATING,
    PRESET_TYPE_HUMIDIFYING,
    PRESET_TYPE_LIGHTING,
    PRESET_TYPE_MONITORING,
    SENSOR_KIND_DLI,
    SENSOR_KIND_LUX,
    SENSOR_KIND_PPFD,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zone_unique_id(sensor: str, actuator: str | None, preset_type: str) -> str:
    """Composite unique_id from preset type + wired hardware.

    Preset type is part of the key so the same sensor can simultaneously
    back a control zone (with an actuator) and a sensor-only zone of a
    different indicator type — they're independent records as far as the
    cloud is concerned.
    """
    if actuator is None or actuator == "":
        return f"{preset_type}__{slugify(sensor)}"
    return f"{preset_type}__{slugify(sensor)}__{slugify(actuator)}"


# ---------------------------------------------------------------------------
# Schemas (per indicator type)
# ---------------------------------------------------------------------------

SCHEMA_HEATING = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_AREA_ID): selector.AreaSelector(),
        vol.Required(CONF_TARGET_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=Platform.SENSOR,
                device_class=SensorDeviceClass.TEMPERATURE,
            )
        ),
        # Optional: no relay → sensor-only zone (telemetry to the cloud).
        vol.Optional(CONF_HEATER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=Platform.SWITCH)
        ),
    }
)


SCHEMA_HUMIDIFYING = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_AREA_ID): selector.AreaSelector(),
        vol.Required(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=Platform.SENSOR,
                device_class=SensorDeviceClass.HUMIDITY,
            )
        ),
        # Optional: no actuator → sensor-only zone.
        vol.Optional(CONF_HUMIDIFIER): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=Platform.SWITCH)
        ),
        # Only meaningful when the humidifier above IS picked; ignored otherwise.
        vol.Required(
            CONF_HUMIDIFIER_DEVICE_CLASS,
            default=HumidifierDeviceClass.HUMIDIFIER,
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    HumidifierDeviceClass.HUMIDIFIER,
                    HumidifierDeviceClass.DEHUMIDIFIER,
                ],
                translation_key=CONF_HUMIDIFIER_DEVICE_CLASS,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)


SCHEMA_MONITORING = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_AREA_ID): selector.AreaSelector(),
        # No device_class filter — any sensor type (CO2, soil moisture,
        # EC, pH, lux, ...) can become a Grosfarm indicator.
        vol.Required(CONF_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=Platform.SENSOR)
        ),
    }
)


_SENSOR_KIND_OPTIONS = [
    selector.SelectOptionDict(value=SENSOR_KIND_PPFD, label="PPFD (µmol/m²/s)"),
    selector.SelectOptionDict(value=SENSOR_KIND_LUX, label="lux"),
    selector.SelectOptionDict(value=SENSOR_KIND_DLI, label="DLI (mol/m²·day)"),
]

_LAMP_TYPE_OPTIONS = [
    selector.SelectOptionDict(value=k, label=k) for k in LAMP_TYPE_LUX_TO_PPFD
]


SCHEMA_LIGHTING = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_AREA_ID): selector.AreaSelector(),
        # PPFD/DLI-датчики у HA нет device_class — допускаем любой sensor.
        # Юзер сам выбирает kind ниже.
        vol.Required(CONF_ILLUMINANCE_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=Platform.SENSOR)
        ),
        vol.Required(
            CONF_SENSOR_KIND, default=SENSOR_KIND_PPFD
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_SENSOR_KIND_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_SENSOR_KIND,
            )
        ),
        # Опционально: лампа досветки. Диммируемый свет (WLED/Tuya/Zigbee RGBW)
        # ИЛИ простое реле/розетка. light.* controller гонит на полную яркость
        # (белый), switch.* — вкл/выкл. Пусто → sensor-only zone, телеметрия в облако.
        vol.Optional(CONF_LIGHT): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[Platform.LIGHT, Platform.SWITCH])
        ),
    }
)


SCHEMA_LIGHTING_LAMP_TYPE = vol.Schema(
    {
        vol.Required(CONF_LAMP_TYPE, default=LAMP_TYPE_LED): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_LAMP_TYPE_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_LAMP_TYPE,
            )
        ),
    }
)


SCHEMA_CLOUD = vol.Schema(
    {
        vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): selector.TextSelector(),
        vol.Required(CONF_LOGIN): selector.TextSelector(),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_API_KEY): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


def _build_presets_schema(current: dict[str, float]) -> vol.Schema:
    """Six optional NumberSelectors for the per-phase heating setpoints.

    Used by `OptionsFlow` for heating zones (and eventually by the cloud
    sync coordinator). Optional everywhere so the user (or cloud) can
    leave some phases unset — the climate entity simply won't expose
    unset presets in its dropdown.
    """
    temp_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfTemperature.CELSIUS,
            min=5,
            max=35,
            step=0.5,
        )
    )
    fields: dict[Any, Any] = {}
    for key in PRESET_KEYS:
        if key in current:
            fields[vol.Optional(key, default=current[key])] = temp_selector
        else:
            fields[vol.Optional(key)] = temp_selector
    return vol.Schema(fields)


def _build_humidity_schema(current: float) -> vol.Schema:
    """Single NumberSelector for the humidifying setpoint."""
    return vol.Schema(
        {
            vol.Required(
                CONF_TARGET_HUMIDITY, default=current
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=PERCENTAGE,
                    min=10,
                    max=95,
                    step=1,
                )
            )
        }
    )


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class _CloudProbeError(Exception):
    """Маркер ошибки cloud-probe с form-error кодом для UI."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GrosfarmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Menu-driven flow: pick an indicator type, then fill one form."""

    VERSION = 1

    # Буфер между шагами lighting (если выбран kind=lux, нужен 2-й шаг).
    _lighting_buffer: dict[str, Any]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Expose the type-specific options editor."""
        return GrosfarmOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the indicator-type picker menu."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[
                PRESET_TYPE_HEATING,
                PRESET_TYPE_HUMIDIFYING,
                PRESET_TYPE_LIGHTING,
                PRESET_TYPE_MONITORING,
                PRESET_TYPE_CLOUD,
            ],
        )

    # -- Temperature -------------------------------------------------------

    async def async_step_heating(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Temperature indicator — sensor required, heater relay optional."""
        if user_input is None:
            return self.async_show_form(
                step_id=PRESET_TYPE_HEATING, data_schema=SCHEMA_HEATING
            )

        heater = user_input.get(CONF_HEATER) or None

        await self.async_set_unique_id(
            _zone_unique_id(user_input[CONF_TARGET_SENSOR], heater, PRESET_TYPE_HEATING)
        )
        self._abort_if_unique_id_configured()

        if heater and self._actuator_is_in_use(heater, CONF_HEATER):
            return self.async_show_form(
                step_id=PRESET_TYPE_HEATING,
                data_schema=SCHEMA_HEATING,
                errors={CONF_HEATER: ERROR_HEATER_ALREADY_USED},
            )

        data: dict[str, Any] = {
            CONF_PRESET_TYPE: PRESET_TYPE_HEATING,
            CONF_NAME: user_input[CONF_NAME],
            CONF_AREA_ID: user_input[CONF_AREA_ID],
            CONF_TARGET_SENSOR: user_input[CONF_TARGET_SENSOR],
        }
        if heater:
            # Control zone: spawn generic_thermostat in async_setup_entry.
            data[CONF_HEATER] = heater
            data[CONF_PRESET_TEMPS] = dict(DEFAULT_PRESET_TEMPS)
        return self.async_create_entry(title=user_input[CONF_NAME], data=data)

    # -- Humidity ----------------------------------------------------------

    async def async_step_humidifying(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Humidity indicator — sensor required, humidifier optional."""
        if user_input is None:
            return self.async_show_form(
                step_id=PRESET_TYPE_HUMIDIFYING, data_schema=SCHEMA_HUMIDIFYING
            )

        humidifier = user_input.get(CONF_HUMIDIFIER) or None

        await self.async_set_unique_id(
            _zone_unique_id(
                user_input[CONF_HUMIDITY_SENSOR],
                humidifier,
                PRESET_TYPE_HUMIDIFYING,
            )
        )
        self._abort_if_unique_id_configured()

        if humidifier and self._actuator_is_in_use(humidifier, CONF_HUMIDIFIER):
            return self.async_show_form(
                step_id=PRESET_TYPE_HUMIDIFYING,
                data_schema=SCHEMA_HUMIDIFYING,
                errors={CONF_HUMIDIFIER: ERROR_HUMIDIFIER_ALREADY_USED},
            )

        data: dict[str, Any] = {
            CONF_PRESET_TYPE: PRESET_TYPE_HUMIDIFYING,
            CONF_NAME: user_input[CONF_NAME],
            CONF_AREA_ID: user_input[CONF_AREA_ID],
            CONF_HUMIDITY_SENSOR: user_input[CONF_HUMIDITY_SENSOR],
        }
        if humidifier:
            data[CONF_HUMIDIFIER] = humidifier
            data[CONF_HUMIDIFIER_DEVICE_CLASS] = user_input[
                CONF_HUMIDIFIER_DEVICE_CLASS
            ]
            data[CONF_TARGET_HUMIDITY] = DEFAULT_TARGET_HUMIDITY
        return self.async_create_entry(title=user_input[CONF_NAME], data=data)

    # -- Lighting ----------------------------------------------------------

    async def async_step_lighting(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Освещённость: датчик (PPFD/lux/DLI) + опциональная лампа.

        Локальный controller считает DLI и решает когда жечь, исходя из режима
        и target_dli, которые присылает облако. Если облако оффлайн — controller
        продолжает работать на последних known значениях (local-first).
        """
        if user_input is None:
            return self.async_show_form(
                step_id=PRESET_TYPE_LIGHTING, data_schema=SCHEMA_LIGHTING
            )

        light = user_input.get(CONF_LIGHT) or None

        await self.async_set_unique_id(
            _zone_unique_id(
                user_input[CONF_ILLUMINANCE_SENSOR], light, PRESET_TYPE_LIGHTING
            )
        )
        self._abort_if_unique_id_configured()

        if light and self._actuator_is_in_use(light, CONF_LIGHT):
            return self.async_show_form(
                step_id=PRESET_TYPE_LIGHTING,
                data_schema=SCHEMA_LIGHTING,
                errors={CONF_LIGHT: ERROR_LIGHT_ALREADY_USED},
            )

        self._lighting_buffer: dict[str, Any] = {
            CONF_PRESET_TYPE: PRESET_TYPE_LIGHTING,
            CONF_NAME: user_input[CONF_NAME],
            CONF_AREA_ID: user_input[CONF_AREA_ID],
            CONF_ILLUMINANCE_SENSOR: user_input[CONF_ILLUMINANCE_SENSOR],
            CONF_SENSOR_KIND: user_input[CONF_SENSOR_KIND],
        }
        if light:
            self._lighting_buffer[CONF_LIGHT] = light

        # Если выбран lux — нужно ещё спросить тип лампы для конверсии в PPFD.
        if user_input[CONF_SENSOR_KIND] == SENSOR_KIND_LUX:
            return await self.async_step_lighting_lamp_type()
        return self._create_lighting_entry()

    async def async_step_lighting_lamp_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Дополнительный шаг — тип лампы для lux→PPFD конверсии."""
        if user_input is None:
            return self.async_show_form(
                step_id="lighting_lamp_type",
                data_schema=SCHEMA_LIGHTING_LAMP_TYPE,
            )
        self._lighting_buffer[CONF_LAMP_TYPE] = user_input[CONF_LAMP_TYPE]
        return self._create_lighting_entry()

    def _create_lighting_entry(self) -> ConfigFlowResult:
        data = self._lighting_buffer
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    # -- Other indicator ---------------------------------------------------

    async def async_step_monitoring(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Other indicator — sensor only, no actuator."""
        if user_input is None:
            return self.async_show_form(
                step_id=PRESET_TYPE_MONITORING, data_schema=SCHEMA_MONITORING
            )

        await self.async_set_unique_id(
            _zone_unique_id(user_input[CONF_SENSOR], None, PRESET_TYPE_MONITORING)
        )
        self._abort_if_unique_id_configured()

        data = {
            CONF_PRESET_TYPE: PRESET_TYPE_MONITORING,
            CONF_NAME: user_input[CONF_NAME],
            CONF_AREA_ID: user_input[CONF_AREA_ID],
            CONF_SENSOR: user_input[CONF_SENSOR],
        }
        return self.async_create_entry(title=user_input[CONF_NAME], data=data)

    # -- Cloud connection --------------------------------------------------

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Подключение к облаку Gros.farm.

        Не зона — singleton-entry, который держит long-lived WS-канал и пушит
        уставки в существующие zone-entries.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            unique = f"cloud::{user_input[CONF_BASE_URL]}::{user_input[CONF_LOGIN]}"
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured()

            try:
                await self._probe_cloud(user_input)
            except _CloudProbeError as exc:
                errors["base"] = exc.code
            else:
                title = f"Gros.farm cloud ({user_input[CONF_LOGIN]})"
                data = {CONF_PRESET_TYPE: PRESET_TYPE_CLOUD, **user_input}
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id=PRESET_TYPE_CLOUD,
            data_schema=SCHEMA_CLOUD,
            errors=errors,
        )

    async def _probe_cloud(self, data: dict[str, Any]) -> None:
        """Лёгкий health + auth check перед созданием entry."""
        # Локальный импорт — модуль может затянуть aiohttp при первой загрузке.
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from .api import GrosfarmAPIError, GrosfarmAuthError, GrosfarmCloudClient

        session = async_get_clientsession(self.hass)
        client = GrosfarmCloudClient(
            session,
            data[CONF_BASE_URL],
            data[CONF_LOGIN],
            data[CONF_PASSWORD],
            data[CONF_API_KEY],
        )
        try:
            await client.authenticate()
        except GrosfarmAuthError as exc:
            raise _CloudProbeError("invalid_auth") from exc
        except GrosfarmAPIError as exc:
            raise _CloudProbeError("cannot_connect") from exc

    # -- Validation helper -------------------------------------------------

    def _actuator_is_in_use(self, actuator: str, conf_key: str) -> bool:
        """Return True if any existing Grosfarm entry already owns this actuator."""
        return any(
            existing.data.get(conf_key) == actuator
            for existing in self._async_current_entries()
        )


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class GrosfarmOptionsFlow(OptionsFlow):
    """Per-indicator setpoint editor; dispatches by `preset_type`.

    Writes merged values to BOTH our parent entry (durable) and the spawned
    child entry's options (immediate effect). Sensor-only zones (no
    actuator) get an empty form — there's nothing to configure locally.
    """

    def __init__(self, entry: ConfigEntry) -> None:
        """Init with the entry we'll edit."""
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dispatch based on preset type and presence of a child helper."""
        # Sensor-only zones don't spawn a child → nothing local to edit.
        if self._entry.data.get(CONF_CHILD_ENTRY_ID) is None:
            return await self.async_step_sensor_only(user_input)

        preset_type = self._entry.data.get(CONF_PRESET_TYPE, PRESET_TYPE_HEATING)
        if preset_type == PRESET_TYPE_HUMIDIFYING:
            return await self.async_step_humidity(user_input)
        return await self.async_step_presets(user_input)

    async def async_step_presets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Temperature with control — edit per-phase setpoints (partial merge)."""
        current = self._entry.data.get(CONF_PRESET_TEMPS, DEFAULT_PRESET_TEMPS)
        if user_input is None:
            return self.async_show_form(
                step_id="presets", data_schema=_build_presets_schema(current)
            )
        merged = {**current, **user_input}
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_PRESET_TEMPS: merged},
        )
        self._propagate_to_child(user_input)
        return self.async_create_entry(title="", data={})

    async def async_step_humidity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Humidity with control — edit the single target humidity."""
        current_float = float(
            self._entry.data.get(CONF_TARGET_HUMIDITY, DEFAULT_TARGET_HUMIDITY)
        )
        if user_input is None:
            return self.async_show_form(
                step_id="humidity", data_schema=_build_humidity_schema(current_float)
            )
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_TARGET_HUMIDITY: user_input[CONF_TARGET_HUMIDITY],
            },
        )
        self._propagate_to_child(
            {CONF_TARGET_HUMIDITY: user_input[CONF_TARGET_HUMIDITY]}
        )
        return self.async_create_entry(title="", data={})

    async def async_step_sensor_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Render a placeholder — sensor-only zones have nothing local to edit."""
        if user_input is None:
            return self.async_show_form(
                step_id="sensor_only", data_schema=vol.Schema({})
            )
        return self.async_create_entry(title="", data={})

    def _propagate_to_child(self, changed: dict[str, Any]) -> None:
        """Push changed options to the spawned child entry (if any)."""
        child_id = self._entry.data.get(CONF_CHILD_ENTRY_ID)
        if child_id is None:
            return
        child = self.hass.config_entries.async_get_entry(child_id)
        if child is None:
            return
        self.hass.config_entries.async_update_entry(
            child, options={**child.options, **changed}
        )
