"""
────────────────────────────────────────────────────────────
 AUTOTECHNIK BOT · main.py · v4.8  (ChatGPT + polling fix v2)
────────────────────────────────────────────────────────────
"""

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
from dotenv import load_dotenv
import openai

load_dotenv()
openai.api_key = "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"

# ─── CONFIG ──────────────────────────────────────────────
API_BASE       = "https://www.autotechnik.store/api/v1"
API_TOKEN      = "d579a8bdade5445c3683a0bb9526b657de79de53"
BOT_TOKEN      = os.getenv("TG_BOT_TOKEN")
CHECK_INTERVAL = 120
REMIND_INTERVAL= 120
DB_PATH        = "db.sqlite3"
HISTORY_LIMIT  = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")

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
    manager_login TEXT PRIMARY KEY,
    telegram_id   INTEGER
);
""")
conn.commit()

# ─── STATE ──────────────────────────────────────────────
client_chat   = {}               # client_tid → customer_id
manager_chat  = {}               # manager_tid → customer_id
chat_manager  = {}               # customer_id → manager_login
unread        = defaultdict(set) # manager_tid → set(customer_id)
history       = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
bot_chat_mode = {}               # telegram_id → bool

# ─── HELPERS ────────────────────────────────────────────
def normalize(ph: str | None) -> str:
    return ''.join(filter(str.isdigit, ph or ''))[-10:]

def clean(t: str | None) -> str:
    return (t or "").replace('\u00A0', ' ').strip()

def rub(val) -> str:
    try:
        return f"{float(val):,.2f}".replace(',', ' ') + " ₽"
    except:
        return "—"

def manager_tid(login: str) -> int | None:
    cur.execute("SELECT telegram_id FROM managers WHERE manager_login=?", (login,))
    r = cur.fetchone()
    return r[0] if r else None

# ─── KEYBOARDS ─────────────────────────────────────────
def kb_start():
    return ReplyKeyboardMarkup([[KeyboardButton("📲 Отправить номер", request_contact=True)]], resize_keyboard=True)

def kb_client():
    return ReplyKeyboardMarkup(
        [
            ["💬 Чат с менеджером"],
            ["📋 Мои активные заказы"],
            ["🎁 Бонусная-карта"],
            ["📚 Каталоги товаров"],
            ["Чат с ботом"]
        ],
        resize_keyboard=True
    )

def kb_manager():
    return ReplyKeyboardMarkup([["🗂 Активные чаты"], ["👥 Мои клиенты"]], resize_keyboard=True)

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
    return ReplyKeyboardMarkup([["🛑 Завершить чат"]], resize_keyboard=True)

# ─── ORDER MESSAGE BUILDER ────────────────────────────
READY_PLAIN = re.compile(r"^готово?\s+к\s+выдаче$", re.I)
READY_DAY   = re.compile(r"^готово?\s+к\s+выдаче\s+(\d+)", re.I)
EXCLUDED    = {s.lower() for s in {
    "Выдано","Отказ поставщика","Отказ клиента",
    "Отказ клиента вышел срок хранения",
    "возврат от покупателя","Возврат поставщику",
    "Возврат одобрен","Возврат отклонён"
}}

def order_message(oid, name, price, status, addr="", list_mode=False):
    st = clean(status)
    base = f"📦 *Заказ №{oid}*\n"
    addr_line = f"\n🏠 Пункт выдачи: {addr}" if addr else ""
    if READY_PLAIN.fullmatch(st):
        return base + f"{clean(name)} — {rub(price)}{addr_line}\n🏬 *Готов к выдаче!*"
    m = READY_DAY.match(st)
    if m:
        day = int(m.group(1))
        if day == 7:
            return base + f"{clean(name)} — {rub(price)}{addr_line}\n⚠️ *Последний день хранения!*"
        else:
            return base + f"{clean(name)} — {rub(price)}{addr_line}\n📅 Ваш заказ готовится."
    if list_mode and st.lower() in EXCLUDED:
        return None
    return base + f"🛒 {clean(name)} — {rub(price)}\n📌 Статус: {status}{addr_line}"

# ─── CATALOGS DATA ─────────────────────────────────────
CATALOG_SECTIONS = {
    "61": [
        ("Запчасти по разделам",                "https://www.autotechnik.store/d_catalog3/61/"),
        ("Запчасти для грузовой техники",       "https://www.autotechnik.store/d_catalog3/124/"),
        ("Силовые агрегаты",                    "https://www.autotechnik.store/d_catalog3/126/"),
        ("Бачки",                               "https://www.autotechnik.store/d_catalog3/61/bachci/"),
        ("Втулки",                              "https://www.autotechnik.store/d_catalog3/61/vtulci/"),
        ("Втулки металические",                 "https://www.autotechnik.store/d_catalog3/61/vtulci-metalichescie/"),
        ("Выхлопная система",                   "https://www.autotechnik.store/d_catalog3/61/vihlopnaya-sistema/"),
        ("Заглушки / Держатели",                "https://www.autotechnik.store/d_catalog3/61/zaglushci/"),
        ("Замки",                               "https://www.autotechnik.store/d_catalog3/61/zamci/"),
        ("Запчасти двигателя",                  "https://www.autotechnik.store/d_catalog3/61/zapchasti-dvigatelya/"),
        ("Зеркала",                             "https://www.autotechnik.store/d_catalog3/61/zercala/"),
        ("Кожухи",                              "https://www.autotechnik.store/d_catalog3/61/corpusa--cojuhi/"),
        ("Краны",                               "https://www.autotechnik.store/d_catalog3/61/crani/"),
        ("Крестовины",                          "https://www.autotechnik.store/d_catalog3/61/crestovini/"),
        ("Кронштейны",                          "https://www.autotechnik.store/d_catalog3/61/cronshteini/"),
    ],
    "autocatalog": [
        ("Подбор по параметрам", "https://www.autotechnik.store/autocatalog/"),
        ("Подшипники",           "https://www.autotechnik.store/d_catalog3/94/"),
        ("Сальники",             "https://www.autotechnik.store/d_catalog3/98/"),
        ("Ремни",                "https://www.autotechnik.store/d_catalog3/97/"),
    ],
    "110": [
        ("Масла",                    "https://www.autotechnik.store/d_catalog3/110/"),
        ("Масла моторные",           "https://www.autotechnik.store/d_catalog3/110/maslo-motornoe/"),
        ("Масла трансмиссионные",    "https://www.autotechnik.store/d_catalog3/110/maslo-transmissionnoe-/"),
    ],
    "100": [
        ("Фильтра",                 "https://www.autotechnik.store/d_catalog3/100/"),
        ("Масляные фильтра",        "https://www.autotechnik.store/d_catalog3/100/maslyanie-filtra/"),
    ],
    "103": [
        ("Автохимия",               "https://www.autotechnik.store/d_catalog3/103/"),
        ("AdBlue",                  "https://www.autotechnik.store/d_catalog3/103/adblue/"),
    ],
    "42": [
        ("Лакокрасочные материалы", "https://www.autotechnik.store/d_catalog3/42/"),
    ],
    "140": [
        ("Абразивные материалы",    "https://www.autotechnik.store/d_catalog3/140/"),
    ],
    "142": [
        ("Автоаксессуары",          "https://www.autotechnik.store/d_catalog3/142/"),
    ],
    "31": [
        ("Крепёжные элементы",      "https://www.autotechnik.store/d_catalog3/31/"),
    ],
    "145": [
        ("Фаркопы",                 "https://www.autotechnik.store/d_catalog3/145/"),
    ],
    "102": [
        ("Электрооборудование",     "https://www.autotechnik.store/d_catalog3/102/"),
    ],
}

async def h_catalogs(u: Update, _):
    buttons = [
        [InlineKeyboardButton("1. Запчасти по разделам", callback_data="cat:61")],
        [InlineKeyboardButton("2. Подбор по параметрам", callback_data="cat:autocatalog")],
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="back_to_client")],
    ]
    await u.message.reply_text("📚 Выберите раздел каталога:", reply_markup=InlineKeyboardMarkup(buttons))

async def h_catalog_section(cbq: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cbq.callback_query.answer()
    data = cbq.callback_query.data
    if data == "back_to_client":
        await cbq.callback_query.message.delete()
        return await cbq.callback_query.message.reply_text("Вы вернулись в меню.", reply_markup=kb_client())
    _, key = data.split(":", 1)
    items = CATALOG_SECTIONS.get(key, [])
    buttons = [[InlineKeyboardButton(txt, url=url)] for txt, url in items]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:61")])
    await cbq.callback_query.message.edit_text("🔹 Подразделы:", reply_markup=InlineKeyboardMarkup(buttons))

# ─── /start ────────────────────────────────────────────
async def h_start(u: Update, _):
    uid = u.effective_user.id
    cur.execute("SELECT 1 FROM managers WHERE telegram_id=?", (uid,))
    if cur.fetchone():
        await u.message.reply_text("👋 Вы вошли как менеджер.", parse_mode="Markdown", reply_markup=kb_manager())
    else:
        await u.message.reply_text("👋 Авторизуйтесь:", parse_mode="Markdown", reply_markup=kb_start())

# ─── /manager or /reg1664 ────────────────────────────
async def h_mgr_reg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args:
        return await u.message.reply_text("Формат: `/reg1664 <логин>`", parse_mode="Markdown")
    login = c.args[0]
    cur.execute("INSERT OR REPLACE INTO managers VALUES(?,?)", (login, u.effective_user.id))
    conn.commit()
    await u.message.reply_text("✅ Вы – менеджер.", reply_markup=kb_manager())

# ─── Contact → client auth ─────────────────────────────
async def h_contact(u: Update, _):
    phone = normalize(u.message.contact.phone_
