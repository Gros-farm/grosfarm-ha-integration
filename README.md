# Grosfarm — интеграция для Home Assistant

[![Validate](https://github.com/Gros-farm/grosfarm-ha-integration/actions/workflows/validate.yml/badge.svg)](https://github.com/Gros-farm/grosfarm-ha-integration/actions/workflows/validate.yml)
[![Tests](https://github.com/Gros-farm/grosfarm-ha-integration/actions/workflows/test.yml/badge.svg)](https://github.com/Gros-farm/grosfarm-ha-integration/actions/workflows/test.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Интеграция для контроллеров **теплиц и гроубоксов Grosfarm**. Цель — чтобы
установщик (электрик без знаний Home Assistant) поднял регуляторы в несколько
кликов через мастер настройки: **без YAML и без ручных автоматизаций.**

**Local-first:** регуляторы продолжают работать на последних принятых уставках,
даже если облако недоступно.

## Возможности

Каждый «показатель» добавляется отдельным пунктом мастера:

| Показатель | Датчик | Актуатор (опционально) | Что делает |
|---|---|---|---|
| 🌡️ **Температура** | температурный | реле обогревателя | замкнутый контур через `generic_thermostat` (гистерезис, защита реле) |
| 💧 **Влажность** | влажности | увлажнитель / осушитель | замкнутый контур через `generic_hygrostat` |
| 💡 **Досветка** | освещённость (PPFD/lux/DLI) | реле лампы | локальный DLI-контроллер: off / natural_supplement / indoor_supplement / indoor_continuous + автокалибровка лампы |
| 📈 **Другой показатель** | любой сенсор | — | только телеметрия в облако (CO₂, влага почвы, EC, pH, …) |
| ☁️ **Подключение к облаку** | — | — | long-lived WebSocket: уставки вниз, телеметрия вверх |

Без актуатора показатель становится **sensor-only** зоной — датчик регистрируется
для телеметрии, но локального контура управления нет.

## Установка через HACS

> Пока интеграция не добавлена в стандартный каталог HACS — установите её как
> **custom repository**.

1. HACS → ⋮ (меню справа сверху) → **Custom repositories**.
2. Repository: `https://github.com/Gros-farm/grosfarm-ha-integration`, Category: **Integration**. Add.
3. Найдите **Grosfarm** в списке HACS → **Download**.
4. Перезапустите Home Assistant.

### Установка вручную

Скопируйте папку `custom_components/grosfarm` в `config/custom_components/` вашего
Home Assistant и перезапустите его.

## Настройка

**Settings → Devices & Services → + ADD INTEGRATION → Grosfarm.**

Откроется меню выбора показателя. Выберите тип, заполните одну форму (имя, зона/
area, датчик, при необходимости — реле) — зона создастся сразу. Уставки по фазам
роста присылает облако Grosfarm; до его подключения сидит один разумный дефолт
(например, температура home = 22 °C), чтобы сущность была сразу рабочей.

Добавляйте столько показателей, сколько нужно — каждый отдельным запуском мастера.

### Облако

Пункт **«Подключение к облаку»** связывает этот Home Assistant с учётной записью
Grosfarm. Подключение держит постоянный WebSocket-канал: облако шлёт уставки и
режимы (по культуре/стадии роста), а интеграция отправляет показания датчиков и
runtime-статусы. Если облако офлайн — интеграция уходит в **автономный режим**
(локальные контуры работают на последних значениях) и сама переподключится.

## Поддержка

Баги и предложения — в [Issues](https://github.com/Gros-farm/grosfarm-ha-integration/issues).

## Лицензия

Proprietary, Grosfarm. См. [LICENSE](./LICENSE).
