"""Единственный источник курсов: http://api.exchangerate.host/convert.

Другие адреса и бесплатные тестовые API здесь не используются.
Эндпоинт умеет конвертировать сумму из одной валюты в другую, но не знает стран.
Страну в код валюты переводит словарь в countries.py, а этот модуль проверяет код у API.
"""

import logging
import math

import requests

from config import API_KEY

log = logging.getLogger(__name__)

CONVERT_URL = "http://api.exchangerate.host/convert"
TIMEOUT_SECONDS = 15


class ApiError(Exception):
    """Ошибка, текст которой уже можно показать пользователю. Ключа в нём нет."""


def convert(from_currency: str, to_currency: str, amount: float) -> float:
    """Конвертирует amount из from_currency в to_currency. Возвращает сумму в валюте to.

    Запрос на 1 единицу — это и есть курс: convert("CNY", "RUB", 1) отвечает,
    сколько рублей дают за 1 юань.
    """
    source = from_currency.strip().upper()
    target = to_currency.strip().upper()
    if len(source) != 3 or len(target) != 3 or not source.isalpha() or not target.isalpha():
        raise ApiError("Код валюты должен быть из трёх букв, например CNY.")
    if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000_000:
        raise ApiError("Сумма для конвертации должна быть больше нуля.")
    if not API_KEY:
        raise ApiError(
            "В .env нет ключа API_KEY. Это ключ api.exchangerate.host, "
            "в запрос он подставляется как access_key."
        )

    try:
        response = requests.get(
            CONVERT_URL,
            params={
                "access_key": API_KEY,
                "from": source,
                "to": target,
                "amount": amount,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise ApiError("Сервис курсов долго не отвечает. Попробуй ещё раз через минуту.") from None
    except requests.RequestException as exc:
        # Текст исключения requests часто содержит полный URL, а в нём access_key.
        # В лог пишем только имя класса, не str(exc).
        log.warning("Запрос к exchangerate.host не удался (%s)", type(exc).__name__)
        raise ApiError(
            "Не удалось связаться с сервисом курсов. Проверь интернет и попробуй ещё раз."
        ) from None

    try:
        payload = response.json()
    except ValueError:
        raise ApiError("Сервис курсов вернул непонятный ответ. Попробуй ещё раз чуть позже.") from None

    if not isinstance(payload, dict) or not payload.get("success"):
        raise ApiError(_friendly_error(payload if isinstance(payload, dict) else {}))

    raw_result = payload.get("result")
    try:
        result = float(raw_result)
    except (TypeError, ValueError):
        raise ApiError("Сервис курсов не прислал сумму конвертации.") from None
    if not math.isfinite(result) or result <= 0:
        raise ApiError("Сервис курсов не смог посчитать эту пару валют.")
    return result


def _friendly_error(payload: dict) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    try:
        code = int(error.get("code"))
    except (TypeError, ValueError):
        code = None

    if code in {101, 102, 103}:
        return (
            "Ключ API не подошёл. Проверь API_KEY в .env: "
            "это ключ exchangerate.host, в запрос он уходит как access_key."
        )
    if code == 104:
        return "У ключа закончился месячный лимит запросов. Попробуй позже или проверь тариф."
    if code == 105:
        return "Этому ключу не открыта конвертация. Нужен тариф, где доступен /convert."
    if code in {106, 201, 202, 403, 404}:
        return (
            "Такой валюты на exchangerate.host нет. "
            "Проверь страну или пришли код из трёх букв, например THB."
        )
    return "Сервис курсов не смог посчитать эту пару. Попробуй другую валюту или повтори позже."
