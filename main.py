"""
ha_energy_collector.py
Сбор показаний счётчиков электроэнергии из Home Assistant → PostgreSQL.

Запуск: python ha_energy_collector.py
Рекомендуется через cron или systemd.timer каждые 5–15 минут.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # Python 3.9+

import requests
import psycopg2

from logger import log
from services import get_ha_states, filter_cnt_entities, parse_energy, get_active_tariff, \
    already_recorded, insert_reading, room_from_friendly_name
from settings import LOG_LEVEL, TIMEZONE, PG_DSN, HA_URL, HA_TOKEN


LOCAL_TZ = ZoneInfo(TIMEZONE)

# ===========================================================================
# Вспомогательные функции
# ===========================================================================


# ===========================================================================
# Основной цикл
# ===========================================================================

def main() -> None:
    log.info("=== Старт сбора показаний ===")

    conn = None
    try:
        # --- Подключение к PostgreSQL ---
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = False

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
            return

        cnt_entities = filter_cnt_entities(all_states)
        log.info("Найдено счётчиков (_cnt): %d", len(cnt_entities))

        if not cnt_entities:
            log.warning("Счётчики не найдены. Проверьте entity_id в HA.")

        # --- Обход счётчиков ---
        for state in cnt_entities:
            entity_id = state["entity_id"]
            room      = room_from_friendly_name(state)

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
    except psycopg2.Error as db_err:
        log.error("Ошибка базы данных: %s", db_err)
        if conn:
            conn.rollback()
    except Exception as exc:
        log.error("Непредвиденная ошибка: %s", exc)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            log.debug("Соединение с БД закрыто")


if __name__ == "__main__":
    main()
