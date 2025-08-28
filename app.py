import os
import re
import io
import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

import psycopg2
from psycopg2.extras import RealDictCursor
from cryptography.fernet import Fernet, InvalidToken

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Frame

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

def dec(blob) -> str:
    if blob is None:
        raise RuntimeError("В базе нет сохранённых данных.")
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    elif isinstance(blob, bytearray):
        blob = bytes(blob)
    elif isinstance(blob, str):
        blob = blob.encode("utf-8")
    return get_fernet().decrypt(blob).decode("utf-8")

# ---------- DB ----------
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан.")
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)

def ensure_schema():
    # Схему оставляем прежней (с колонкой alerts), но просто не используем её.
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
    alerts: bool  # не используется, оставлено для совместимости

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

def delete_user(telegram_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("delete from users where telegram_id=%s", (telegram_id,))
        conn.commit()

# ---------- Scraper helpers ----------
async def _fill_first_that_works(page, locators_factories, value: str):
    last_err = None
    for lf in locators_factories:
        try:
            await lf().fill(value, timeout=3000)
            return
        except Exception as e:
            last_err = e
    raise RuntimeError("Не нашёл поле ввода. Детали: " + str(last_err))

async def _click_first_that_works(page, locators_factories):
    last_err = None
    for lf in locators_factories:
        try:
            await lf().click(timeout=3000)
            return
        except Exception as e:
            last_err = e
    raise RuntimeError("Не получилось нажать кнопку входа. Детали: " + str(last_err))

# --- Парсер Vaadin-страницы: берём value у vaadin-text-field рядом с меткой ---
async def _status_from_frame(frame: Frame) -> str | None:
    js = """
    () => {
      const norm = s => (s || "")
        .toString()
        .normalize("NFKD")
        .replace(/[\\u0300-\\u036f]/g,"")
        .toLowerCase()
        .replace(/\\s+/g," ")
        .trim();

      const labelsWanted = ["etap postepowania","status sprawy","stage of proceedings"];

      const labels = Array.from(document.querySelectorAll("label"));
      for (const lb of labels) {
        const t = norm(lb.innerText);
        if (!labelsWanted.some(w => t.includes(w))) continue;

        const row = lb.closest("div")?.parentElement || lb.parentElement || document.body;
        const sel = "vaadin-text-field,vaadin-text-area,input,textarea,select,[value]";
        const candidates = Array.from(row.querySelectorAll(sel));

        let sib = lb.parentElement;
        for (let i=0; i<4 && sib; i++) {
          sib = sib.nextElementSibling;
          if (sib) candidates.push(...sib.querySelectorAll(sel));
        }

        for (const el of candidates) {
          let v = "";
          if ("value" in el) v = el.value || "";
          if (!v && el.getAttribute) v = el.getAttribute("value") || "";
          if (!v) v = (el.textContent || "");
          v = v.replace(/\\s+/g," ").trim();
          if (v) return v;
        }
      }

      const textNodes = Array.from(document.querySelectorAll("*"))
        .filter(n => labelsWanted.some(w => norm(n.textContent).includes(w)));
      for (const n of textNodes) {
        const sel = "vaadin-text-field,vaadin-text-area,input,textarea,select,[value]";
        const field = n.parentElement?.querySelector(sel)
                  || n.closest("div")?.querySelector(sel)
                  || n.ownerDocument.querySelector(sel);
        if (field) {
          let v = ("value" in field ? field.value : "") || field.getAttribute?.("value") || "";
          v = (v || field.textContent || "").replace(/\\s+/g," ").trim();
          if (v) return v;
        }
      }
      return null;
    }
    """
    with suppress(Exception):
        txt = await frame.evaluate(js)
        if txt:
            return re.sub(r"\s+", " ", txt).strip()
    return None

# ---------- Scraper ----------
async def fetch_status(case_no: str, password: str) -> str | tuple[str, bytes]:
    if not BROWSERLESS_WS:
        raise RuntimeError("BROWSERLESS_WS не задан.")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(BROWSERLESS_WS)
        except Exception as e:
            raise RuntimeError("Не удаётся подключиться к удалённому браузеру: " + str(e))

        context = await browser.new_context(
            locale="pl-PL",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

            with suppress(Exception):
                await page.get_by_role("button", name=re.compile("Akceptuj|Zgadzam|Accept|Zgoda", re.I)).click(timeout=2000)

            await _fill_first_that_works(page, [
                lambda: page.get_by_label(re.compile(r"Numer sprawy", re.I)),
                lambda: page.locator("input[name*='numer' i], input[id*='numer' i]"),
            ], case_no)

            await _fill_first_that_works(page, [
                lambda: page.get_by_label(re.compile(r"Hasło", re.I)),
                lambda: page.locator("input[type='password']"),
            ], password)

            await _click_first_that_works(page, [
                lambda: page.get_by_role("button", name=re.compile(r"Zaloguj|Log in|Zaloguj się", re.I)),
                lambda: page.locator("button[type='submit'],input[type='submit']"),
            ])

            await page.wait_for_load_state("domcontentloaded", timeout=45000)
            with suppress(Exception):
                await page.locator("label:has-text('Etap post')").first.wait_for(timeout=15000)

            with suppress(Exception):
                err = await page.get_by_text(re.compile(r"(błędne|nieprawidłow).*hasł|logow|błąd logowania", re.I)).inner_text(timeout=1500)
                if err: raise RuntimeError("Błąd logowania: sprawdź numer sprawy и hasło.")

            status = await _status_from_frame(page.main_frame)
            if status:
                return status

            for fr in page.frames:
                if fr is page.main_frame:
                    continue
                with suppress(Exception):
                    status = await _status_from_frame(fr)
                    if status:
                        return status

            img = await page.screenshot(full_page=True)
            return ("screenshot", img)

        except PlaywrightTimeout:
            raise RuntimeError("Портал не отвечает или работает медленно. Попробуйте позже.")
        finally:
            await context.close()

# ---------- UI ----------
AWAIT_CASE, AWAIT_PASS = range(2)

def main_kb(has_creds: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_creds:
        rows.append([InlineKeyboardButton("🔍 Проверить статус", callback_data="check")])
        rows.append([InlineKeyboardButton("🔑 Изменить данные", callback_data="connect")])
        rows.append([InlineKeyboardButton("🗑 Удалить данные", callback_data="unlink")])
    else:
        rows.append([InlineKeyboardButton("🔑 Подключить дело", callback_data="connect")])
    return InlineKeyboardMarkup(rows)

async def greet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    creds = get_creds(uid)
    text = (
        "Привет! Я помогу отслеживать статус твоего дела на портале Гданьского воеводы.\n\n"
        "• Нажми «🔑 Подключить дело», чтобы один раз сохранить *Numer sprawy* и *Hasło*.\n"
        "• Дальше жми «🔍 Проверить статус» — пришлю *Etap postępowania*.\n"
        "• Данные шифруются, их можно удалить одной кнопкой."
    )
    await update.message.reply_text(text, parse_mode="Markdown",
        reply_markup=main_kb(bool(creds)))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "connect":
