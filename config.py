"""Ключи из .env. Сам файл .env не создаём — он уже есть и должен остаться вне git."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

# Сначала корень проекта, потом .venv/.env.
# Сейчас ключ лежит во втором месте. load_dotenv не затирает уже найденные значения,
# поэтому корневой .env важнее.
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".venv" / ".env")

# API_KEY уходит в запрос как access_key. Второе имя — на случай другого названия в .env.
API_KEY = (os.getenv("API_KEY") or os.getenv("ACCESS_KEY") or "").strip()
BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
