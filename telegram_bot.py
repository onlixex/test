#!/usr/bin/env python3
# coding: utf-8

import re
import json
import logging
from typing import Optional, Dict, Any

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    Contact,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------
# ЛОГИРОВАНИЕ
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger(__name__)

# ---------------------------
# КОНФИГ (подставлены твои токены/пароли)
# ---------------------------
TELEGRAM_TOKEN = "8286351251:AAHI5T5ccOxZ_CmE6mgpEXBoNkJNc5O9oj0"
POSTGRES_DSN = "postgresql://helpdesk_user:123QWE456rty@127.0.0.1:5432/helpdesk"

ZAMMAD_BASE_URL = "http://127.0.0.1:3000"
ZAMMAD_TOKEN = "i3Y4nf4txmzkp4mxKyCZrdesq0q3YcbU-8CtStaFrd87BZAeoxKSxTtohLPwvEZI"

# Zero-shot service
ZERO_SHOT_URL = "http://127.0.0.1:8000/zero-shot"

# Кнопка-клавиатура для контакта
CONTACT_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("Отправить номер", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# ---------------------------
# Postgres helpers
# ---------------------------
def get_db_conn():
    return psycopg2.connect(POSTGRES_DSN, cursor_factory=RealDictCursor)

def ensure_tables():
    """Создаём таблицы, если их нет."""
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_map (
                        telegram_id BIGINT PRIMARY KEY,
                        zammad_user_id INTEGER,
                        phone TEXT,
                        email TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        full_name TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS classification_log (
                        id SERIAL PRIMARY KEY,
                        telegram_id BIGINT,
                        ticket_id INTEGER,
                        text TEXT,
                        domain TEXT,
                        action TEXT,
                        case_label TEXT,
                        need_confirmation BOOLEAN,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                """)
            conn.commit()
        log.info("DB: tables ensured")
    except Exception:
        log.exception("DB: ensure_tables failed")

def find_user_by_telegram(telegram_id: int) -> Optional[Dict[str, Any]]:
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM user_map WHERE telegram_id=%s", (telegram_id,))
            return cur.fetchone()
    except Exception:
        log.exception("DB error in find_user_by_telegram")
        return None

def save_user_map(
    telegram_id: int,
    zammad_user_id: Optional[int] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
):
    """Сохраняем отдельно first_name/last_name и собираем full_name."""
    full_name = " ".join([p for p in [(first_name or "").strip(), (last_name or "").strip()] if p]) or None
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_map
                    (telegram_id, zammad_user_id, phone, email, first_name, last_name, full_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    zammad_user_id = EXCLUDED.zammad_user_id,
                    phone = EXCLUDED.phone,
                    email = EXCLUDED.email,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    full_name = EXCLUDED.full_name
                """,
                (telegram_id, zammad_user_id, phone, email, first_name, last_name, full_name),
            )
            conn.commit()
        log.info("Saved mapping tg=%s -> zammad_id=%s phone=%s", telegram_id, zammad_user_id, phone)
    except Exception:
        log.exception("DB error in save_user_map")

def log_classification(
    telegram_id: int,
    ticket_id: Optional[int],
    text: str,
    domain: Optional[str],
    action: Optional[str],
    case_label: Optional[str],
    need_confirmation: bool,
):
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO classification_log
                    (telegram_id, ticket_id, text, domain, action, case_label, need_confirmation)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (telegram_id, ticket_id, text, domain, action, case_label, need_confirmation),
            )
            conn.commit()
    except Exception:
        log.exception("DB error in log_classification")

# ---------------------------
# Zammad helpers — рабочая конструкция запроса телефона
# ---------------------------
def zammad_headers() -> Dict[str, str]:
    return {"Authorization": f"Token token={ZAMMAD_TOKEN}", "Content-Type": "application/json"}

def normalize_digits(phone: str) -> str:
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)

def search_user_in_zammad_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    digits = normalize_digits(phone)
    if not digits:
        return None
    try:
        url = f"{ZAMMAD_BASE_URL}/api/v1/users/search"
        params = {"limit": 1, "query": f'phone.keyword:{digits}'}
        log.info("Searching Zammad with query: %s", params["query"])
        r = requests.get(url, headers=zammad_headers(), params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            log.info("Zammad: user found for %s -> id=%s", digits, data[0].get("id"))
            return data[0]
        log.info("Zammad: no user for query %s", digits)
        return None
    except Exception:
        log.exception("Zammad search_user_in_zammad_by_phone failed")
        return None
# ---------------------------
# Zammad ticket helpers
# ---------------------------
def create_ticket_in_zammad(customer_id_or_email: Any, subject: str, body: str, group: str = "Users") -> Optional[int]:
    try:
        article_from = customer_id_or_email if isinstance(customer_id_or_email, str) else None
        customer_id = customer_id_or_email if isinstance(customer_id_or_email, int) else None

        payload = {
            "title": subject,
            "group": group,
            "customer_id": customer_id,
            "customer": article_from,
            "article": {
                "from": article_from or "no-reply@example.com",
                "subject": subject,
                "body": body,
                "type": "note",
                "internal": False,
                "sender": "Customer",
            },
        }
        url = f"{ZAMMAD_BASE_URL}/api/v1/tickets"
        r = requests.post(url, headers=zammad_headers(), json=payload, timeout=15)
        if r.status_code not in (200, 201):
            log.error("Zammad create ticket failed: %s %s", r.status_code, r.text)
            return None
        resp = r.json()
        tid = resp.get("id") or resp.get("ticket", {}).get("id")
        log.info("Created Zammad ticket id=%s", tid)
        return tid
    except Exception:
        log.exception("create_ticket_in_zammad error")
        return None

def add_tag_to_ticket(ticket_id: int, tag_name: str) -> bool:
    try:
        url = f"{ZAMMAD_BASE_URL}/api/v1/tags/add"
        payload = {"object": "Ticket", "o_id": ticket_id, "name": tag_name}
        r = requests.post(url, headers=zammad_headers(), json=payload, timeout=10)
        if r.status_code in (200, 201):
            log.info("Added tag '%s' to ticket %s", tag_name, ticket_id)
            return True
        log.warning("Failed to add tag to ticket: %s %s", r.status_code, r.text)
        return False
    except Exception:
        log.exception("add_tag_to_ticket error")
        return False

# ---------------------------
# ZERO-SHOT
# ---------------------------
def call_zero_shot(text: str, ticket_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    try:
        payload = {"text": text}
        if ticket_id:
            payload["ticket_id"] = ticket_id
        r = requests.post(ZERO_SHOT_URL, json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("call_zero_shot failed")
        return None

def _safe_label(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("label", "") or ""
    if isinstance(block, str):
        return block
    return ""

def _safe_group(zres: Optional[Dict[str, Any]], mapping_email: Optional[str]) -> str:
    try:
        if isinstance(zres, dict):
            domain = zres.get("domain")
            if isinstance(domain, dict) and domain.get("mapped_group"):
                return domain["mapped_group"]
            if zres.get("mapped_group"):
                return zres["mapped_group"]
        if mapping_email:
            em = mapping_email.lower()
            if em.endswith("@it.example.com"):
                return "IT"
            if em.endswith("@hr.example.com"):
                return "HR"
    except Exception:
        pass
    return "Support"

# ---------------------------
# TELEGRAM HANDLERS
# ---------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добрый день! Я помогу создать заявку в службу поддержки.\n"
        "Нажмите «Отправить номер», чтобы идентифицироваться.",
        reply_markup=CONTACT_KB,
    )

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        contact: Contact = update.message.contact
        tg_id = update.effective_user.id
        phone = contact.phone_number
        first_name = contact.first_name or ""
        last_name = contact.last_name or ""

        log.info("Received contact from tg=%s phone=%s first_name=%s last_name=%s",
                 tg_id, phone, first_name, last_name)

        user = search_user_in_zammad_by_phone(phone)
        if user:
            z_id = user.get("id")
            email = user.get("email") or user.get("login")
            save_user_map(
                telegram_id=tg_id,
                zammad_user_id=z_id,
                phone=phone,
                email=email,
                first_name=first_name,
                last_name=last_name,
            )
            greet = " ".join([p for p in [first_name, last_name] if p]).strip() or "пользователь"
            await update.message.reply_text(f"Здравствуйте, {greet}! Опишите, пожалуйста, проблему.")
        else:
            save_user_map(
                telegram_id=tg_id,
                zammad_user_id=None,
                phone=phone,
                email=None,
                first_name=first_name,
                last_name=last_name,
            )
            await update.message.reply_text(
                "Контакт не найден в Zammad. Укажите, пожалуйста, рабочий email (или напишите ФИО) для поиска."
            )
    except Exception:
        log.exception("contact_handler error")
        await update.message.reply_text("Ошибка при обработке контакта. Попробуйте ещё раз.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = update.effective_user.id
        text = (update.message.text or "").strip()
        log.info("Message from tg=%s: %s", tg_id, text[:200])

        pending_ticket = context.user_data.pop("awaiting_manual_tag_for_ticket", None)
        if pending_ticket:
            tag = text.strip()
            if not tag:
                await update.message.reply_text("Пустой тег — введите ещё раз.")
                context.user_data["awaiting_manual_tag_for_ticket"] = pending_ticket
                return
            ok = add_tag_to_ticket(pending_ticket, tag)
            await update.message.reply_text(f"{'Добавил' if ok else 'Не удалось добавить'} тег '{tag}' на тикет #{pending_ticket}.")
            return

        mapping = find_user_by_telegram(tg_id)
        if not mapping:
            await update.message.reply_text(
                "Я вас ещё не знаю. Нажмите «Отправить номер» или пришлите email для поиска.",
                reply_markup=CONTACT_KB,
            )
            return

        customer = mapping.get("zammad_user_id") or mapping.get("email") or mapping.get("phone")
        subject = text if len(text) <= 80 else text[:77] + "…"

        ticket_id = create_ticket_in_zammad(customer, subject, text, group=_safe_group(None, mapping.get("email")))
        if not ticket_id:
            await update.message.reply_text("Ошибка при создании тикета. Попробуйте позже.")
            return

        zres = call_zero_shot(text, ticket_id=ticket_id) or {}
        domain_label = _safe_label(zres.get("domain"))
        action_label = _safe_label(zres.get("action"))
        case_label = _safe_label(zres.get("case"))
        need_conf = bool(zres.get("need_confirmation", True))

        log_classification(
            telegram_id=tg_id,
            ticket_id=ticket_id,
            text=text,
            domain=domain_label,
            action=action_label,
            case_label=case_label,
            need_confirmation=need_conf,
        )

        if not need_conf:
            applied = []
            for blk in ("domain", "action"):
                val = zres.get(blk)
                tag = None
                if isinstance(val, dict):
                    tag = val.get("mapped_tag") or val.get("label")
                elif isinstance(val, str):
                    tag = val
                if tag and add_tag_to_ticket(ticket_id, tag):
                    applied.append(tag)
            await update.message.reply_text(
                f"Создан тикет #{ticket_id}. Классификация: {domain_label or '—'} → {action_label or '—'}. Теги: {', '.join(applied) if applied else 'нет'}"
            )
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, верно", callback_data=json.dumps({"act": "confirm", "ticket": ticket_id, "domain": domain_label, "action": action_label}))],
                [InlineKeyboardButton("Нет, выберу сам", callback_data=json.dumps({"act": "choose", "ticket": ticket_id}))]
            ])
            await update.message.reply_text(f"Я думаю: {domain_label or '—'} → {action_label or '—'}. Правильно?", reply_markup=kb)

    except Exception:
        log.exception("message_handler error")
        await update.message.reply_text("Ошибка при обработке сообщения. Попробуйте ещё раз.")

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        data = json.loads(q.data)
        act = data.get("act")
        ticket = data.get("ticket")

        if act == "confirm":
            domain = data.get("domain") or ""
            action = data.get("action") or ""
            added = []
            for tag in (domain, action):
                if tag and add_tag_to_ticket(ticket, tag):
                    added.append(tag)
            await q.edit_message_text(f"Теги навешаны: {', '.join(added) if added else 'ошибка'} на тикет #{ticket}.")

        elif act == "choose":
            context.user_data["awaiting_manual_tag_for_ticket"] = ticket
            await q.edit_message_text("Введите вручную категорию (например: почта, vpn, настройка) — я применю её к тикету.")
        else:
            await q.edit_message_text("Неизвестное действие.")

    except Exception:
        log.exception("callback_query_handler error")
        try:
            await update.callback_query.edit_message_text("Ошибка обработки действия.")
        except Exception:
            pass

# ---------------------------
# ERROR HANDLER
# ---------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception in handler", exc_info=context.error)

# ---------------------------
# RUN
# ---------------------------
def main():
    ensure_tables()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    app.add_error_handler(error_handler)

    log.info("Application started")
    app.run_polling(poll_interval=1.0)

if __name__ == "__main__":
    main()
