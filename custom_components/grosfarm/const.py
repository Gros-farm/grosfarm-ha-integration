"""Constants for the Grosfarm integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "grosfarm"

# Platforms forwarded by our parent config entry. Sensor — для дашборда
# (lighting controller status, DLI today, cloud connection state, ...).
# Heating/humidifying зоны помимо этого спавнят helpers (generic_thermostat,
# generic_hygrostat) и берут оттуда climate entity. Monitoring — sensor-only.
PLATFORMS: Final[list[Platform]] = [Platform.SENSOR]

# Preset type — picked by the user on the menu step, persisted on entry.data.
CONF_PRESET_TYPE: Final = "preset_type"

PRESET_TYPE_HEATING: Final = "heating"
PRESET_TYPE_HUMIDIFYING: Final = "humidifying"
PRESET_TYPE_MONITORING: Final = "monitoring"
PRESET_TYPE_CLOUD: Final = "cloud"
PRESET_TYPE_LIGHTING: Final = "lighting"

# --- Cloud connection (singleton entry per base_url+login) ---
CONF_BASE_URL: Final = "base_url"
CONF_LOGIN: Final = "login"
CONF_PASSWORD: Final = "password"
CONF_API_KEY: Final = "api_key"
CONF_MAC_ADDRESS: Final = "mac_address"

DEFAULT_BASE_URL: Final = "http://192.168.0.197:8765"

# Раз в N секунд coordinator отправляет батч показаний сенсоров managed entries.
TELEMETRY_PUSH_INTERVAL_SECONDS: Final = 30
RECONNECT_BACKOFF_SECONDS: Final = (1, 2, 5, 10, 30)

# --- Heating preset (entry.data keys) ---
CONF_NAME: Final = "name"
CONF_TARGET_SENSOR: Final = "target_sensor"
CONF_HEATER: Final = "heater"
CONF_PRESET_TEMPS: Final = "preset_temps"

# --- Humidifying preset (entry.data keys) ---
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
CONF_HUMIDIFIER: Final = "humidifier"
CONF_TARGET_HUMIDITY: Final = "target_humidity"
# generic_hygrostat needs to know if this is a humidifier or a dehumidifier.
# We default to humidifier (Grosfarm's primary use case — greenhouses, growboxes).
CONF_HUMIDIFIER_DEVICE_CLASS: Final = "humidifier_device_class"

# --- Monitoring preset (entry.data keys) ---
CONF_SENSOR: Final = "sensor"

# HA sensor device_class → cloud indicator. По нему coordinator шлёт телеметрию,
# а sensor-платформа показывает, какой показатель снимает monitoring-зона.
DEVICE_CLASS_TO_INDICATOR: Final[dict[str, str]] = {
    "temperature": "air_temperature",
    "humidity": "air_humidity",
    "carbon_dioxide": "co2_concentration",
    "illuminance": "illuminance",
    "pressure": "atmospheric_pressure",
    "moisture": "substrate_humidity",
}

# --- Lighting preset (entry.data keys) ---
CONF_ILLUMINANCE_SENSOR: Final = "illuminance_sensor"
CONF_LIGHT: Final = "light"  # lamp actuator: light.* (dimmable) or switch.* (relay), optional
CONF_SENSOR_KIND: Final = "sensor_kind"  # "ppfd" | "lux" | "dli"
# Для kind=lux: led/hps/mh/fluorescent/incandescent/sunlight.
CONF_LAMP_TYPE: Final = "lamp_type"
CONF_LAMP_PPFD: Final = "lamp_ppfd"  # µmol/m²/s, заполняется калибровкой
CONF_TARGET_DLI: Final = "target_dli"  # mol/m²·day, пушится от cloud
# off / natural_supplement / indoor_supplement / indoor_continuous.
CONF_LIGHTING_MODE: Final = "lighting_mode"

# Sensor kind значения.
SENSOR_KIND_PPFD: Final = "ppfd"
SENSOR_KIND_LUX: Final = "lux"
SENSOR_KIND_DLI: Final = "dli"

# Lamp type значения + коэффициенты lux→PPFD (µmol/m²/s per lux).
# Значения взяты из общеупотребительной литературы (Heliospectra app notes,
# Lighting Research Center). Точность ±20% — спектр конкретной лампы разный.
# TODO: уточнить с агрономом.
LAMP_TYPE_LED: Final = "led"
LAMP_TYPE_HPS: Final = "hps"
LAMP_TYPE_MH: Final = "mh"
LAMP_TYPE_FLUORESCENT: Final = "fluorescent"
LAMP_TYPE_INCANDESCENT: Final = "incandescent"
LAMP_TYPE_SUNLIGHT: Final = "sunlight"

LAMP_TYPE_LUX_TO_PPFD: Final[dict[str, float]] = {
    LAMP_TYPE_LED: 0.015,
    LAMP_TYPE_HPS: 0.013,
    LAMP_TYPE_MH: 0.014,
    LAMP_TYPE_FLUORESCENT: 0.015,
    LAMP_TYPE_INCANDESCENT: 0.020,
    LAMP_TYPE_SUNLIGHT: 0.0185,
}

# Warm-up длительность калибровки, секунд. Зависит от типа лампы.
LAMP_TYPE_WARMUP_SECONDS: Final[dict[str, int]] = {
    LAMP_TYPE_LED: 30,
    LAMP_TYPE_HPS: 300,  # натриевая прогревается до 5 минут
    LAMP_TYPE_MH: 300,
    LAMP_TYPE_FLUORESCENT: 60,
    LAMP_TYPE_INCANDESCENT: 30,
    LAMP_TYPE_SUNLIGHT: 0,  # калибровка не имеет смысла
}

# Lighting режимы (приходят из cloud).
LIGHTING_MODE_OFF: Final = "off"
LIGHTING_MODE_NATURAL_SUPPLEMENT: Final = "natural_supplement"
LIGHTING_MODE_INDOOR_SUPPLEMENT: Final = "indoor_supplement"
LIGHTING_MODE_INDOOR_CONTINUOUS: Final = "indoor_continuous"

DEFAULT_LIGHTING_MODE: Final = LIGHTING_MODE_OFF
DEFAULT_TARGET_DLI: Final = 0.0  # mol/m²·day. Cloud присылает реальное значение.
LIGHTING_TICK_SECONDS: Final = 60
LIGHTING_CALIBRATION_SAMPLE_SECONDS: Final = 60  # окно усреднения замеров

# Form-error key для коллизии лампы (используется так же как heater_already_used).
ERROR_LIGHT_ALREADY_USED: Final = "light_already_used"

# --- Shared metadata ---
CONF_AREA_ID: Final = "area_id"
CONF_CHILD_ENTRY_ID: Final = "child_entry_id"

# Form-error keys (mapped to translatable strings under `config.error.*`).
ERROR_HEATER_ALREADY_USED: Final = "heater_already_used"
ERROR_HUMIDIFIER_ALREADY_USED: Final = "humidifier_already_used"

# Grosfarm is a SaaS — phase setpoints come down from the cloud, one phase
# at a time (current day/night). At install we DON'T ask the user to fill
# all six phases by hand: the user just picks hardware, and we seed one
# sensible default so the controlling entity is immediately usable until
# the cloud connects. Unset presets simply don't appear in the climate
# entity's preset dropdown (generic_thermostat omits presets whose
# temperature isn't configured).
PRESET_KEYS: Final[tuple[str, ...]] = (
    "away_temp",
    "eco_temp",
    "sleep_temp",
    "home_temp",
    "comfort_temp",
    "activity_temp",
)
DEFAULT_PRESET_TEMPS: Final[dict[str, float]] = {
    "home_temp": 22.0,
}
DEFAULT_TARGET_HUMIDITY: Final = 60.0

# Hardware-protection values. NOT exposed in the UI — they're applied
# directly to the spawned helper. Changing these requires a code edit,
# not a config edit.
SAFE_COLD_TOLERANCE: Final = 0.5  # °C
SAFE_HOT_TOLERANCE: Final = 0.5  # °C
SAFE_DRY_TOLERANCE: Final = 3.0  # % rh
SAFE_WET_TOLERANCE: Final = 3.0  # % rh
SAFE_MIN_CYCLE_SECONDS: Final = 180
