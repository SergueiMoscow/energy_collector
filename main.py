"""
ha_energy_collector.py
Сбор показаний счётчиков электроэнергии из Home Assistant → PostgreSQL.

Запуск: python ha_energy_collector.py
Рекомендуется через cron или systemd.timer каждые 5–15 минут.
"""

import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # Python 3.9+

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

# Загружаем переменные из .env файла (если он есть)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # если python-dotenv не установлен, продолжаем без .env

# ---------------------------------------------------------------------------
# НАСТРОЙКИ — можно вынести в .env / переменные окружения
# ---------------------------------------------------------------------------
HA_URL      = os.getenv("HA_URL",   "http://homeassistant.local:8123")
HA_TOKEN    = os.getenv("HA_TOKEN", "YOUR_LONG_LIVED_TOKEN_HERE")
PG_DSN      = os.getenv("PG_DSN",   "host=localhost dbname=energy user=postgres password=secret")
TIMEZONE    = os.getenv("TZ",       "Europe/Moscow")   # ваш часовой пояс

LOG_LEVEL   = logging.INFO
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(TIMEZONE)


# ===========================================================================
# DDL — создание таблиц при первом запуске
# ===========================================================================
DDL = """
CREATE TABLE IF NOT EXISTS tariff_rates (
    id          SERIAL PRIMARY KEY,
    valid_from  DATE         NOT NULL DEFAULT '2025-01-01',
    name        VARCHAR(32)  NOT NULL,          -- 'peak' / 'semi' / 'night'
    name_ru     VARCHAR(32)  NOT NULL,          -- 'пик' / 'полупик' / 'ночь'
    hour_start  SMALLINT     NOT NULL,          -- 0-23, включительно
    hour_end    SMALLINT     NOT NULL,          -- 0-23, включительно (до 59:59 этого часа)
    price_kwh   NUMERIC(8,4) NOT NULL DEFAULT 0 -- руб/кВт·ч (заполнить вручную)
);

COMMENT ON COLUMN tariff_rates.hour_end IS
    'Тариф действует до HH:59:59. Пример: hour_start=13, hour_end=14 → с 13:00 по 14:59:59';

CREATE TABLE IF NOT EXISTS meter_readings (
    id           BIGSERIAL PRIMARY KEY,
    recorded_at  TIMESTAMPTZ  NOT NULL,          -- время замера (UTC)
    room         VARCHAR(64)  NOT NULL,          -- 'Сруб', '2.09', '2.06', ...
    energy_kwh   NUMERIC(10,3) NOT NULL,         -- показание счётчика, кВт·ч
    tariff_name  VARCHAR(32)  NOT NULL,          -- 'peak' / 'semi' / 'night' / 'unknown'
    entity_id    VARCHAR(128) NOT NULL           -- исходный entity_id из HA
);

CREATE INDEX IF NOT EXISTS idx_readings_room_time
    ON meter_readings (room, recorded_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_room_tariff_hour
    ON meter_readings (room, tariff_name, date_trunc('hour', recorded_at AT TIME ZONE 'UTC'));
"""

SEED_TARIFFS = """
INSERT INTO tariff_rates (valid_from, name, name_ru, hour_start, hour_end, price_kwh)
VALUES
    ('2025-01-01', 'peak',  'пик',      7,  9,  0),
    ('2025-01-01', 'peak',  'пик',      17, 20, 0),
    ('2025-01-01', 'semi',  'полупик',  10, 16, 0),
    ('2025-01-01', 'semi',  'полупик',  21, 22, 0),
    ('2025-01-01', 'night', 'ночь',     23, 6,  0)
ON CONFLICT DO NOTHING;
"""
# ^^^  Замените price_kwh на реальные тарифы вашей УК/поставщика


# ===========================================================================
# Вспомогательные функции
# ===========================================================================

def get_ha_states(url: str, token: str) -> list[dict]:
    """Запрашивает все states из HA REST API."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{url}/api/states", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def filter_cnt_entities(states: list[dict]) -> list[dict]:
    """Оставляет только entity_id, заканчивающиеся на '_cnt'."""
    return [s for s in states if s.get("entity_id", "").endswith("_cnt")]


def parse_energy(state: dict) -> float | None:
    """Извлекает поле 'energy' из attributes счётчика."""
    attrs = state.get("attributes", {})
    val = attrs.get("energy")
    if val is None:
        # иногда energy лежит прямо в state (зависит от интеграции)
        try:
            val = float(state.get("state", ""))
        except (ValueError, TypeError):
            return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def room_from_entity(entity_id: str) -> str:
    """
    Преобразует entity_id в читаемое имя комнаты.
    Примеры:
        sensor.srub_cnt      → 'Сруб'
        sensor.room_206_cnt  → '2.06'
        sensor.room_209_cnt  → '2.09'
    Можно расширить маппинг под свои нужды.
    """
    mapping = {
        "sensor.srub_cnt":     "Сруб",
        "sensor.room_206_cnt": "2.06",
        "sensor.room_209_cnt": "2.09",
    }
    if entity_id in mapping:
        return mapping[entity_id]
    # Fallback: убираем суффикс _cnt и префикс sensor.
    name = entity_id.removeprefix("sensor.").removesuffix("_cnt")
    return name.replace("_", " ").title()


def get_active_tariff(conn, now_local: datetime) -> dict | None:
    """
    Возвращает актуальный тариф для заданного локального времени.
    Учитывает valid_from ≤ today и текущий час.
    Ночной тариф (hour_start > hour_end) обрабатывается отдельно.
    """
    hour = now_local.hour
    today = now_local.date()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT name, name_ru, hour_start, hour_end, price_kwh
            FROM tariff_rates
            WHERE valid_from <= %s
            ORDER BY valid_from DESC
        """, (today,))
        rows = cur.fetchall()

    for row in rows:
        h_start = row["hour_start"]
        h_end   = row["hour_end"]
        if h_start <= h_end:
            # обычный диапазон: 13-20
            if h_start <= hour <= h_end:
                return dict(row)
        else:
            # ночной переход через полночь: 23-6
            if hour >= h_start or hour <= h_end:
                return dict(row)

    return None


def already_recorded(conn, room: str, tariff_name: str, recorded_at: datetime) -> bool:
    """
    Проверяет, есть ли уже запись для данной комнаты, тарифа и часа.
    Используется частичный UNIQUE index на date_trunc('hour', ...).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM meter_readings
            WHERE room = %s
              AND tariff_name = %s
              AND date_trunc('hour', recorded_at AT TIME ZONE 'UTC')
                = date_trunc('hour', %s::timestamptz AT TIME ZONE 'UTC')
            LIMIT 1
        """, (room, tariff_name, recorded_at))
        return cur.fetchone() is not None


def insert_reading(conn, recorded_at: datetime, room: str,
                   energy_kwh: float, tariff_name: str, entity_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO meter_readings
                (recorded_at, room, energy_kwh, tariff_name, entity_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (recorded_at, room, energy_kwh, tariff_name, entity_id))
    conn.commit()
    log.info("Записано: %s | %s | %.3f кВт·ч | тариф: %s", room, recorded_at, energy_kwh, tariff_name)


# ===========================================================================
# Основной цикл
# ===========================================================================

def main() -> None:
    log.info("=== Старт сбора показаний ===")

    # --- Подключение к PostgreSQL ---
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False

    # --- Инициализация схемы ---
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(SEED_TARIFFS)
    conn.commit()
    log.info("Схема БД проверена / создана")

    # --- Текущее время ---
    now_utc   = datetime.now(tz=timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    log.info("Текущее время: %s (%s)", now_local.strftime("%Y-%m-%d %H:%M:%S %Z"), TIMEZONE)

    # --- Определяем активный тариф ---
    tariff = get_active_tariff(conn, now_local)
    if tariff is None:
        log.warning("Тариф для часа %d не найден! Запись будет с тарифом 'unknown'.", now_local.hour)
        tariff_name = "unknown"
    else:
        tariff_name = tariff["name"]
        log.info("Активный тариф: %s (%s), %d:00 – %d:59",
                 tariff["name_ru"], tariff_name,
                 tariff["hour_start"], tariff["hour_end"])

    # --- Запрос состояний из HA ---
    try:
        all_states = get_ha_states(HA_URL, HA_TOKEN)
    except requests.RequestException as exc:
        log.error("Ошибка запроса к HA: %s", exc)
        conn.close()
        return

    cnt_entities = filter_cnt_entities(all_states)
    log.info("Найдено счётчиков (_cnt): %d", len(cnt_entities))

    if not cnt_entities:
        log.warning("Счётчики не найдены. Проверьте entity_id в HA.")

    # --- Обход счётчиков ---
    for state in cnt_entities:
        entity_id = state["entity_id"]
        room      = room_from_entity(entity_id)

        energy = parse_energy(state)
        if energy is None:
            log.warning("Нет поля 'energy' у %s, пропускаем", entity_id)
            continue

        # Время последнего обновления из HA (надёжнее, чем datetime.now)
        last_updated_str = state.get("last_updated")
        try:
            # HA отдаёт ISO 8601 с Z или +00:00
            recorded_at = datetime.fromisoformat(
                last_updated_str.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError):
            recorded_at = now_utc
            log.debug("Не удалось разобрать last_updated у %s, используем now()", entity_id)

        # --- Проверка: нужно ли писать ---
        if already_recorded(conn, room, tariff_name, recorded_at):
            log.debug("Уже есть запись за этот час: %s / %s", room, tariff_name)
            continue

        # --- Вставка ---
        insert_reading(conn, recorded_at, room, energy, tariff_name, entity_id)

    log.info("=== Сбор завершён ===")
    conn.close()


if __name__ == "__main__":
    main()
