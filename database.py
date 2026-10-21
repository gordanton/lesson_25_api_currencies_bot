"""Локальное хранилище кошельков. Один файл SQLite, у каждого Telegram-пользователя свои поездки.

Курс в путешествии — сколько домашней валюты дают за 1 единицу валюты страны пребывания.
Пример: Китай, курс 12.8 означает 1 CNY = 12.8 RUB.
Оба остатка хранятся отдельно. Новая трата уменьшает и юани, и рубли.
Смена курса не переписывает уже лежащие на балансе суммы: наличные в кармане от курса не меняются.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "wallet.db"


@contextmanager
def _connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                active_trip_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                from_country TEXT NOT NULL,
                to_country TEXT NOT NULL,
                home_currency TEXT NOT NULL,
                dest_currency TEXT NOT NULL,
                rate REAL NOT NULL,
                home_balance REAL NOT NULL,
                dest_balance REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                dest_amount REAL NOT NULL,
                home_amount REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES trips(id)
            );

            CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_expenses_trip ON expenses(trip_id);
            """
        )


def ensure_user(telegram_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
            (telegram_id,),
        )


def create_trip(
    telegram_id: int,
    from_country: str,
    to_country: str,
    home_currency: str,
    dest_currency: str,
    rate: float,
    home_balance: float,
    dest_balance: float,
) -> dict:
    created_at = _now()
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
            (telegram_id,),
        )
        cursor = connection.execute(
            """
            INSERT INTO trips (
                telegram_id, from_country, to_country,
                home_currency, dest_currency, rate,
                home_balance, dest_balance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                from_country,
                to_country,
                home_currency,
                dest_currency,
                rate,
                home_balance,
                dest_balance,
                created_at,
            ),
        )
        trip_id = cursor.lastrowid
        connection.execute(
            "UPDATE users SET active_trip_id = ? WHERE telegram_id = ?",
            (trip_id, telegram_id),
        )
    # Читаем после commit: внутри with транзакция ещё не закрыта, вторая связь её не увидит.
    trip = get_trip(telegram_id, trip_id)
    if trip is None:
        raise RuntimeError("Путешествие не сохранилось")
    return trip


def get_trip(telegram_id: int, trip_id: int) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM trips WHERE id = ? AND telegram_id = ?",
            (trip_id, telegram_id),
        ).fetchone()
    return dict(row) if row else None


def get_active_trip(telegram_id: int) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT trips.*
            FROM users
            JOIN trips ON trips.id = users.active_trip_id
            WHERE users.telegram_id = ? AND trips.telegram_id = ?
            """,
            (telegram_id, telegram_id),
        ).fetchone()
    return dict(row) if row else None


def list_trips(telegram_id: int) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM trips WHERE telegram_id = ? ORDER BY id DESC",
            (telegram_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_active_trip(telegram_id: int, trip_id: int) -> dict | None:
    trip = get_trip(telegram_id, trip_id)
    if trip is None:
        return None
    with _connect() as connection:
        connection.execute(
            "UPDATE users SET active_trip_id = ? WHERE telegram_id = ?",
            (trip_id, telegram_id),
        )
    return trip


def update_rate(telegram_id: int, trip_id: int, rate: float) -> dict | None:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE trips SET rate = ? WHERE id = ? AND telegram_id = ?",
            (rate, trip_id, telegram_id),
        )
        updated = cursor.rowcount == 1
    if not updated:
        return None
    return get_trip(telegram_id, trip_id)


def add_expense(
    telegram_id: int,
    trip_id: int,
    dest_amount: float,
    home_amount: float,
) -> dict | None:
    """Списывает трату с обоих остатков. Возвращает обновлённое путешествие."""
    created_at = _now()
    with _connect() as connection:
        row = connection.execute(
            "SELECT dest_balance, home_balance FROM trips WHERE id = ? AND telegram_id = ?",
            (trip_id, telegram_id),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE trips
            SET dest_balance = ?, home_balance = ?
            WHERE id = ? AND telegram_id = ?
            """,
            (
                round(row["dest_balance"] - dest_amount, 2),
                round(row["home_balance"] - home_amount, 2),
                trip_id,
                telegram_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO expenses (trip_id, telegram_id, dest_amount, home_amount, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trip_id, telegram_id, dest_amount, home_amount, created_at),
        )
    return get_trip(telegram_id, trip_id)


def list_expenses(telegram_id: int, trip_id: int, limit: int = 20) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM expenses
            WHERE trip_id = ? AND telegram_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (trip_id, telegram_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
