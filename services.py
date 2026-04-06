from datetime import datetime

import requests
from psycopg2.extras import RealDictCursor

from logger import log


def get_ha_states(url: str, token: str) -> list[dict]:
    """Запрашивает все states из HA REST API."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{url}/api/states", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def filter_cnt_entities(states: list[dict]) -> list[dict]:
    result = []
    for s in states:
        attrs = s.get("attributes", {})
        friendly = attrs.get("friendly_name", "")
        device_class = attrs.get("device_class", "")
        entity_id = attrs.get("entity_id",  "")
        if device_class == "energy" and " cnt " in friendly and "produced" not in entity_id:
            result.append(s)
    return result


def parse_energy(state: dict) -> float | None:
    try:
        return float(state["state"])
    except (KeyError, ValueError, TypeError):
        return None


def room_from_friendly_name(state: dict) -> str:
    friendly = state.get("attributes", {}).get("friendly_name", "")
    # '1.10 cnt Энергия' → '1.10'
    return friendly.split(" cnt ")[0].strip()


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
