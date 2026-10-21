"""Мини-кошелёк путешественника в Telegram.

Как устроен диалог: у pytelegrambotapi нет готовой машины состояний, как у aiogram.
Словарь state помнит, на каком шаге каждый пользователь. Так кнопки меню не ломают
сценарий «следующий ответ», в отличие от register_next_step_handler.

Число вне сценария — это трата в валюте страны пребывания.
Курс для траты берётся из путешествия (тот, что подтвердили или ввели вручную).
API нужен, чтобы проверить валюты, показать курс и конвертировать стартовую сумму.
"""

import logging
import re

from telebot import TeleBot, types

import current_api
import database
from config import API_KEY, BOT_TOKEN
from countries import normalize, resolve_place
from current_api import ApiError

log = logging.getLogger(__name__)

STEP_FROM = "from"
STEP_TO = "to"
STEP_RATE = "rate"
STEP_MANUAL = "manual"
STEP_AMOUNT = "amount"
STEP_EXPENSE = "expense"
STEP_SET_RATE = "set_rate"

# user_id -> {"step": ..., "draft": {...}} или {"step": ..., "expense": {...}}
state: dict[int, dict] = {}

MAIN_BUTTONS = (
    ("Создать новое путешествие", "menu:new"),
    ("Мои путешествия", "menu:trips"),
    ("Баланс", "menu:balance"),
    ("История расходов", "menu:history"),
    ("Изменить курс", "menu:rate"),
)

WELCOME = (
    "Привет! Я мини-кошелёк для поездки.\n\n"
    "Создай путешествие: откуда едешь и куда. Я проверю валюты на exchangerate.host "
    "и покажу курс. Если в обменнике курс другой — поставишь свой.\n\n"
    "Дальше присылай числом траты в валюте страны, где ты сейчас. "
    "Я пересчитаю их в домашнюю валюту и, если подтвердишь, вычту из баланса.\n\n"
    "С чего начнём?"
)

_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")
_YES = {"да", "yes", "ок", "ok", "подходит", "ага"}
_NO = {"нет", "no", "не подходит"}


def main_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    for title, data in MAIN_BUTTONS:
        keyboard.add(types.InlineKeyboardButton(title, callback_data=data))
    return keyboard


def yes_no_keyboard(prefix: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("✅ Да", callback_data=f"{prefix}:yes"),
        types.InlineKeyboardButton("❌ Нет", callback_data=f"{prefix}:no"),
    )
    return keyboard


def money(value: float) -> str:
    """10 720 или 10 720,5. Пробел — тысячи, запятая — дробная часть, как в русской записи."""
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.2f}".rstrip("0").rstrip(".")
    return sign + text.replace(",", " ").replace(".", ",")


def fmt_rate(value: float) -> str:
    if abs(value) >= 1:
        text = f"{value:,.4f}".rstrip("0").rstrip(".")
    else:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def fmt_when(value: str) -> str:
    # В базе время лежит как 2026-10-22 00:19, в чате удобнее 22.10.2026 00:19.
    parts = value.split(" ")
    if len(parts) != 2 or parts[0].count("-") != 2:
        return value
    year, month, day = parts[0].split("-")
    return f"{day}.{month}.{year} {parts[1]}"


def parse_amount(text: str) -> float | None:
    cleaned = text.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not _AMOUNT_RE.fullmatch(cleaned):
        return None
    value = float(cleaned)
    if value <= 0 or value > 1_000_000_000_000:
        return None
    return value


def trip_title(trip: dict) -> str:
    return f"{trip['from_country']} → {trip['to_country']}"


def balance_text(trip: dict) -> str:
    return (
        f"{trip_title(trip)}\n"
        f"Курс: 1 {trip['dest_currency']} = {fmt_rate(trip['rate'])} {trip['home_currency']}\n"
        f"Остаток: {money(trip['dest_balance'])} {trip['dest_currency']} = "
        f"{money(trip['home_balance'])} {trip['home_currency']}"
    )


def pair_line(amount: float, dest: str, home_amount: float, home: str) -> str:
    return f"{money(amount)} {dest} = {money(home_amount)} {home}"


def register_handlers(bot: TeleBot) -> None:
    @bot.message_handler(commands=["start"])
    def cmd_start(message: types.Message) -> None:
        state.pop(message.from_user.id, None)
        database.ensure_user(message.from_user.id)
        trip = database.get_active_trip(message.from_user.id)
        text = WELCOME
        if trip:
            text += "\n\nСейчас активно:\n" + balance_text(trip)
        bot.send_message(message.chat.id, text, reply_markup=main_menu())

    @bot.message_handler(commands=["newtrip"])
    def cmd_newtrip(message: types.Message) -> None:
        start_new_trip(bot, message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["switch"])
    def cmd_switch(message: types.Message) -> None:
        state.pop(message.from_user.id, None)
        show_trips(bot, message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["balance"])
    def cmd_balance(message: types.Message) -> None:
        state.pop(message.from_user.id, None)
        show_balance(bot, message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["history"])
    def cmd_history(message: types.Message) -> None:
        state.pop(message.from_user.id, None)
        show_history(bot, message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["setrate"])
    def cmd_setrate(message: types.Message) -> None:
        start_set_rate(bot, message.chat.id, message.from_user.id)

    @bot.message_handler(func=lambda message: True, content_types=["text"])
    def on_text(message: types.Message) -> None:
        try:
            handle_text(bot, message)
        except Exception:
            log.exception("Не обработалось текстовое сообщение")
            bot.send_message(
                message.chat.id,
                "Не получилось обработать сообщение. Попробуй ещё раз или открой /start.",
                reply_markup=main_menu(),
            )

    @bot.message_handler(
        content_types=["photo", "sticker", "voice", "document", "audio", "video", "location"]
    )
    def on_other(message: types.Message) -> None:
        bot.send_message(
            message.chat.id,
            "Я понимаю числа и кнопки меню. Пришли сумму числом, например 250.",
            reply_markup=main_menu(),
        )

    @bot.callback_query_handler(func=lambda call: True)
    def on_callback(call: types.CallbackQuery) -> None:
        # Отвечаем сразу: у Telegram около 10 секунд на callback, а запрос курса может быть дольше.
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            log.warning("Не удалось ответить на нажатие кнопки")
        if call.message is None or call.from_user is None:
            return
        try:
            handle_callback(bot, call)
        except Exception:
            log.exception("Не обработалось нажатие кнопки")
            bot.send_message(
                call.message.chat.id,
                "Не получилось выполнить действие. Попробуй ещё раз.",
                reply_markup=main_menu(),
            )


def handle_text(bot: TeleBot, message: types.Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    database.ensure_user(user_id)
    text = (message.text or "").strip()
    if not text:
        return

    current = state.get(user_id)
    if current:
        step = current.get("step")
        if step == STEP_FROM:
            take_from_country(bot, chat_id, user_id, text)
        elif step == STEP_TO:
            take_to_country(bot, chat_id, user_id, text)
        elif step == STEP_RATE:
            take_rate_answer(bot, chat_id, user_id, text)
        elif step == STEP_MANUAL:
            take_manual_rate(bot, chat_id, user_id, text)
        elif step == STEP_AMOUNT:
            take_start_amount(bot, chat_id, user_id, text)
        elif step == STEP_SET_RATE:
            take_new_rate(bot, chat_id, user_id, text)
        elif step == STEP_EXPENSE:
            take_expense_word(bot, chat_id, user_id, text)
        return

    if text.startswith("/"):
        bot.send_message(chat_id, "Такой команды нет. Меню открывается через /start.", reply_markup=main_menu())
        return

    amount = parse_amount(text)
    if amount is not None:
        offer_expense(bot, chat_id, user_id, amount)
        return

    bot.send_message(
        chat_id,
        "Не похоже на сумму. Пришли число, например 350, или выбери пункт меню.\n"
        "Запятая и точка — это дробная часть: 12,5 и 12.5 одно и то же. Тысячи пиши через пробел.",
        reply_markup=main_menu(),
    )


def handle_callback(bot: TeleBot, call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data or ""
    database.ensure_user(user_id)

    if data == "menu:new":
        start_new_trip(bot, chat_id, user_id)
    elif data == "menu:trips":
        state.pop(user_id, None)
        show_trips(bot, chat_id, user_id)
    elif data == "menu:balance":
        state.pop(user_id, None)
        show_balance(bot, chat_id, user_id)
    elif data == "menu:history":
        state.pop(user_id, None)
        show_history(bot, chat_id, user_id)
    elif data == "menu:rate":
        start_set_rate(bot, chat_id, user_id)
    elif data.startswith("switch:"):
        state.pop(user_id, None)
        switch_trip(bot, chat_id, user_id, data.split(":", 1)[1])
    elif data == "rate:yes":
        accept_api_rate(bot, chat_id, user_id)
    elif data == "rate:no":
        ask_manual_rate(bot, chat_id, user_id)
    elif data == "rate:api":
        apply_fresh_api_rate(bot, chat_id, user_id)
    elif data == "exp:yes":
        confirm_expense(bot, chat_id, user_id)
    elif data == "exp:no":
        decline_expense(bot, chat_id, user_id)
    else:
        bot.send_message(chat_id, "Эта кнопка уже не работает. Открой меню через /start.", reply_markup=main_menu())


def start_new_trip(bot: TeleBot, chat_id: int, user_id: int) -> None:
    database.ensure_user(user_id)
    state[user_id] = {"step": STEP_FROM, "draft": {}}
    bot.send_message(
        chat_id,
        "Новое путешествие.\n\n"
        "Откуда едешь? Напиши страну или код валюты.\n"
        "Например: Россия или RUB.\n\n"
        "Пока поездка не сохранена. Отменить можно командой /start.",
        reply_markup=main_menu(),
    )


def take_from_country(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    if len(text) > 60:
        bot.send_message(chat_id, "Слишком длинно. Напиши страну или код валюты, например Россия.")
        return
    place = resolve_place(text)
    if place is None:
        bot.send_message(
            chat_id,
            "Не понял страну. Напиши, например, «Россия» или код валюты RUB. "
            "Если страны нет в моём списке — сразу трёхбуквенный код.",
        )
        return
    title, currency = place
    draft = state[user_id]["draft"]
    draft["from_country"] = title
    draft["home_currency"] = currency
    state[user_id]["step"] = STEP_TO
    bot.send_message(
        chat_id,
        f"Домашняя валюта: {currency} ({title}).\n\n"
        "Куда едешь? Например: Китай или CNY.",
    )


def take_to_country(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    if len(text) > 60:
        bot.send_message(chat_id, "Слишком длинно. Напиши страну или код валюты, например Китай.")
        return
    place = resolve_place(text)
    if place is None:
        bot.send_message(
            chat_id,
            "Не понял страну назначения. Напиши, например, «Китай» или код CNY.",
        )
        return
    title, currency = place
    draft = state[user_id]["draft"]
    draft["to_country"] = title
    draft["dest_currency"] = currency
    ask_api_rate(bot, chat_id, user_id)


def ask_api_rate(bot: TeleBot, chat_id: int, user_id: int) -> None:
    draft = state[user_id]["draft"]
    home = draft["home_currency"]
    dest = draft["dest_currency"]
    bot.send_chat_action(chat_id, "typing")
    bot.send_message(chat_id, f"Проверяю {home} и {dest} на exchangerate.host…")

    if home == dest:
        try:
            current_api.convert(home, dest, 1)
        except ApiError as exc:
            state[user_id]["step"] = STEP_TO
            bot.send_message(
                chat_id,
                f"{exc}\n\nПришли страну назначения ещё раз или код валюты из трёх букв.",
            )
            return
        draft["rate"] = 1.0
        draft["rate_source"] = "api"
        state[user_id]["step"] = STEP_AMOUNT
        bot.send_message(
            chat_id,
            f"Обе страны в валюте {home}, сервис её подтвердил, курс 1 к 1.\n\n" + _amount_prompt(draft),
        )
        return

    try:
        # 1 единица валюты пребывания в домашней валюте: 1 CNY = ? RUB.
        rate = current_api.convert(dest, home, 1)
    except ApiError as exc:
        state[user_id]["step"] = STEP_TO
        bot.send_message(
            chat_id,
            f"{exc}\n\n"
            "Пришли страну назначения ещё раз. "
            "Если ошибка в стране отправления — начни заново через «Создать новое путешествие».",
        )
        return

    draft["rate"] = rate
    draft["api_rate"] = rate
    draft["rate_source"] = "api"
    state[user_id]["step"] = STEP_RATE
    bot.send_message(
        chat_id,
        f"Для {draft['to_country']} беру {dest}, для {draft['from_country']} — {home}. "
        f"Обе валюты есть на exchangerate.host.\n\n"
        f"Сейчас 1 {dest} = {fmt_rate(rate)} {home}.\n\n"
        "Курс подходит?",
        reply_markup=yes_no_keyboard("rate"),
    )


def take_rate_answer(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    answer = normalize(text)
    if answer in _YES:
        accept_api_rate(bot, chat_id, user_id)
        return
    if answer in _NO:
        ask_manual_rate(bot, chat_id, user_id)
        return
    bot.send_message(chat_id, "Нажми «Да» или «Нет» под курсом. Можно и словами: да / нет.")


def accept_api_rate(bot: TeleBot, chat_id: int, user_id: int) -> None:
    current = state.get(user_id)
    if not current or current.get("step") != STEP_RATE:
        bot.send_message(
            chat_id,
            "Это подтверждение курса уже неактуально. Создай путешествие заново.",
            reply_markup=main_menu(),
        )
        return
    current["draft"]["rate"] = current["draft"]["api_rate"]
    current["draft"]["rate_source"] = "api"
    current["step"] = STEP_AMOUNT
    bot.send_message(chat_id, "Беру курс сервиса.\n\n" + _amount_prompt(current["draft"]))


def ask_manual_rate(bot: TeleBot, chat_id: int, user_id: int) -> None:
    current = state.get(user_id)
    if not current or current.get("step") not in {STEP_RATE, STEP_MANUAL}:
        bot.send_message(
            chat_id,
            "Сейчас я не жду свой курс. Открой «Создать новое путешествие» или «Изменить курс».",
            reply_markup=main_menu(),
        )
        return
    draft = current["draft"]
    current["step"] = STEP_MANUAL
    bot.send_message(
        chat_id,
        "Напиши курс обменника: сколько "
        f"{draft['home_currency']} дают за 1 {draft['dest_currency']}.\n"
        f"Например, если 1 {draft['dest_currency']} = 12,8 {draft['home_currency']}, пришли 12.8",
    )


def take_manual_rate(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    rate = parse_amount(text)
    if rate is None:
        bot.send_message(chat_id, "Нужно положительное число. Например 12.8 или 12,8.")
        return
    current = state[user_id]
    current["draft"]["rate"] = rate
    current["draft"]["rate_source"] = "manual"
    current["step"] = STEP_AMOUNT
    bot.send_message(
        chat_id,
        f"Твой курс: 1 {current['draft']['dest_currency']} = {fmt_rate(rate)} "
        f"{current['draft']['home_currency']}.\n\n" + _amount_prompt(current["draft"]),
    )


def _amount_prompt(draft: dict) -> str:
    return (
        f"Какая стартовая сумма в {draft['home_currency']}? "
        f"Я спрошу конвертацию у exchangerate.host и поставлю баланс "
        f"и в {draft['home_currency']}, и в {draft['dest_currency']}."
    )


def take_start_amount(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    amount = parse_amount(text)
    if amount is None:
        bot.send_message(chat_id, "Нужна сумма больше нуля. Например 50000.")
        return
    finish_trip(bot, chat_id, user_id, amount)


def finish_trip(bot: TeleBot, chat_id: int, user_id: int, amount: float) -> None:
    current = state.get(user_id)
    if not current or current.get("step") != STEP_AMOUNT:
        bot.send_message(chat_id, "Создание путешествия прервалось. Начни заново через меню.", reply_markup=main_menu())
        return

    draft = current["draft"]
    home = draft["home_currency"]
    dest = draft["dest_currency"]
    rate = float(draft["rate"])
    bot.send_chat_action(chat_id, "typing")

    api_dest: float | None
    note = ""
    try:
        api_dest = current_api.convert(home, dest, amount)
    except ApiError as exc:
        # Свой курс пользователь уже выбрал сам, поэтому сбой API не блокирует кошелёк.
        # Курс сервиса без запасного варианта взять неоткуда — просим прислать сумму ещё раз.
        if draft.get("rate_source") != "manual":
            bot.send_message(chat_id, f"{exc}\n\nПришли стартовую сумму ещё раз.")
            return
        api_dest = None
        note = f"\n\nСервис курсов сейчас недоступен. Баланс посчитал по твоему курсу.\n{exc}"

    if draft.get("rate_source") == "manual":
        dest_balance = round(amount / rate, 2)
        if api_dest is not None:
            note = (
                f"\n\nПо API это {money(api_dest)} {dest}. "
                f"В кошелёк записал твой курс: {money(dest_balance)} {dest}."
            )
    else:
        dest_balance = round(api_dest or 0, 2)

    if dest_balance <= 0:
        bot.send_message(chat_id, "Конвертация дала ноль. Пришли другую сумму или проверь валюты.")
        return

    trip = database.create_trip(
        telegram_id=user_id,
        from_country=draft["from_country"],
        to_country=draft["to_country"],
        home_currency=home,
        dest_currency=dest,
        rate=rate,
        home_balance=round(amount, 2),
        dest_balance=dest_balance,
    )
    state.pop(user_id, None)
    bot.send_message(
        chat_id,
        f"Путешествие «{trip_title(trip)}» создано и сделано активным.\n"
        f"Старт: {money(trip['home_balance'])} {home} = {money(trip['dest_balance'])} {dest}."
        f"{note}\n\n"
        f"{balance_text(trip)}\n\n"
        f"Дальше присылай траты в {dest}. Например: 100",
        reply_markup=main_menu(),
    )


def offer_expense(bot: TeleBot, chat_id: int, user_id: int, amount: float) -> None:
    trip = database.get_active_trip(user_id)
    if trip is None:
        bot.send_message(
            chat_id,
            "Пока некуда записать трату: нет активного путешествия. Создай его в меню.",
            reply_markup=main_menu(),
        )
        return
    dest_amount = round(amount, 2)
    home_amount = round(dest_amount * float(trip["rate"]), 2)
    state[user_id] = {
        "step": STEP_EXPENSE,
        "expense": {
            "trip_id": trip["id"],
            "dest_amount": dest_amount,
            "home_amount": home_amount,
        },
    }
    bot.send_message(
        chat_id,
        pair_line(dest_amount, trip["dest_currency"], home_amount, trip["home_currency"])
        + "\n\nУчесть как расход?",
        reply_markup=yes_no_keyboard("exp"),
    )


def take_expense_word(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    answer = normalize(text)
    if answer in _YES:
        confirm_expense(bot, chat_id, user_id)
        return
    if answer in _NO:
        decline_expense(bot, chat_id, user_id)
        return
    amount = parse_amount(text)
    if amount is not None:
        offer_expense(bot, chat_id, user_id, amount)
        return
    bot.send_message(chat_id, "Нажми «Да» или «Нет» под суммой. Или пришли другую сумму числом.")


def confirm_expense(bot: TeleBot, chat_id: int, user_id: int) -> None:
    current = state.get(user_id)
    expense = (current or {}).get("expense")
    if not current or current.get("step") != STEP_EXPENSE or not expense:
        bot.send_message(
            chat_id,
            "Это подтверждение уже неактуально. Пришли сумму ещё раз.",
            reply_markup=main_menu(),
        )
        return
    trip = database.add_expense(
        user_id,
        int(expense["trip_id"]),
        float(expense["dest_amount"]),
        float(expense["home_amount"]),
    )
    state.pop(user_id, None)
    if trip is None:
        bot.send_message(chat_id, "Не нашёл это путешествие среди твоих. Пришли сумму ещё раз.", reply_markup=main_menu())
        return
    warning = ""
    if trip["dest_balance"] < 0 or trip["home_balance"] < 0:
        warning = "\n\nБаланс ушёл в минус: стартовой суммы не хватило."
    bot.send_message(
        chat_id,
        "Записал расход: "
        + pair_line(expense["dest_amount"], trip["dest_currency"], expense["home_amount"], trip["home_currency"])
        + "\n\n"
        + balance_text(trip)
        + warning,
        reply_markup=main_menu(),
    )


def decline_expense(bot: TeleBot, chat_id: int, user_id: int) -> None:
    if not state.get(user_id) or state[user_id].get("step") != STEP_EXPENSE:
        bot.send_message(chat_id, "Подтверждать уже нечего.", reply_markup=main_menu())
        return
    state.pop(user_id, None)
    bot.send_message(chat_id, "Хорошо, не записываю.", reply_markup=main_menu())


def show_balance(bot: TeleBot, chat_id: int, user_id: int) -> None:
    database.ensure_user(user_id)
    trip = database.get_active_trip(user_id)
    if trip is None:
        bot.send_message(
            chat_id,
            "Активного путешествия нет. Создай первое кнопкой ниже.",
            reply_markup=main_menu(),
        )
        return
    bot.send_message(chat_id, balance_text(trip), reply_markup=main_menu())


def show_history(bot: TeleBot, chat_id: int, user_id: int) -> None:
    database.ensure_user(user_id)
    trip = database.get_active_trip(user_id)
    if trip is None:
        bot.send_message(chat_id, "История появится, когда будет активное путешествие.", reply_markup=main_menu())
        return
    rows = database.list_expenses(user_id, trip["id"], limit=21)
    if not rows:
        bot.send_message(
            chat_id,
            f"{trip_title(trip)}\nПока расходов нет. Пришли число — это будет трата в {trip['dest_currency']}.",
            reply_markup=main_menu(),
        )
        return
    shown = rows[:20]
    lines = [f"История расходов — {trip_title(trip)}", "Сначала свежие.", ""]
    for index, row in enumerate(shown, start=1):
        lines.append(
            f"{index}. {fmt_when(row['created_at'])} — "
            + pair_line(row["dest_amount"], trip["dest_currency"], row["home_amount"], trip["home_currency"])
        )
    if len(rows) > 20:
        lines.append("")
        lines.append("Показаны последние 20.")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=main_menu())


def show_trips(bot: TeleBot, chat_id: int, user_id: int) -> None:
    database.ensure_user(user_id)
    trips = database.list_trips(user_id)
    if not trips:
        bot.send_message(
            chat_id,
            "Пока нет ни одного путешествия. Нажми «Создать новое путешествие».",
            reply_markup=main_menu(),
        )
        return
    active = database.get_active_trip(user_id)
    active_id = active["id"] if active else None
    keyboard = types.InlineKeyboardMarkup()
    for trip in trips:
        mark = "✅ " if trip["id"] == active_id else ""
        keyboard.add(
            types.InlineKeyboardButton(
                f"{mark}{trip_title(trip)}",
                callback_data=f"switch:{trip['id']}",
            )
        )
    for title, data in MAIN_BUTTONS:
        keyboard.add(types.InlineKeyboardButton(title, callback_data=data))
    bot.send_message(
        chat_id,
        "Твои путешествия. Нажми на строку, чтобы сделать её активной. Галочка — текущая.",
        reply_markup=keyboard,
    )


def switch_trip(bot: TeleBot, chat_id: int, user_id: int, raw_id: str) -> None:
    try:
        trip_id = int(raw_id)
    except ValueError:
        bot.send_message(chat_id, "Не разобрал, какое путешествие открыть.", reply_markup=main_menu())
        return
    trip = database.set_active_trip(user_id, trip_id)
    if trip is None:
        bot.send_message(chat_id, "Это путешествие не найдено среди твоих.", reply_markup=main_menu())
        return
    bot.send_message(chat_id, "Теперь активно:\n" + balance_text(trip), reply_markup=main_menu())


def start_set_rate(bot: TeleBot, chat_id: int, user_id: int) -> None:
    database.ensure_user(user_id)
    trip = database.get_active_trip(user_id)
    if trip is None:
        state.pop(user_id, None)
        bot.send_message(
            chat_id,
            "Сначала создай путешествие — курс хранится внутри него.",
            reply_markup=main_menu(),
        )
        return
    state[user_id] = {"step": STEP_SET_RATE, "trip_id": trip["id"]}
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Взять курс с API", callback_data="rate:api"))
    for title, data in MAIN_BUTTONS:
        keyboard.add(types.InlineKeyboardButton(title, callback_data=data))
    bot.send_message(
        chat_id,
        f"{trip_title(trip)}\n"
        f"Сейчас 1 {trip['dest_currency']} = {fmt_rate(trip['rate'])} {trip['home_currency']}.\n\n"
        f"Напиши новый курс: сколько {trip['home_currency']} за 1 {trip['dest_currency']}.\n"
        "Или возьми свежий курс с API.\n\n"
        "Уже лежащие на балансе суммы не пересчитаю — новый курс только для следующих трат.",
        reply_markup=keyboard,
    )


def take_new_rate(bot: TeleBot, chat_id: int, user_id: int, text: str) -> None:
    rate = parse_amount(text)
    if rate is None:
        bot.send_message(chat_id, "Нужно положительное число. Например 12.8 — столько домашней валюты за 1 единицу на месте.")
        return
    trip_id = int(state[user_id]["trip_id"])
    trip = database.update_rate(user_id, trip_id, rate)
    state.pop(user_id, None)
    if trip is None:
        bot.send_message(chat_id, "Не нашёл активное путешествие. Открой список и выбери его.", reply_markup=main_menu())
        return
    bot.send_message(
        chat_id,
        f"Курс обновлён: 1 {trip['dest_currency']} = {fmt_rate(trip['rate'])} {trip['home_currency']}.\n"
        "Остаток на балансе не менял.\n\n" + balance_text(trip),
        reply_markup=main_menu(),
    )


def apply_fresh_api_rate(bot: TeleBot, chat_id: int, user_id: int) -> None:
    current = state.get(user_id) or {}
    trip_id = current.get("trip_id")
    trip = database.get_trip(user_id, int(trip_id)) if trip_id else database.get_active_trip(user_id)
    if trip is None:
        bot.send_message(chat_id, "Нет путешествия, для которого брать курс.", reply_markup=main_menu())
        return
    bot.send_chat_action(chat_id, "typing")
    bot.send_message(chat_id, "Запрашиваю свежий курс…")
    try:
        rate = current_api.convert(trip["dest_currency"], trip["home_currency"], 1)
    except ApiError as exc:
        bot.send_message(chat_id, str(exc), reply_markup=main_menu())
        return
    updated = database.update_rate(user_id, trip["id"], rate)
    state.pop(user_id, None)
    if updated is None:
        bot.send_message(chat_id, "Не получилось сохранить курс.", reply_markup=main_menu())
        return
    bot.send_message(
        chat_id,
        f"Курс с API: 1 {updated['dest_currency']} = {fmt_rate(updated['rate'])} {updated['home_currency']}.\n"
        "Остаток на балансе не менял.\n\n" + balance_text(updated),
        reply_markup=main_menu(),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not BOT_TOKEN:
        print("Нет BOT_TOKEN. Добавь его в .env второй строкой. Образец — файл EnvExample.")
        return
    if not API_KEY:
        print("Нет API_KEY. Без ключа exchangerate.host курс не получить. Образец — файл EnvExample.")
        return

    database.init_db()
    bot = TeleBot(BOT_TOKEN)
    register_handlers(bot)
    # Не печатаем текст ошибки Telegram: в ней бывает URL вида /bot<токен>/getMe.
    try:
        me = bot.get_me()
    except Exception:
        print("Telegram не принял токен. Проверь BOT_TOKEN в .env — его выдаёт @BotFather.")
        return

    try:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Начать и открыть меню"),
                types.BotCommand("newtrip", "Создать новое путешествие"),
                types.BotCommand("switch", "Мои путешествия"),
                types.BotCommand("balance", "Баланс"),
                types.BotCommand("history", "История расходов"),
                types.BotCommand("setrate", "Изменить курс"),
            ]
        )
    except Exception:
        log.warning("Команды в меню Telegram не зарегистрировались, бот всё равно работает")

    print(f"Бот запущен: @{me.username}. Остановить — Ctrl+C.")
    # skip_pending: сообщения, пришедшие пока бот был выключен, не станут пачкой трат.
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
