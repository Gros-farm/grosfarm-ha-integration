## Grosfarm

Home Assistant integration for **greenhouse / growbox controllers**. An installer
(an electrician, no Home Assistant knowledge needed) sets up the regulators in a
few clicks through the config flow — no YAML, no manual automations.

**Indicators / presets**

- 🌡️ **Temperature** — sensor + optional heater relay (closed-loop via `generic_thermostat`).
- 💧 **Humidity** — sensor + optional humidifier/dehumidifier (`generic_hygrostat`).
- 💡 **Lighting** — illuminance sensor (PPFD/lux/DLI) + optional grow-lamp relay, with a
  local DLI controller (off / natural supplement / indoor supplement / continuous).
- 📈 **Other indicator** — any sensor, telemetry only.
- ☁️ **Cloud connection** — links this Home Assistant to a Grosfarm cloud account
  over a long-lived WebSocket (setpoints down, telemetry up).

**Local-first:** regulators keep running on the last received setpoints if the
cloud is offline.
