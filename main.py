"""
────────────────────────────────────────────────────────────
 AUTOTECHNIK BOT · main.py · v4.7  (ChatGPT + polling fix)
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
    "Выдано","Отказ поставщика","Отказ клиента","Возврат от покупателя"
}}

def order_message(oid, name, price, status, addr="", list_mode=False):
    st = clean(status)
    base = f"📦 *Заказ №{oid}*\n"
    addr_line = f"\n🏠 {addr}" if addr else ""
    if READY_PLAIN.fullmatch(st):
        return base + f"{clean(name)} — {rub(price)}{addr_line}\n🏬 *Готов к выдаче!*"
    m = READY_DAY.match(st)
    if m:
        day = int(m.group(1))
        note = "⚠️ *Последний день хранения!*" if day == 7 else "📅 Ваш заказ готовится."
        return base + f"{clean(name)} — {rub(price)}{addr_line}\n{note}"
    if list_mode and st.lower() in EXCLUDED:
        return None
    return base + f"🛒 {clean(name)} — {rub(price)}\n📌 Статус: {status}{addr_line}"

# ─── CATALOGS DATA ─────────────────────────────────────
CATALOG_SECTIONS = {
    "61": [
        ("Запчасти по разделам",         "https://www.autotechnik.store/d_catalog3/61/"),
        ("Запчасти для грузовой техники","https://www.autotechnik.store/d_catalog3/124/"),
        ("Силовые агрегаты",            "https://www.autotechnik.store/d_catalog3/126/"),
        # ... остальные подразделы ...
    ],
    "autocatalog": [
        ("Подбор по параметрам", "https://www.autotechnik.store/autocatalog/"),
        ("Подшипники",           "https://www.autotechnik.store/d_catalog3/94/"),
        ("Сальники",             "https://www.autotechnik.store/d_catalog3/98/"),
        ("Ремни",                "https://www.autotechnik.store/d_catalog3/97/"),
    ],
    # ... другие ключи ...
}

async def h_catalogs(u: Update, _):
    buttons = [
        [InlineKeyboardButton("1. Запчасти по разделам", callback_data="cat:61")],
        [InlineKeyboardButton("2. Подбор по параметрам", callback_data="cat:autocatalog")],
        [InlineKeyboardButton("⬅️ Назад в меню",           callback_data="back_to_client")],
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
    phone = normalize(u.message.contact.phone_number)
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"{API_BASE}/customers/?token={API_TOKEN}")
        custs = r.json().get("result", [])
    except Exception as e:
        return await u.message.reply_text(f"❌ API недоступно: {e}")
    cust = next((x for x in custs if normalize(x.get("phone")) == phone), None)
    if not cust:
        return await u.message.reply_text("❌ Номер не найден.")
    cid  = cust.get("id") or cust.get("customerID")
    mlog = cust.get("managerLogin") or ""
    cur.execute(
        "INSERT OR REPLACE INTO users VALUES(?,?,?,?,?)",
        (u.effective_user.id, phone, cid, mlog, json.dumps({}))
    )
    conn.commit()
    await u.message.reply_text("✅ Авторизация успешна!", reply_markup=kb_client())

# ─── Bonus card ────────────────────────────────────────
async def h_card(u: Update, _):
    cur.execute("SELECT phone FROM users WHERE telegram_id=?", (u.effective_user.id,))
    row = cur.fetchone()
    if not row:
        return await u.message.reply_text("Сначала авторизуйтесь.")
    code = normalize(row[0])
    url  = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={quote_plus(code)}"
    await u.message.reply_photo(url, caption=f"🎁 Бонус-карта `{code}`", parse_mode="Markdown")

# ─── My orders ─────────────────────────────────────────
async def h_my_orders(u: Update, _):
    cur.execute("SELECT customer_id FROM users WHERE telegram_id=?", (u.effective_user.id,))
    r = cur.fetchone()
    if not r:
        return await u.message.reply_text("Сначала авторизуйтесь.")
    cid = r[0]; sent = False
    try:
        async with httpx.AsyncClient(timeout=25) as cl:
            resp = await cl.get(f"{API_BASE}/customers/{cid}/orders/?token={API_TOKEN}&withPositions=1")
        orders = resp.json().get("result", [])
    except:
        return await u.message.reply_text("❌ Ошибка API")
    for o in orders:
        oid  = o.get("orderNumber") or o.get("id")
        addr = o.get("deliveryOrderAddress") or ""
        for p in o.get("positions", []):
            txt = order_message(oid, p.get("description"), p.get("price") or p.get("sum"), p.get("statusName"), addr, True)
            if txt:
                sent = True
                await u.message.reply_text(txt, parse_mode="Markdown")
    if not sent:
        await u.message.reply_text("😊 Нет активных заказов.")

# ─── Chat request to manager ───────────────────────────
async def h_chat_request(u: Update, _):
    cur.execute("SELECT customer_id,manager_login,phone FROM users WHERE telegram_id=?", (u.effective_user.id,))
    r = cur.fetchone()
    if not r:
        return await u.message.reply_text("Сначала авторизуйтесь.")
    cid, mlog, phone = r
    client_chat[u.effective_user.id] = cid
    chat_manager[cid] = mlog
    mgr = manager_tid(mlog)
    if mgr:
        unread[mgr].add(cid)
        await app.bot.send_message(mgr, f"🔔 Новый чат от {u.effective_user.full_name} ({phone})")
    await u.message.reply_text("✅ Менеджер уведомлён.", reply_markup=ReplyKeyboardRemove())
    await u.message.reply_text("Меню:", reply_markup=kb_client())

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
    await u.message.reply_text("✅ Чат с ботом активен.", reply_markup=kb_client_chat())

# ─── Manager lists ─────────────────────────────────────
async def _send_mgr_list(u: Update, *, active=False):
    uid = u.effective_user.id
    if active:
        title = "🗂 *Активные чаты:*"
        opened = [cid for mgr_tid, cid in manager_chat.items() if mgr_tid == uid]
        pending= [cid for cid in unread[uid] if cid not in opened]
        cids   = opened + pending
        if not cids:
            return await u.message.reply_text("Список пуст.")
        q = ",".join("?" for _ in cids)
        cur.execute(f"SELECT customer_id,telegram_id,phone FROM users WHERE customer_id IN ({q})", cids)
        rows = cur.fetchall()
    else:
        title="👥 *Мои клиенты:*"
        cur.execute("SELECT manager_login FROM managers WHERE telegram_id=?", (uid,))
        r=cur.fetchone()
        if not r: return
        mlog=r[0]
        cur.execute("SELECT customer_id,telegram_id,phone FROM users WHERE manager_login=?", (mlog,))
        rows=cur.fetchall()
    buttons=[]
    for cid,tid,phone in rows:
        try:
            name = (await u.get_bot().get_chat(tid)).full_name
        except:
            name = f"cid {cid}"
        label = f"{name} ({phone})"
        if active and cid in unread[uid]:
            label = "🔴 " + label
        buttons.append([InlineKeyboardButton(label, callback_data=f"open:{cid}")])
    await u.message.reply_text(title, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def h_btn_active(u: Update, _):  await _send_mgr_list(u, active=True)
async def h_btn_clients(u: Update, _): await _send_mgr_list(u, active=False)

# ─── CALLBACKS ────────────────────────────────────────
async def h_cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cb = upd.callback_query; await cb.answer()
    data, uid = cb.data, cb.from_user.id
    if data == "cli_close":
        bot_chat_mode.pop(uid, None)
        return await _close_common(uid, ctx, from_manager=False)
    if data.startswith("open:"):
        cid = int(data.split(":",1)[1])
        manager_chat[uid] = cid
        unread[uid].discard(cid)
        await ctx.bot.send_message(uid, "✅ Чат открыт.", reply_markup=ikb_mgr_chat())
        cur.execute("SELECT telegram_id FROM users WHERE customer_id=?", (cid,))
        r=cur.fetchone()
        if r:
            await ctx.bot.send_message(r[0], "💬 Менеджер подключился.", parse_mode="Markdown", reply_markup=kb_client_chat())
    elif data=="mgr_close":
        await _close_common(uid, ctx, from_manager=True)
    elif data=="mgr_history":
        cid=manager_chat.get(uid)
        msgs=history.get(cid, [])
        text="\n".join(f"*{who}:* {m}" for who,m in msgs) or "История пуста."
        await cb.edit_message_text(text, parse_mode="Markdown", reply_markup=ikb_mgr_chat())

async def _close_common(uid, ctx, *, from_manager):
    if from_manager:
        cid=manager_chat.pop(uid,None)
        if cid:
            chat_manager.pop(cid,None)
            unread[uid].discard(cid)
            cur.execute("SELECT telegram_id FROM users WHERE customer_id=?", (cid,))
            r=cur.fetchone()
            if r:
                client_chat.pop(r[0],None)
                await ctx.bot.send_message(r[0], "🛑 Чат закрыт менеджером.", reply_markup=kb_client())
        await ctx.bot.send_message(uid, "🛑 Чат закрыт.", reply_markup=kb_manager())
    else:
        cid=client_chat.pop(uid,None)
        if cid:
            mlog=chat_manager.pop(cid,None)
            mgr=manager_tid(mlog) if mlog else None
            if mgr:
                manager_chat.pop(mgr,None)
                unread[mgr].discard(cid)
                await ctx.bot.send_message(mgr, "🛑 Клиент завершил чат.", reply_markup=kb_manager())
        await ctx.bot.send_message(uid, "🛑 Чат завершён.", reply_markup=kb_client())

async def h_cli_close(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=u.effective_user.id
    bot_chat_mode.pop(uid,None)
    await _close_common(uid, ctx, from_manager=False)

# ─── TEXT HANDLERS ─────────────────────────────────────
async def h_text_manager(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = manager_chat.get(u.effective_user.id)
    if not cid:
        return
    cur.execute("SELECT telegram_id FROM users WHERE customer_id=?", (cid,))
    r=cur.fetchone()
    if not r:
        return
    tgt = r[0]
    txt = u.message.text
    history[cid].append(("Менеджер", txt))
    await ctx.bot.send_message(tgt, f"👤 Менеджер: {txt}")

async def h_text_client(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    text = u.message.text

    # закрытие бот-чата
    if bot_chat_mode.get(uid) and text == "🛑 Завершить чат":
        bot_chat_mode.pop(uid,None)
        return await u.message.reply_text("🛑 Чат с ботом завершён.", reply_markup=kb_client())

    # бот-чат
    if bot_chat_mode.get(uid):
        resp = await openai.ChatCompletion.acreate(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"Ты — ассистент автосервиса Autotechnik. Отвечай только по автозапчастям, ремонту и заказам."},
                {"role":"user","content":text}
            ]
        )
        return await u.message.reply_text(resp.choices[0].message.content)

    # клиент→менеджер
    cid = client_chat.get(uid)
    if not cid:
        return
    mlog = chat_manager.get(cid)
    mgr  = manager_tid(mlog) if mlog else None
    history[cid].append((u.effective_user.full_name, text))
    if mgr and manager_chat.get(mgr)==cid:
        await ctx.bot.send_message(mgr, f"👤 {u.effective_user.full_name}: {text}")
    elif mgr:
        unread[mgr].add(cid)

# ─── BACKGROUND TASKS ─────────────────────────────────
async def remind_unread():
    for mgr_tid, cids in unread.items():
        for cid in list(cids):
            cur.execute("SELECT phone FROM users WHERE customer_id=?", (cid,))
            phone = cur.fetchone()[0] if cur.fetchone() else "—"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶ Подключиться", callback_data=f"open:{cid}")]])
            try:
                await app.bot.send_message(mgr_tid, f"🔔 Новый чат от {phone}", reply_markup=kb)
            except:
                pass

async def check_once():
    cur.execute("SELECT telegram_id,customer_id,last_statuses FROM users")
    for tid, cid, last_json in cur.fetchall():
        try:
            async with httpx.AsyncClient(timeout=25) as cl:
                resp = await cl.get(f"{API_BASE}/customers/{cid}/orders/?token={API_TOKEN}&withPositions=1")
            resp.raise_for_status()
            orders = resp.json().get("result", [])
        except:
            continue
        try:
            old = json.loads(last_json) if last_json else {}
        except:
            old = {}
        first_run = len(old)==0
        now = {}
        to_send=[]
        for o in orders:
            oid=o.get("orderNumber") or o.get("id")
            addr=o.get("deliveryOrderAddress") or ""
            for p in o.get("positions",[]):
                key=f"{oid}__{p.get('id', p.get('article'))}"
                stat=clean(p.get("statusName"))
                now[key]=stat
                if not first_run and old.get(key)!=stat:
                    msg=order_message(oid, p.get("description"), p.get("price") or p.get("sum"), stat, addr)
                    if msg:
                        to_send.append(msg)
        for m in to_send:
            try:
                await app.bot.send_message(tid, m, parse_mode="Markdown")
            except:
                pass
        cur.execute("UPDATE users SET last_statuses=? WHERE telegram_id=?", (json.dumps(now, ensure_ascii=False), tid))
    conn.commit()

def main():
    nest_asyncio.apply()
    req = HTTPXRequest(connect_timeout=10, read_timeout=30)
    global app
    app = Application.builder()\
        .token(BOT_TOKEN)\
        .request(req)\
        .drop_pending_updates(True)\
        .build()

    # handlers
    app.add_handler(CommandHandler("start", h_start))
    app.add_handler(CommandHandler(["manager","reg1664"], h_mgr_reg))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_text_manager), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_text_client),  group=1)
    app.add_handler(MessageHandler(filters.Regex(r"^🛑 Завершить чат$"), h_cli_close), group=2)

    # scheduler
    sch = AsyncIOScheduler()
    sch.add_job(check_once,    "interval", seconds=CHECK_INTERVAL)
    sch.add_job(remind_unread, "interval", seconds=REMIND_INTERVAL)
    sch.start()

    app.run_polling()

if __name__ == "__main__":
    main()
