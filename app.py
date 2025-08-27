import os, re
from contextlib import suppress

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# --------- ENV ---------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BROWSERLESS_WS = os.getenv("BROWSERLESS_WS")  # wss://production-ams.browserless.io/?token=...
PUBLIC_URL = os.getenv("PUBLIC_URL")          # https://your-service.onrender.com
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")  # any long random string
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH") or (f"webhook/{WEBHOOK_SECRET_TOKEN or 'changeme'}")
PORT = int(os.getenv("PORT", "8000"))

LOGIN_URL = "https://klient.gdansk.uw.gov.pl"

# --------- scraping ---------
async def fetch_status(case_no: str, password: str) -> str:
    """
    Opens the portal, logs in and extracts the text near the label 'Etap postępowania'.
    """
    if not BROWSERLESS_WS:
        raise RuntimeError("Переменная окружения BROWSERLESS_WS не задана. См. инструкции.")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(BROWSERLESS_WS)
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
                    await locator.fill(case_no, timeout=3000)
                    filled = True
                    break
                except Exception:
                    pass
            if not filled:
                raise RuntimeError("Не нашёл поле 'Numer sprawy' — возможно, изменилась страница.")

            # fill "Hasło"
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
                raise RuntimeError("Не нашёл поле 'Hasło' — возможно, изменилась страница.")

            # click "Zaloguj"
            clicked = False
            for locator in [
                page.get_by_role("button", name=re.compile(r"Zaloguj|Zalogować|Log in", re.I)),
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
                raise RuntimeError("Не удалось нажать кнопку входа.")

            await page.wait_for_load_state("domcontentloaded", timeout=30000)

            with suppress(Exception):
                err = await page.get_by_text(re.compile(r"(błędne|nieprawidłow).*hasł|logow", re.I)).inner_text(timeout=1500)
                if err:
                    raise RuntimeError("Błąd logowania: sprawdź numer sprawy i hasło.")

            await page.wait_for_selector("text=Etap postępowania", timeout=15000)

            text = ""
            with suppress(Exception):
                text = await page.locator(
                    "xpath=(//*[contains(normalize-space(.),'Etap postępowania')])[1]"
                    "/following::*[self::span or self::div or self::td or self::p][1]"
                ).inner_text(timeout=2500)
            if not text:
                container = await page.locator(
                    "xpath=(//*[contains(normalize-space(.),'Etap postępowania')])[1]/ancestor::*[self::tr or self::div][1]"
                ).inner_text(timeout=2500)
                import re as _re
                text = _re.sub(r".*Etap postępowania[:\\s]*", "", container, flags=_re.S|_re.I).strip()

            import re as _re
            text = _re.sub(r"\\s+", " ", (text or "")).strip()
            if not text:
                raise RuntimeError("Не удалось найти текст статуса. Возможно, изменилась разметка портала.")
            return text

        except PlaywrightTimeout:
            raise RuntimeError("Портал не отвечает или работает медленно. Попробуйте ещё раз позже.")
        finally:
            await context.close()

# --------- telegram handlers ---------
def parse_args(txt: str):
    parts = re.split(r"\\s+", txt.strip(), maxsplit=2)
    if len(parts) >= 3:
        return parts[1], parts[2]
    raise ValueError("Формат: /status <NUMER_SPRAWY> <HASLO>")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли команду:\\n"
        "/status <NUMER_SPRAWY> <HASLO>\\n\\n"
        "Я залогинюсь на портале и верну текущий 'Etap postępowania'. "
        "Ничего не сохраняю."
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        case_no, password = parse_args(update.message.text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    msg = await update.message.reply_text("⏳ Проверяю статус на портале...")
    try:
        stage = await fetch_status(case_no, password)
        await msg.edit_text(f"📌 Etap postępowania: *{stage}*", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"⚠️ {e}")

# --------- main (Webhook mode) ---------
def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN не задан.")
    if not PUBLIC_URL or not PUBLIC_URL.startswith("https://"):
        raise SystemExit("PUBLIC_URL не задан или не начинается с https:// (см. инструкции).")
    if not WEBHOOK_SECRET_TOKEN:
        raise SystemExit("WEBHOOK_SECRET_TOKEN не задан. Задайте любой длинный случайный текст.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # This will set the webhook with Telegram on startup and start an HTTPS webhook listener
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,  # local path
        webhook_url=f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}",  # full public URL
        secret_token=WEBHOOK_SECRET_TOKEN,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
