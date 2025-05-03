# AUTOTECHNIK BOT · main.py · v4.5  (full version, с добавленной 1С-интеграцией и кнопками)

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections import defaultdict, deque
from urllib.parse import quote_plus

import httpx
import nest_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ─── CONFIG ──────────────────────────────────────────────
API_BASE        = "https://www.autotechnik.store/api/v1"
API_V2          = "https://www.autotechnik.store/api/v2"
API_TOKEN       = "d579a8bdade5445c3683a0bb9526b657de79de53"
BOT_TOKEN       = os.getenv("TG_BOT_TOKEN")
CHECK_INTERVAL  = 120
REMIND_INTERVAL = 120
DB_PATH         = "db.sqlite3"
HISTORY_LIMIT   = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s"
)

# ─── DATABASE ───────────────────────────────────────────
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur  = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    phone         TEXT,
    customer_id   INTEGER,
    manager_login TEXT,
    last_statuses TEXT
);
CREATE TABLE IF NOT EXISTS managers (
    manager_login     TEXT PRIMARY KEY,
    telegram_id       INTEGER,
    unf_employee_id   INTEGER
);
""")
conn.commit()

# ─── STATE ──────────────────────────────────────────────
client_chat     = {}               # client_tid → customer_id
manager_chat    = {}               # manager_tid → customer_id
chat_manager    = {}               # customer_id → manager_login
unread          = defaultdict(set)# manager_tid → set(customer_id)
history         = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

# ─── HELPERS ────────────────────────────────────────────
def normalize(ph: str | None) -> str:
    return ''.join(filter(str.isdigit, ph or ''))[-10:]

def clean(t: str | None) -> str:
    return (t or "").replace('\u00A0', ' ').strip()

def rub(val) -> str:
    try:
        return f"{float(val):,.2f}".replace(',', ' ').replace('.00','') + " ₽"
    except:
        return "—"

def manager_tid(login: str) -> int | None:
    cur.execute("SELECT telegram_id FROM managers WHERE manager_login=?", (login,))
    r = cur.fetchone()
    return r[0] if r else None

# ─── KEYBOARDS ─────────────────────────────────────────
def kb_start():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📲 Отправить номер", request_contact=True)]],
        resize_keyboard=True
    )

def kb_client():
    return ReplyKeyboardMarkup(
        [
            ["💬 Чат с менеджером"],
            ["📋 Мои активные заказы"],
            ["🎁 Бонусная-карта"],
            ["📚 Каталоги товаров"]
        ],
        resize_keyboard=True
    )

def kb_manager():
    # <-- Здесь добавили две новые кнопки вместо «/balance» и «/advance»
    return ReplyKeyboardMarkup(
        [
            ["🗂 Активные чаты", "💰 Баланс"],
            ["👥 Мои клиенты",     "💸 Аванс"]
        ],
        resize_keyboard=True
    )

def ikb_mgr_chat():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Закрыть чат", callback_data="mgr_close"),
        InlineKeyboardButton("📜 История",    callback_data="mgr_history"),
    ]])

def ikb_cli_chat():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Завершить чат", callback_data="cli_close"),
    ]])

def kb_client_chat():
    return ReplyKeyboardMarkup(
        [["🛑 Завершить чат"]],
        resize_keyboard=True
    )

# … (весь остальной код без изменений: order_message, каталоги, чаты, handlers…)

# ─── UNF-ИНТЕГРАЦИЯ + ОБРАБОТЧИКИ КНОПОК ─────────────────
from unf_client import call_1c

# временная очередь для ввода суммы аванса
pending_advance = set()

async def h_balance(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия кнопки «💰 Баланс» — сразу вызывает 1С и показывает остатки."""
    uid = u.effective_user.id
    cur.execute("SELECT unf_employee_id FROM managers WHERE telegram_id=?", (uid,))
    row = cur.fetchone()
    if not row or not row[0]:
        return await u.message.reply_text("❌ Не привязали ваш Telegram к сотруднику 1С.")
    emp_id = row[0]
    try:
        data = await call_1c("get_balances", {})
    except Exception as e:
        return await u.message.reply_text(f"❌ Ошибка обращения в 1С: {e}")
    bal = data.get("balances", {}).get(str(emp_id), {})
    salary   = bal.get("salary", 0)
    advances = bal.get("advances", 0)
    bonus    = bal.get("bonus", 0)
    total    = salary - advances + bonus
    text = (
        f"💼 *Баланс сотрудника в 1С*\n"
        f"– Зарплата: {salary} ₽\n"
        f"– Авансы: {advances} ₽\n"
        f"– Бонусы: {bonus} ₽\n"
        f"• Итого: *{total} ₽*"
    )
    await u.message.reply_text(text, parse_mode="Markdown")

async def h_advance_button(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Нажали «💸 Аванс» — просим ввести сумму."""
    uid = u.effective_user.id
    cur.execute("SELECT unf_employee_id FROM managers WHERE telegram_id=?", (uid,))
    if not cur.fetchone() or not cur.fetchone()[0]:
        return await u.message.reply_text("❌ Не привязали ваш Telegram к сотруднику 1С.")
    pending_advance.add(uid)
    await u.message.reply_text("Введите сумму аванса в ₽ (например: 10000):")

async def h_advance_amount(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили текст от менеджера — если ждём сумму аванса, пробуем провести запрос."""
    uid  = u.effective_user.id
    txt  = u.message.text.strip()
    if uid not in pending_advance:
        return  # не наше
    if not txt.isdigit():
        return await u.message.reply_text("❌ Пожалуйста, только цифры. Введите сумму ещё раз:")
    amount = int(txt)
    pending_advance.discard(uid)

    # вытаскиваем 1С-ID
    cur.execute("SELECT unf_employee_id FROM managers WHERE telegram_id=?", (uid,))
    emp_id = cur.fetchone()[0]
    payload = {"employeeId": emp_id, "amount": amount, "type": "advance"}
    try:
        resp = await call_1c("create_payment", payload)
    except Exception as e:
        return await u.message.reply_text(f"❌ Не удалось запросить аванс: {e}")
    status = resp.get("status", "ok")
    await u.message.reply_text(f"✅ Аванс {amount} ₽ запрошен (статус: {status}).")

# Добавляем кнопки в диспетчер внутри main()
def _register_unf(app: Application, sch: AsyncIOScheduler):
    # вместо команд, теперь ловим текст от кнопок
    app.add_handler(MessageHandler(filters.Regex(r"^💰 Баланс$"), h_balance))
    app.add_handler(MessageHandler(filters.Regex(r"^💸 Аванс$"), h_advance_button))
    # и обрабатываем ввод суммы
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_advance_amount), group=0)
    # автоматическая зарплата 1-го числа
    sch.add_job(
        lambda: asyncio.create_task(call_1c("create_salary", {})),
        trigger=CronTrigger(day="1", hour="0", minute="5", timezone="Europe/Tallinn")
    )

# ─── MAIN ──────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN не задан!")
    req = HTTPXRequest(connect_timeout=10, read_timeout=30)
    global app
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    # ── здесь ваши существующие хендлеры ──
    # app.add_handler(CommandHandler("start", h_start))
    # … все остальные, как было …

    # текстовые хэндлеры и закрытие чата …
    # …

    # ➤ Интеграция с 1С-UNF + кнопки вместо команд
    sch = AsyncIOScheduler(timezone="Europe/Tallinn")
    _register_unf(app, sch)
    sch.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("✅ Bot started!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
