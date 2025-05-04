"""
────────────────────────────────────────────────────────────
 AUTOTECHNIK BOT · main.py · v4.6  (добавлен чат с ботом на базе ChatGPT)
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

# **NEW**: ChatGPT integration
import openai

load_dotenv()

# **NEW**: API-ключ ChatGPT
openai.api_key = "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"

# ─── CONFIG ──────────────────────────────────────────────
API_BASE       = "https://www.autotechnik.store/api/v1"
API_V2         = "https://www.autotechnik.store/api/v2"
API_TOKEN      = "d579a8bdade5445c3683a0bb9526b657de79de53"
BOT_TOKEN      = os.getenv("TG_BOT_TOKEN")
CHECK_INTERVAL  = 120
REMIND_INTERVAL = 120  # уведомления о новом чате — каждые 2 минуты
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
bot_chat_mode = {}               # telegram_id → bool: режим «чат с ботом»

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
    cur.execute(
        "SELECT telegram_id FROM managers WHERE manager_login=?",
        (login,)
    )
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
            ["📚 Каталоги товаров"],
            ["Чат с ботом"]
        ],
        resize_keyboard=True
    )

def kb_manager():
    return ReplyKeyboardMarkup(
        [["🗂 Активные чаты"], ["👥 Мои клиенты"]],
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
            return base + f"{clean(name)} — {rub(price)}{addr_line}\n📅 Ожидайте, ваш заказ готовится."
    if list_mode and st.lower() in EXCLUDED:
        return None
    return base + f"🛒 {clean(name)} — {rub(price)}\n📌 Статус: {status}{addr_line}"

# ─── CATALOGS DATA & HANDLERS ──────────────────────────
CATALOG_SECTIONS = {
    # … ваш словарь без изменений …
}

async def h_catalogs(u: Update, _):
    # … реализация без изменений …
    pass

async def h_catalog_section(cbq: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # … реализация без изменений …
    pass

# ─── /start ────────────────────────────────────────────
async def h_start(u: Update, _):
    uid = u.effective_user.id
    cur.execute("SELECT 1 FROM managers WHERE telegram_id=?", (uid,))
    if cur.fetchone():
        await u.message.reply_text(
            "👋 Вы вошли как *менеджер*.",
            parse_mode="Markdown",
            reply_markup=kb_manager()
        )
    else:
        await u.message.reply_text(
            "👋 *Нужна авторизация!*\n📱 Отправьте номер.",
            parse_mode="Markdown",
            reply_markup=kb_start()
        )

# ─── /manager or /reg1664 ────────────────────────────
async def h_mgr_reg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args:
        return await u.message.reply_text(
            "Формат: `/reg1664 <логин>`", parse_mode="Markdown"
        )
    login = c.args[0]
    cur.execute("INSERT OR REPLACE INTO managers VALUES(?,?)", (login, u.effective_user.id))
    conn.commit()
    await u.message.reply_text("✅ Зарегистрированы как менеджер!", reply_markup=kb_manager())

# ─── Contact → client auth ─────────────────────────────
async def h_contact(u: Update, _):
    phone = normalize(u.message.contact.phone_number)
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"{API_BASE}/customers/?token={API_TOKEN}")
        custs = r.json().get("result", [])
    except Exception as exc:
        return await u.message.reply_text(f"❌ API недоступно: {exc}")

    cust = next((x for x in custs if normalize(x.get("phone")) == phone), None)
    if not cust:
        return await u.message.reply_text("❌ Номер не найден.")

    cid  = cust.get("id") or cust.get("customerID")
    mlog = cust.get("managerLogin") or ""
    cur.execute(
        "INSERT OR REPLACE INTO users (telegram_id,phone,customer_id,manager_login,last_statuses) VALUES(?,?,?,?,?)",
        (u.effective_user.id, phone, cid, mlog, json.dumps({}))
    )
    conn.commit()
    await u.message.reply_text("✅ Авторизация успешна!", reply_markup=kb_client())

# ─── Bonus card ────────────────────────────────────────
async def h_card(u: Update, _):
    # … без изменений …
    pass

# ─── My orders ─────────────────────────────────────────
async def h_my_orders(u: Update, _):
    # … без изменений …
    pass

# ─── Chat request to manager ───────────────────────────
async def h_chat_request(u: Update, _):
    # … без изменений …
    pass

# ─── Chat request to bot ───────────────────────────────
async def h_chat_bot_request(u: Update, _):
    uid = u.effective_user.id
    cur.execute("SELECT customer_id FROM users WHERE telegram_id=?", (uid,))
    r = cur.fetchone()
    if not r:
        return await u.message.reply_text("Сначала авторизуйтесь.", reply_markup=kb_start())
    cid = r[0]
    client_chat[uid]   = cid
    bot_chat_mode[uid] = True
    await u.message.reply_text(
        "✅ Вы начали чат с ботом. Задавайте вопросы об автозапчастях, ремонте и оформлении заказов.",
        reply_markup=ReplyKeyboardMarkup([["🛑 Завершить чат"]], resize_keyboard=True)
    )

# ─── MANAGER LISTS ────────────────────────────────────
async def _send_mgr_list(u: Update, *, active=False):
    # … без изменений …
    pass

async def h_btn_active(u: Update, _):  await _send_mgr_list(u, active=True)
async def h_btn_clients(u: Update, _): await _send_mgr_list(u, active=False)

# ─── CALLBACKS (open/close/history) ────────────────────
async def h_cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cb = upd.callback_query
    await cb.answer()
    data, uid = cb.data, cb.from_user.id

    if data == "cli_close":
        bot_chat_mode.pop(uid, None)
        await _close_common(uid, ctx, from_manager=False)
        return

    if data.startswith("open:") or data in ("mgr_close", "mgr_history"):
        # … без изменений …
        pass

async def _close_common(uid, ctx, *, from_manager):
    # … без изменений …
    pass

# ─── Ловим «🛑 Завершить чат» ───────────────────────────
async def h_cli_close(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    bot_chat_mode.pop(uid, None)
    await _close_common(uid, ctx, from_manager=False)

# ─── TEXT HANDLERS ─────────────────────────────────────
async def h_text_manager(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = manager_chat.get(u.effective_user.id)
    if not cid:
        return
    cur.execute("SELECT telegram_id FROM users WHERE customer_id=?", (cid,))
    r = cur.fetchone()
    if not r:
        return
    tgt = r[0]
    txt = u.message.text
    history[cid].append(("Менеджер", txt))
    await ctx.bot.send_message(tgt, f"👤 Менеджер: {txt}")

async def h_text_client(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    text = u.message.text

    # закрытие чата с ботом
    if bot_chat_mode.get(uid) and text == "🛑 Завершить чат":
        bot_chat_mode.pop(uid, None)
        await u.message.reply_text("🛑 Чат с ботом завершён.", reply_markup=kb_client())
        return

    # режим «Чат с ботом»
    if bot_chat_mode.get(uid):
        resp = await openai.ChatCompletion.acreate(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content":
                 "Ты — ассистент автосервиса Autotechnik. Отвечай только на вопросы об автозапчастях, автомобилях, ремонте и оформлении заказов. Если вопрос вне этой области — вежливо отказать."},
                {"role": "user", "content": text}
            ],
            functions=[
                {
                    "name": "search_catalog",
                    "description": "Поиск деталей по артикулу и бренду",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code":  {"type":"string"},
                            "brand": {"type":"string"},
                            "token": {"type":"string"}
                        },
                        "required": ["code","brand","token"]
                    }
                },
                {
                    "name": "create_order",
                    "description": "Оформление нового заказа",
                    "parameters": {
                        "type":"object",
                        "properties": {
                            "customerID": {"type":"integer"},
                            "positions":  {"type":"array","items":{
                                               "type":"object",
                                               "properties":{
                                                   "brand":  {"type":"string"},
                                                   "article":{"type":"string"},
                                                   "amount": {"type":"integer"},
                                                   "price":  {"type":"number"}
                                               },
                                               "required":["brand","article","amount"]
                                           }},
                            "token":      {"type":"string"}
                        },
                        "required":["customerID","positions","token"]
                    }
                }
            ],
            function_call="auto"
        )
        msg = resp.choices[0].message

        if msg.get("function_call"):
            fn, args = msg.function_call.name, json.loads(msg.function_call.arguments)

            if fn == "search_catalog":
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"{API_BASE}/search/items/",
                        params={
                            "article": args["code"],
                            "brand":   args["brand"],
                            "token":   args["token"]
                        }
                    )
                result = r.json()

            elif fn == "create_order":
                async with httpx.AsyncClient() as client:
                    r = await client.post(
                        f"{API_BASE}/customers/{args['customerID']}/orders/",
                        json={"positions": args["positions"]},
                        params={"token": args["token"]}
                    )
                result = r.json()

            else:
                result = {"error": f"Unknown function {fn}"}

            follow = await openai.ChatCompletion.acreate(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system","content":"Сформируй ответ пользователю на основе данных функции."},
                    {"role":"assistant","function_call": msg.function_call},
                    {"role":"function","name": fn, "content": json.dumps(result)}
                ]
            )
            return await u.message.reply_text(follow.choices[0].message.content)

        return await u.message.reply_text(msg.content)

    # обычный клиент→менеджер чат
    cid = client_chat.get(uid)
    if not cid:
        return
    mlog = chat_manager.get(cid)
    mgr  = manager_tid(mlog) if mlog else None
    history[cid].append((u.effective_user.full_name, text))
    if mgr and manager_chat.get(mgr) == cid:
        await ctx.bot.send_message(mgr, f"👤 {u.effective_user.full_name}: {text}")
    else:
        if mgr:
            unread[mgr].add(cid)

# ─── BACKGROUND TASKS ─────────────────────────────────
async def remind_unread():
    for mgr_tid, cids in unread.items():
        for cid in list(cids):
            cur.execute("SELECT phone FROM users WHERE customer_id=?", (cid,))
            row = cur.fetchone()
            phone = row[0] if row else "—"
            text = f"🔔 Новый чат от {phone}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶ Подключиться", callback_data=f"open:{cid}")]])
            try:
                await app.bot.send_message(mgr_tid, text, reply_markup=kb)
            except:
                pass

def stable_ref(p):
    return (
        p.get("reference")
        or p.get("ref")
        or p.get("positionID")
        or p.get("id")
        or f"{p.get('article')}_{p.get('brand')}"
    )

async def check_once():
    cur.execute("SELECT telegram_id,customer_id,last_statuses FROM users")
    for tid, cid, last_json in cur.fetchall():
        try:
            async with httpx.AsyncClient(timeout=25) as cl:
                r = await cl.get(f"{API_BASE}/customers/{cid}/orders/?token={API_TOKEN}&withPositions=1")
            r.raise_for_status()
            orders = r.json().get("result", [])
        except:
            continue
        try:
            old = json.loads(last_json) if last_json else {}
        except:
            old = {}
        first_run = len(old) == 0
        now = {}
        to_send = []
        for o in orders:
            oid  = o.get("orderNumber") or o.get("id")
            addr = o.get("deliveryOrderAddress") or ""
            for p in o.get("positions", []):
                key  = f"{oid}__{stable_ref(p)}"
                stat = clean(p.get("statusName"))
                now[key] = stat
                if not first_run and old.get(key) != stat:
                    msg = order_message(oid, p.get("description"), p.get("price") or p.get("sum"), stat, addr=addr)
                    if msg:
                        to_send.append(msg)
        for m in to_send:
            try:
                await app.bot.send_message(tid, m, parse_mode="Markdown")
            except:
                pass
        cur.execute("UPDATE users SET last_statuses = ? WHERE telegram_id = ?", (json.dumps(now, ensure_ascii=False), tid))
    conn.commit()

# ─── MAIN ──────────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN не задан!")
    req = HTTPXRequest(connect_timeout=10, read_timeout=30)
    global app
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    # handlers
    app.add_handler(CommandHandler("start", h_start))
    app.add_handler(CommandHandler(["manager", "reg1664"], h_mgr_reg))
    app.add_handler(CommandHandler("stop", lambda u,c: _close_common(u.effective_user.id, c, from_manager=False)))
    app.add_handler(MessageHandler(filters.CONTACT, h_contact))
    app.add_handler(MessageHandler(filters.Regex("^🎁"), h_card))
    app.add_handler(MessageHandler(filters.Regex("^📋"), h_my_orders))
    app.add_handler(MessageHandler(filters.Regex("^💬"), h_chat_request))
    app.add_handler(MessageHandler(filters.Regex("^Чат с ботом$"), h_chat_bot_request))
    app.add_handler(MessageHandler(filters.Regex("^🗂"), h_btn_active))
    app.add_handler(MessageHandler(filters.Regex("^👥"), h_btn_clients))
    app.add_handler(MessageHandler(filters.Regex("^📚"), h_catalogs))
    app.add_handler(CallbackQueryHandler(h_catalog_section, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(h_cb))

    # текстовые хэндлеры
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_text_manager), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_text_client),  group=1)
    app.add_handler(MessageHandler(filters.Regex(r"^🛑 Завершить чат$"), h_cli_close), group=2)

    # scheduler
    sch = AsyncIOScheduler()
    sch.add_job(check_once,    "interval", seconds=CHECK_INTERVAL)
    sch.add_job(remind_unread, "interval", seconds=REMIND_INTERVAL)
    sch.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("✅ Bot started!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
