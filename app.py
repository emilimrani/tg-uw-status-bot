import os, re, logging
from contextlib import suppress
from dataclasses import dataclass

import psycopg2
from psycopg2.extras import RealDictCursor

from cryptography.fernet import Fernet, InvalidToken

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters
)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BROWSERLESS_WS = os.getenv("BROWSERLESS_WS")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH") or (f"webhook/{WEBHOOK_SECRET_TOKEN or 'changeme'}")
PORT = int(os.getenv("PORT", "8000"))

DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY")

LOGIN_URL = "https://klient.gdansk.uw.gov.pl"

# ---------- LOG ----------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("bot")

# ---------- helpers ----------
def safe_markdown(text: str) -> str:
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")

async def safe_edit_or_send(query, text, reply_markup=None, parse_mode="Markdown"):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        log.warning("edit_message_text failed: %s", e)
        with suppress(Exception):
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

# ---------- Encryption ----------
def get_fernet() -> Fernet:
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY не задан.")
    return Fernet(SECRET_KEY)

def enc(text: str) -> bytes:
    return get_fernet().encrypt(text.encode("utf-8"))

def dec(blob: bytes) -> str:
    return get_fernet().decrypt(blob).decode("utf-8")

# ---------- DB ----------
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан.")
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)

def ensure_schema():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            create table if not exists users (
                telegram_id  bigint primary key,
                case_enc     bytea not null,
                pass_enc     bytea not null,
                alerts       boolean not null default false,
                created_at   timestamptz not null default now(),
                updated_at   timestamptz not null default now()
            );
        """)
        conn.commit()

@dataclass
class Creds:
    case_no: str
    password: str
    alerts: bool

def get_creds(telegram_id: int) -> Creds | None:
    with db() as conn, conn.cursor() as cur:
        cur.execute("select case_enc, pass_enc, alerts from users where telegram_id=%s", (telegram_id,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return Creds(dec(row["case_enc"]), dec(row["pass_enc"]), row["alerts"])
        except InvalidToken:
            return None

def upsert_creds(telegram_id: int, case_no: str, password: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            insert into users(telegram_id, case_enc, pass_enc)
            values(%s, %s, %s)
            on conflict (telegram_id) do update set
              case_enc=excluded.case_enc, pass_enc=excluded.pass_enc, updated_at=now();
        """, (telegram_id, enc(case_no), enc(password)))
        conn.commit()

def set_alerts(telegram_id: int, enabled: bool):
    with db() as conn, conn.cursor() as cur:
        cur.execute("update users set alerts=%s, updated_at=now() where telegram_id=%s", (enabled, telegram_id))
        conn.commit()

def delete_user(telegram_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("delete from users where telegram_id=%s", (telegram_id,))
        conn.commit()

# ---------- Scraper ----------
async def fetch_status(case_no: str, password: str) -> str:
    if not BROWSERLESS_WS:
        raise RuntimeError("BROWSERLESS_WS не задан.")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(BROWSERLESS_WS)
        except Exception as e:
            raise RuntimeError("Не удаётся подключиться к удалённому браузеру (Browserless). "
                               "Проверь `BROWSERLESS_WS` и лимиты плана. Детали: " + str(e))
        context = await browser.new_context(
            locale="pl-PL",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"),
            ignore_https_errors=True,
        )
        page = await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            with suppress(Exception):
                await page.get_by_role("button", name=re.compile("Akceptuj|Zgadzam|Accept", re.I)).click(timeout=3000)

            # fill "Numer sprawy"
            filled = False
            for locator in [
                page.get_by_label(re.compile(r"Numer sprawy", re.I)),
                page.get_by_placeholder(re.compile(r"Numer sprawy|Number of application", re.I)),
                page.locator("input[name*='numer' i], input[id*='numer' i]"),
            ]:
                try:
                    await locator.fill(case_no, timeout=2500); filled=True; break
                except Exception: pass
            if not filled: raise RuntimeError("Не нашёл поле 'Numer sprawy'.")

            # fill "Hasło"
            filled = False
            for locator in [
                page.get_by_label(re.compile(r"Hasło", re.I)),
                page.get_by_placeholder(re.compile(r"Hasło|Password", re.I)),
                page.locator("input[type='password']"),
            ]:
                try:
                    await locator.fill(password, timeout=2500); filled=True; break
                except Exception: pass
            if not filled: raise RuntimeError("Не нашёл поле 'Hasło'.")

            # click login
            clicked=False
            for locator in [
                page.get_by_role("button", name=re.compile(r"Zaloguj|Zalogować|Log in", re.I)),
                page.locator("input[type='submit']"),
                page.locator("button[type='submit']"),
            ]:
                try:
                    await locator.click(timeout=2500); clicked=True; break
                except Exception: pass
            if not clicked: raise RuntimeError("Не удалось нажать 'Zaloguj'.")

            await page.wait_for_load_state("domcontentloaded", timeout=30000)

            with suppress(Exception):
                err = await page.get_by_text(re.compile(r"(błędne|nieprawidłow).*hasł|logow", re.I)).inner_text(timeout=1200)
                if err: raise RuntimeError("Błąd logowania: sprawdź numer sprawy и hasło.")

            await page.wait_for_selector("text=Etap postępowania", timeout=15000)

            text = ""
            with suppress(Exception):
                text = await page.locator(
                    "xpath=(//*[contains(normalize-space(.),'Etap postępowania')])[1]"
                    "/following::*[self::span or self::div or self::td or self::p][1]"
                ).inner_text(timeout=2000)
            if not text:
                container = await page.locator(
                    "xpath=(//*[contains(normalize-space(.),'Etap postępowania')])[1]/ancestor::*[self::tr or self::div][1]"
                ).inner_text(timeout=2000)
                text = re.sub(r".*Etap postępowania[:\s]*", "", container, flags=re.S|re.I).strip()

            text = re.sub(r"\s+", " ", (text or "")).strip()
            if not text:
                raise RuntimeError("Не удалось найти текст статуса.")
            return text
        except PlaywrightTimeout:
            raise RuntimeError("Портал не отвечает или работает медленно. Попробуйте позже.")
        finally:
            await context.close()

# ---------- UI ----------
AWAIT_CASE, AWAIT_PASS = range(2)

def main_kb(has_creds: bool, alerts: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_creds:
        rows.append([InlineKeyboardButton("🔍 Проверить статус", callback_data="check")])
        rows.append([InlineKeyboardButton(("🔔 Уведомления: ВКЛ" if alerts else "🔕 Уведомления: ВЫКЛ"),
                                          callback_data="alerts_toggle")])
        rows.append([InlineKeyboardButton("🔑 Изменить данные", callback_data="connect")])
        rows.append([InlineKeyboardButton("🗑 Удалить данные", callback_data="unlink")])
    else:
        rows.append([InlineKeyboardButton("🔑 Подключить дело", callback_data="connect")])
    return InlineKeyboardMarkup(rows)

async def greet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    creds = get_creds(uid)
    text = (
        "Привет! Я помогу отслеживать статус твоего дела в воеводском ужонде.\n\n"
        "• Нажми «🔑 Подключить дело», чтобы один раз сохранить *Numer sprawy* и *Hasło*.\n"
        "• Потом просто жми «🔍 Проверить статус» — пришлю *Etap postępowania*.\n"
        "• Данные зашифрованы, их можно удалить одной кнопкой."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_kb(bool(creds), creds.alerts if creds else False))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "connect":
        context.user_data["connect"] = {}
        await safe_edit_or_send(query, "Введи *номер дела* (Numer sprawy):", parse_mode="Markdown")
        return AWAIT_CASE

    if data == "check":
        creds = get_creds(uid)
        if not creds:
            await safe_edit_or_send(query, "Сначала подключи дело: нажми «🔑 Подключить дело».", reply_markup=main_kb(False, False))
            return ConversationHandler.END
        await safe_edit_or_send(query, "⏳ Проверяю статус...")
        try:
            stage = await fetch_status(creds.case_no, creds.password)
            await safe_edit_or_send(query, f"📌 Etap postępowania: *{safe_markdown(stage)}*", parse_mode="Markdown",
                                    reply_markup=main_kb(True, creds.alerts))
        except Exception as e:
            await safe_edit_or_send(query, f"⚠️ {e}", reply_markup=main_kb(True, creds.alerts))
        return ConversationHandler.END

    if data == "alerts_toggle":
        creds = get_creds(uid)
        if not creds:
            await safe_edit_or_send(query, "Сначала подключи дело.", reply_markup=main_kb(False, False))
            return ConversationHandler.END
        new_state = not creds.alerts
        set_alerts(uid, new_state)
        if new_state:
            context.job_queue.run_repeating(check_job, interval=6*60*60, first=10, name=f"alert_{uid}", data=uid)
        else:
            for job in context.job_queue.get_jobs_by_name(f"alert_{uid}"):
                job.schedule_removal()
        await safe_edit_or_send(query,
            "Уведомления " + ("включены. Я буду проверять статус каждые ~6 часов." if new_state else "выключены."),
            reply_markup=main_kb(True, new_state)
        )
        return ConversationHandler.END

    if data == "unlink":
        delete_user(uid)
        for job in context.job_queue.get_jobs_by_name(f"alert_{uid}"):
            job.schedule_removal()
        await safe_edit_or_send(query, "Данные удалены. Нажми «🔑 Подключить дело», чтобы добавить заново.",
                                reply_markup=main_kb(False, False))
        return ConversationHandler.END

    return ConversationHandler.END

async def ask_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    case_no = update.message.text.strip()
    context.user_data["connect"]["case_no"] = case_no
    await update.message.reply_text("Принято. Теперь отправь *пароль* (Hasło):", parse_mode="Markdown")
    return AWAIT_PASS

async def save_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pwd = update.message.text.strip()
    case_no = context.user_data.get("connect", {}).get("case_no")
    if not case_no:
        await update.message.reply_text("Что-то пошло не так. Нажми «🔑 Подключить дело» и начни заново.",
                                        reply_markup=main_kb(False, False))
        return ConversationHandler.END
    upsert_creds(uid, case_no, pwd)
    await update.message.reply_text("Готово! Данные сохранены.\nТеперь просто жми «🔍 Проверить статус».",
                                    reply_markup=main_kb(True, False))
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, отменил.", reply_markup=main_kb(False, False))
    return ConversationHandler.END

# ---- Diagnostics ----
async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    checks = []
    checks.append(("TELEGRAM_TOKEN", "ok" if bool(TELEGRAM_TOKEN) else "missing"))
    checks.append(("BROWSERLESS_WS", "ok" if (BROWSERLESS_WS and BROWSERLESS_WS.startswith("wss://")) else "bad"))
    checks.append(("PUBLIC_URL", "ok" if (PUBLIC_URL and PUBLIC_URL.startswith("https://")) else "bad"))
    checks.append(("WEBHOOK_SECRET_TOKEN", "ok" if bool(WEBHOOK_SECRET_TOKEN) else "missing"))
    checks.append(("DATABASE_URL", "ok" if (DATABASE_URL and "postgres" in DATABASE_URL) else "bad"))
    checks.append(("SECRET_KEY", "ok" if bool(SECRET_KEY) else "missing"))
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1;")
        db_ok = "ok"
    except Exception as e:
        db_ok = f"fail: {e}"
    msg = "Диагностика окружения:\n" + "\n".join([f"• {k}: {v}" for k,v in checks]) + f"\n• DB connect: {db_ok}"
    await update.message.reply_text(msg)

# ---------- Background job ----------
async def check_job(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data
    creds = get_creds(uid)
    if not creds or not creds.alerts:
        return
    try:
        stage = await fetch_status(creds.case_no, creds.password)
        await context.bot.send_message(chat_id=uid, text=f"🔔 Обновление статуса:\n📌 *{safe_markdown(stage)}*",
                                       parse_mode="Markdown", reply_markup=main_kb(True, True))
    except Exception as e:
        await context.bot.send_message(chat_id=uid, text=f"⚠️ Не удалось проверить статус: {e}",
                                       reply_markup=main_kb(True, True))

# ---------- main ----------
def main():
    if not TELEGRAM_TOKEN: raise SystemExit("TELEGRAM_TOKEN не задан.")
    if not PUBLIC_URL or not PUBLIC_URL.startswith("https://"): raise SystemExit("PUBLIC_URL должен начинаться с https://")
    if not WEBHOOK_SECRET_TOKEN: raise SystemExit("WEBHOOK_SECRET_TOKEN не задан.")
    ensure_schema()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", greet))
    app.add_handler(CommandHandler("diag", diag))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_button)],
        states={
            0: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pass)],
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_creds)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        map_to_parent={},
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_button))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET_TOKEN,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
