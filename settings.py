import os
from dotenv import load_dotenv
import logging

load_dotenv()


# ---------------------------------------------------------------------------
# НАСТРОЙКИ — читаются из переменных окружения / .env
# ---------------------------------------------------------------------------
HA_URL      = os.getenv("HA_URL",   "http://homeassistant.local:8123")
HA_TOKEN    = os.getenv("HA_TOKEN", "YOUR_LONG_LIVED_TOKEN_HERE")
PG_DSN = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST')}:{os.getenv('POSTGRES_PORT')}/{os.getenv('POSTGRES_DB')}"
TIMEZONE    = os.getenv("TZ",       "Europe/Moscow")   # ваш часовой пояс

LOG_LEVEL   = logging.INFO
# ---------------------------------------------------------------------------
