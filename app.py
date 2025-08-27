import os
import re
import io
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
    # простое экранирование для Markdown
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


async def safe_edit_or_send(query, text, reply_markup=None, parse_mode="Markdown"):
    """Пробуем отредактировать исходное сообщение; если нельзя — шлём новое."""
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
    """Надёжно превращаем bytea из БД в bytes и расшифровываем."""
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
    # sslmode=require ок и для external/internal строк на Render
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)


def ensure_schema():
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            create table if not exists users (
                telegram_id  bigint primary key,
                case_enc     bytea not null,
                pass_enc     bytea not null,
                alerts       boolean not null default false,
                created_at   timestamptz not null default now(),
                updated_at   timestamptz not null default now()
            );
            """
        )
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
            # ключ поменяли — проще попросить ввести заново
            return None


def upsert_creds(telegram_id: int, case_no: str, password: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into users(telegram_id, case_enc, pass_enc)
            values(%s, %s, %s)
            on conflict (telegram_id) do update set
              case_enc=excluded.case_enc,
              pass_enc=excluded.pass_enc,
              updated_at=now();
            """,
            (telegram_id, enc(case_no), enc(password)),
        )
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
async def fetch_status(case_no: str, password: str) -> str | tuple[str, bytes]:
    """
    Возвращает текст статуса или ('screenshot', image_bytes) —
    если текст не удалось достать (но логин прошёл).
    """
    if not BROWSERLESS_WS:
        raise RuntimeError("BROWSERLESS_WS не задан.")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(BROWSERLESS_WS)
        except Exception as e:
            raise RuntimeError(
                "Не удаётся подключиться к удалённому браузеру (Browserless). "
                "Проверь `BROWSERLESS_WS` и лимиты плана. Детали: " + str(e)
            )

        context = await browser.new_context(
            locale="pl-PL",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

            # Cookie banner (если есть)
            with suppress(Exception):
                await page.get_by_role("button", name=re.compile("Akceptuj|Zgadzam|Accept|Zgoda", re.I)).click(
                    timeout=3000
                )

            # Numer sprawy
            filled = False
            for locator in [
                page.get_by_label(re.compile(r"Numer sprawy", re.I)),
                page.get_by_placeholder(re.compile(r"Numer sprawy|Number of application", re.I)),
                page.locator("input[name*='numer' i], input[id*='numer' i]"),
            ]:
                try:
                    await locator.fill(case_no, timeout=3000)
                    filled = True
                    break
                except Exception:
                    pass
            if not filled:
                raise RuntimeError("Не нашёл поле 'Numer sprawy'.")

            # Hasło
            filled = False
            for locator in [
                page.get_by_label(re.compile(r"Hasło", re.I)),
                page.get_by_placeholder(re.compile(r"Hasło|Password", re.I)),
                page.locator("input[type='password']"),
            ]:
                try:
                    await locator.fill(password, timeout=3000)
                    filled = True
                    break
                except Exception:
                    pass
            if not filled:
                raise RuntimeError("Не нашёл поле 'Hasło'.")

            # Zaloguj
            clicked = False
            for locator in [
                page.get_by_role("button", name=re.compile(r"Zaloguj|Zalogować|Log in|Zaloguj się", re.I)),
                page.locator("input[type='submit']"),
                page.locator("button[type='submit']"),
            ]:
                try:
                    await locator.click(timeout=3000)
                    clicked = True
                    break
                except Exception:
                    pass
            if not clicked:
                raise RuntimeError("Не удалось нажать 'Zaloguj'.")

            await page.wait_for_load_state("domcontentloaded", timeout=45000)

            # Явная ошибка логина (плохой пароль/номер)
            with suppress(Exception):
                err = await page.get_by_text(
                    re.compile(r"(błędne|nieprawidłow).*hasł|logow|błąd logowania", re.I)
                ).inner_text(timeout=1500)
                if err:
                    raise RuntimeError("Błąd logowania: sprawdź numer sprawy и hasło.")

            # --- ИЩЕМ "Etap postępowania" НАДЁЖНО ---
            labels = [
                "Etap postępowania",
                "Etap postepowania",
                "Status sprawy",
                "Stage of proceedings",
            ]

            # (A) Таблица: <td>label</td><td>value</td> (как на твоём скрине)
            for lab in labels:
                for xp in [
                    f"xpath=//td[normalize-space()='{lab}']/following-sibling::td[1]",
                    f"xpath=//th[normalize-space()='{lab}']/following-sibling::td[1]",
                    f"xpath=//td[contains(normalize-space(),'{lab}')]/following-sibling::td[1]",
                    f"xpath=//th[contains(normalize-space(),'{lab}')]/following-sibling::td[1]",
                ]:
                    with suppress(Exception):
                        txt = await page.locator(xp).inner_text(timeout=2000)
                        txt = re.sub(r"\s+", " ", (txt or "")).strip()
                        if txt:
                            return txt

            # (B) Любая разметка: берём ближайший контейнер-ряд и «значение» после метки
            with suppress(Exception):
                row = page.locator(
                    "xpath=(//*[contains(normalize-space(.),'Etap postępowania') or "
                    "contains(normalize-space(.),'Etap postepowania') or "
                    "contains(normalize-space(.),'Status sprawy') or "
                    "contains(normalize-space(.),'Stage of proceedings')])[1]"
                    "/ancestor::*[self::tr or self::div[contains(@class,'row') or contains(@class,'form')]][1]"
                )
                txt = await row.inner_text(timeout=2000)
                txt = re.sub(
                    r".*?(Etap post[ęe]powania|Status sprawy|Stage of proceedings)\s*",
                    "",
                    txt,
                    flags=re.I | re.S,
                )
                txt = re.split(r"\n+", txt, 1)[0]
                txt = re.sub(r"\s+", " ", txt).strip()
                if txt:
                    return txt

            # (C) Запасные селекторы
            selectors = [
                "xpath=(//*[contains(normalize-space(.),'Etap postępowania')])[1]/following::*[self::span|self::div|self::td|self::p][1]",
                "[data-test*='etap' i], [data-testid*='etap' i]",
                "xpath=(//*[matches(translate(normalize-space(.),'ĄĆĘŁŃÓŚŹŻąćęłńóśźż','ACELNOSZZacelnoszz'),"
                "'Etap postepowania|Status sprawy|Stage of proceedings')])[1]"
                "/following::*[self::span|self::div|self::td|self::p][1]",
            ]
            for sel in selectors:
                with suppress(Exception):
                    txt = await page.locator(sel).inner_text(timeout=2000)
                    txt = re.sub(r"\s+", " ", (txt or "")).strip()
                    if txt:
                        return txt

            # Если ничего не нашли — вернём скриншот, чтобы видеть реальную страницу
            img = await page.screenshot(full_page=True)
            return ("screenshot", img)

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
        rows.append([InlineKeyboardButton(
            "🔔 Уведомления: ВКЛ" if alerts else "🔕 Уведомления: ВЫКЛ",
            callback_data="alerts_toggle"
        )])
        rows.append([InlineKeyboardButton("🔑 Изменить данные", callback_data="connect")])
        rows.append([InlineKeyboardButton("🗑 Удалить данные", callback_data="unlink")])
    else:
        rows.append([InlineKeyboardButton("🔑 Подключить дело", callback_data="connect")])
    return InlineKeyboardMarkup(rows)
