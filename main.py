#!/usr/bin/env python3
"""
Парсер Яндекс.Карт + Рассылка
Пользователь вводит нишу, город, кол-во строк → бот парсит → сразу рассылает.

pip install python-telegram-bot telethon selenium webdriver-manager pandas openpyxl
"""

import asyncio, logging, os, random, re, sqlite3, time, traceback
from functools import wraps

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, InputUserDeactivatedError,
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
)

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ── КОНФИГ ────────────────────────────────────────────────────────────────────
BOT_TOKEN = "8656669785:AAG90VY2i8GcLJ7_f1FXIzwInRNpzr8eyx4"
API_ID    = 31970431
API_HASH  = "666358d5278cd72050cfe82e79dd49fb"
ADMIN_IDS = {8434813604, 8577264553}
DB_FILE   = "parser.db"



SNIPPET_SEL = "div.search-business-snippet-view"
LIST_SEL    = "ul.search-list-view__list"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── ГЛОБАЛЬНОЕ СОСТОЯНИЕ ──────────────────────────────────────────────────────
is_running = False
stop_flag  = False
tg_client: TelegramClient | None = None

# ── БД ────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS contacted (
        contact TEXT PRIMARY KEY, name TEXT, city TEXT, ts TEXT)""")
    defaults = {
        "broadcast_text": "Здравствуйте! Хотим предложить вам сотрудничество.",
        "msg_delay": "5",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    conn.commit(); conn.close()

def get_s(key, default=""):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default

def set_s(key, value):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    conn.commit(); conn.close()

def is_contacted(contact):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT 1 FROM contacted WHERE contact=?", (contact,)).fetchone()
    conn.close()
    return row is not None

def mark_contacted(contact, name, city):
    from datetime import datetime
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO contacted VALUES (?,?,?,?)",
                 (contact, name, city, datetime.now().isoformat()))
    conn.commit(); conn.close()

def total_contacted():
    conn = sqlite3.connect(DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM contacted").fetchone()[0]
    conn.close(); return n

# ── SELENIUM ──────────────────────────────────────────────────────────────────
def make_driver():
    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    for a in [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1920,1080",
        "--lang=ru-RU",
        "--disable-extensions",
        "--no-first-run",
        "--disable-setuid-sandbox",
    ]:
        opts.add_argument(a)
    chrome_binary = None
    for path in ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                 "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
        if os.path.exists(path):
            chrome_binary = path
            break
    drv = uc.Chrome(options=opts, headless=True, use_subprocess=True,
                    browser_executable_path=chrome_binary)
    drv.set_page_load_timeout(20)
    return drv

def _scroll(driver):
    try:
        cont = driver.find_element(By.CSS_SELECTOR, LIST_SEL)
        last = no_ch = 0
        for _ in range(60):
            if stop_flag: break
            driver.execute_script("arguments[0].lastElementChild.scrollIntoView();", cont)
            time.sleep(random.uniform(3.0, 5.0))
            cnt = len(driver.find_elements(By.CSS_SELECTOR, SNIPPET_SEL))
            if cnt == last:
                no_ch += 1
                if no_ch >= 3: break
            else:
                no_ch = 0
            last = cnt
    except Exception:
        pass

def _parse_card(card, driver):
    try:
        name = "—"
        el = card.find_elements(By.CSS_SELECTOR, "div.search-business-snippet-view__title")
        if el:
            name = driver.execute_script("return arguments[0].childNodes[0].textContent;", el[0]).strip()
        addr = "—"
        el = card.find_elements(By.CSS_SELECTOR, "a.search-business-snippet-view__address")
        if el: addr = el[0].text.strip()
        url = "—"
        el = card.find_elements(By.CSS_SELECTOR, "a[href*='/maps/org/']")
        if el: url = re.sub(r'/gallery/?$', '/', el[0].get_attribute("href") or "")
        return {"name": name, "addr": addr, "url": url, "phone": "—", "tg": "—"}
    except Exception:
        return None

def _get_contacts(driver, url):
    if url == "—": return "—", "—"
    try:
        driver.get(url)
        time.sleep(random.uniform(2.5, 4.0))
        phones = []
        def collect():
            for a in driver.find_elements(By.CSS_SELECTOR, "a[href^='tel:']"):
                raw = (a.get_attribute("href") or "").replace("tel:", "").strip()
                if raw and raw not in phones: phones.append(raw)
        collect()
        if not phones:
            clicked = False
            for sel in ["div.business-phone-view a","button.business-phone-view__phone-button",
                        "button[class*='PhoneButton']","button[class*='phone-button']"]:
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                for btn in btns:
                    try: driver.execute_script("arguments[0].click();", btn); clicked = True
                    except Exception: pass
            if clicked:
                time.sleep(2.0); collect()
        if not phones:
            body = driver.find_element(By.TAG_NAME, "body").text
            for m in re.findall(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', body):
                clean = re.sub(r'[\s\-\(\)]', '', m)
                if clean.startswith("8") and len(clean) == 11: clean = "+7" + clean[1:]
                if clean not in phones: phones.append(clean)
        phone = phones[0] if phones else "—"
        tg = "—"
        for sel in ["div.business-contacts-view a[href]","div.business-urls-view a[href]",
                    "div[class*='contacts'] a[href]","div[class*='social'] a[href]"]:
            for a in driver.find_elements(By.CSS_SELECTOR, sel):
                href = (a.get_attribute("href") or "").strip()
                if re.search(r'(t\.me|telegram\.me)/[A-Za-z0-9_@+]', href, re.I):
                    tg = re.sub(r'\?.*$', '', href).rstrip('/')
                    break
            if tg != "—": break
        return phone, tg
    except Exception as e:
        log.error(f"_get_contacts: {e}"); return "—", "—"

def parse_sync(niche, city, limit, chat_id=None):
    """Синхронный парсинг. Запускать через run_in_executor."""
    log.info(f"Парсинг: {niche} / {city} / лимит {limit}")
    driver = make_driver()
    results, seen = [], set()
    try:
        opened = False
        for attempt in range(3):
            try:
                driver.get("https://yandex.ru/maps")
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input.input__control")))
                opened = True
                break
            except Exception:
                if attempt == 2: break
                time.sleep(5)
        if not opened:
            return results
        box = driver.find_element(By.CSS_SELECTOR, "input.input__control")
        box.clear(); box.send_keys(f"{niche}, {city}"); time.sleep(1)
        box.send_keys(Keys.ENTER); time.sleep(8)
        _scroll(driver)
        cards = driver.find_elements(By.CSS_SELECTOR, SNIPPET_SEL)
        log.info(f"  Карточек: {len(cards)}")
        for card in cards:
            if stop_flag or len(results) >= limit: break
            row = _parse_card(card, driver)
            if not row: continue
            key = (row["name"], row["addr"])
            if key in seen: continue
            seen.add(key); results.append(row)
        import requests as req

        # Уведомляем что первый проход завершён
        if chat_id:
            req.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     json={"chat_id": chat_id, "text": f"📋 Найдено {len(results)} орг. Собираю контакты..."}, timeout=5)

        for i, row in enumerate(results):
            if stop_flag: break
            row["phone"], row["tg"] = _get_contacts(driver, row["url"])
            log.info(f"  [{i+1}/{len(results)}] {row['name'][:30]} | {row['phone']} | {row['tg']}")
            if chat_id and (i + 1) % 10 == 0:
                req.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                         json={"chat_id": chat_id, "text": f"⏳ Контакты: {i+1}/{len(results)}"}, timeout=5)
    except Exception:
        log.error(traceback.format_exc())
    finally:
        driver.quit()
    return results

# ── РАССЫЛКА ──────────────────────────────────────────────────────────────────
async def send_msg(client, contact, text):
    try:
        await client.send_message(contact, text)
        return True
    except FloodWaitError as e:
        log.warning(f"FloodWait {e.seconds}s")
        await asyncio.sleep(e.seconds + 5)
        try: await client.send_message(contact, text); return True
        except Exception: return False
    except (UserPrivacyRestrictedError, InputUserDeactivatedError):
        log.info(f"Пропуск {contact}: приватность/деактивирован")
        return False
    except ValueError as e:
        log.info(f"Пропуск {contact}: неверный формат — {e}")
        return False
    except Exception as e:
        log.error(f"send_msg {contact}: {type(e).__name__}: {e}")
        return False

# ── ОСНОВНОЙ ФЛОУ ─────────────────────────────────────────────────────────────
async def run_parse_and_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                              niche, city, limit):
    global is_running, stop_flag
    is_running = True
    stop_flag  = False
    msg_text   = get_s("broadcast_text")
    delay      = int(get_s("msg_delay", "5"))

    await update.message.reply_text(
        f"🔍 Парсинг: {niche} / {city} / до {limit} строк...\n"
        f"Это займёт несколько минут. Жди."
    )

    try:
        loop = asyncio.get_event_loop()
        orgs = await loop.run_in_executor(None, parse_sync, niche, city, limit, update.effective_chat.id)

        if not orgs:
            await update.message.reply_text("❌ Ничего не найдено.")
            return

        # Фильтруем контакты
        def normalize_contact(org):
            """Возвращает номер телефона. Telegram как запасной вариант."""
            phone = org.get("phone", "—")
            tg = org.get("tg", "—")
            if phone != "—":
                return phone
            if tg != "—":
                m = re.search(r'https?://t\.me/([A-Za-z0-9_@+]+)', tg)
                return m.group(1) if m else None
            return None

        to_send = []
        no_contact = already = 0
        for org in orgs:
            contact = normalize_contact(org)
            if not contact:
                no_contact += 1; continue
            if is_contacted(contact):
                already += 1; continue
            to_send.append((contact, org["name"]))

        log.info(f"Спарсено: {len(orgs)}, к отправке: {len(to_send)}, нет контакта: {no_contact}, уже: {already}")
        await update.message.reply_text(
            f"✅ Спарсено: {len(orgs)}\n"
            f"📤 К отправке: {len(to_send)}\n"
            f"⚪ Нет контакта: {no_contact}\n"
            f"⏭ Уже получали: {already}"
        )

        if not to_send:
            await update.message.reply_text("📭 Некому отправлять — у всех орг нет телефона/Telegram.")
            return

        if tg_client is None:
            await update.message.reply_text("❌ Telethon не авторизован — нажми 🔑 Авторизация и запусти заново.")
            return
        if not tg_client.is_connected():
            try:
                await tg_client.connect()
            except Exception as e:
                await update.message.reply_text(f"❌ Не удалось переподключить Telethon: {e}")
                return

        await update.message.reply_text("📨 Начинаю рассылку...")

        sent = errors = 0
        for i, (contact, name) in enumerate(to_send, 1):
            if stop_flag: break
            ok = await send_msg(tg_client, contact, msg_text)
            if ok:
                mark_contacted(contact, name, city); sent += 1
            else:
                errors += 1
            # Обновляем прогресс каждые 5
            if i % 5 == 0 or i == len(to_send):
                try:
                    await update.message.reply_text(
                        f"📨 Рассылка: {i}/{len(to_send)}\n"
                        f"✅ Отправлено: {sent}  ❌ Ошибок: {errors}"
                    )
                except Exception:
                    pass
            if i < len(to_send) and not stop_flag:
                await asyncio.sleep(delay)

        stopped = "🛑 Остановлено\n" if stop_flag else ""
        await update.message.reply_text(
            f"{stopped}"
            f"✅ Готово!\n\n"
            f"🏙 Ниша: {niche} / {city}\n"
            f"📊 Спарсено: {len(orgs)}\n"
            f"📤 Отправлено: {sent}\n"
            f"❌ Ошибок: {errors}\n"
            f"📦 Всего в базе: {total_contacted()}"
        )

    except Exception as e:
        log.error(traceback.format_exc())
        await update.message.reply_text(f"🔴 Ошибка: {e}")
    finally:
        is_running = False

# ── TELEGRAM BOT ──────────────────────────────────────────────────────────────
def _code_keyboard(current: str) -> InlineKeyboardMarkup:
    display = " ".join(current) if current else "_ _ _ _ _"
    rows = [
        [InlineKeyboardButton(display, callback_data="code_noop")],
        [InlineKeyboardButton(str(i), callback_data=f"code_{i}") for i in range(1, 4)],
        [InlineKeyboardButton(str(i), callback_data=f"code_{i}") for i in range(4, 7)],
        [InlineKeyboardButton(str(i), callback_data=f"code_{i}") for i in range(7, 10)],
        [
            InlineKeyboardButton("⌫", callback_data="code_back"),
            InlineKeyboardButton("0",  callback_data="code_0"),
            InlineKeyboardButton("✅ Подтвердить", callback_data="code_confirm"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

async def callback_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global tg_client
    query = update.callback_query
    await query.answer()
    data = query.data
    step = ctx.user_data.get("step")

    if step != "auth_code" or not data.startswith("code_"):
        return

    if data == "code_noop":
        return

    code = ctx.user_data.get("code_input", "")

    if data == "code_back":
        code = code[:-1]
    elif data == "code_confirm":
        if not code:
            await query.answer("Введи код!", show_alert=True)
            return
        client: TelegramClient = ctx.user_data.get("auth_client")
        phone  = ctx.user_data.get("auth_phone")
        sent   = ctx.user_data.get("sent_obj")
        try:
            me = await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            tg_client = client
            ctx.user_data["step"] = None
            ctx.user_data["code_input"] = ""
            await query.edit_message_text(f"✅ Авторизован: {me.first_name or phone}")
        except SessionPasswordNeededError:
            ctx.user_data["step"] = "auth_2fa"
            ctx.user_data["code_input"] = ""
            await query.edit_message_text("🔐 Введи пароль 2FA (текстом):")
        except PhoneCodeExpiredError:
            ctx.user_data["step"] = None
            ctx.user_data["code_input"] = ""
            await query.edit_message_text("❌ Код истёк. Нажми 🔑 Авторизация и запроси снова.")
        except PhoneCodeInvalidError:
            ctx.user_data["code_input"] = ""
            await query.edit_message_text("❌ Неверный код. Нажми 🔑 Авторизация и попробуй снова.")
        except Exception as e:
            ctx.user_data["step"] = None
            ctx.user_data["code_input"] = ""
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return
    else:
        digit = data.replace("code_", "")
        if digit.isdigit() and len(code) < 6:
            code += digit

    ctx.user_data["code_input"] = code
    try:
        await query.edit_message_reply_markup(reply_markup=_code_keyboard(code))
    except Exception:
        pass

def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔ Доступ запрещён.")
            return
        return await func(update, ctx)
    return wrapper

KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🔍 Новый парсинг")],
    [KeyboardButton("📊 Статус"),        KeyboardButton("🛑 Остановить")],
    [KeyboardButton("📝 Текст рассылки"), KeyboardButton("⏱ Задержка")],
    [KeyboardButton("🔑 Авторизация"),   KeyboardButton("🗑 Очистить базу")],
], resize_keyboard=True)

@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Парсер Яндекс.Карт + рассылка", reply_markup=KEYBOARD)

@restricted
async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global stop_flag, tg_client
    text = update.message.text.strip()
    step = ctx.user_data.get("step")

    # ── Главное меню ──────────────────────────────────────────────────────────
    if text == "🔍 Новый парсинг":
        if is_running:
            await update.message.reply_text("⏳ Уже идёт парсинг. Дождись окончания или нажми «🛑 Остановить».")
            return
        ctx.user_data["step"] = "niche"
        await update.message.reply_text("🔍 Введи нишу (например: стоматология, барбершоп, аптека):")
        return

    if text == "📊 Статус":
        sess = "✅" if (tg_client and tg_client.is_connected()) else "❌ нет"
        await update.message.reply_text(
            f"📊 Статус\n\n"
            f"🔄 Парсинг: {'🟡 идёт' if is_running else '⚪ ожидание'}\n"
            f"🔑 Telethon: {sess}\n"
            f"⏱ Задержка: {get_s('msg_delay')} сек.\n"
            f"📦 Всего в базе: {total_contacted()}\n\n"
            f"📝 Текст:\n{get_s('broadcast_text')}"
        )
        return

    if text == "🛑 Остановить":
        stop_flag = True
        await update.message.reply_text("🛑 Остановка после текущего шага.")
        return

    if text == "📝 Текст рассылки":
        ctx.user_data["step"] = "set_text"
        await update.message.reply_text(
            f"Текущий текст:\n\n{get_s('broadcast_text')}\n\nВведи новый текст:"
        )
        return

    if text == "⏱ Задержка":
        ctx.user_data["step"] = "set_delay"
        await update.message.reply_text(f"Текущая задержка: {get_s('msg_delay')} сек.\nВведи новое значение (число):")
        return

    if text == "🔑 Авторизация":
        ctx.user_data["step"] = "auth_phone"
        await update.message.reply_text("📱 Введи номер телефона (+79XXXXXXXXX):")
        return

    if text == "🗑 Очистить базу":
        ctx.user_data["step"] = "confirm_clear"
        await update.message.reply_text(f"⚠️ Удалить {total_contacted()} записей?\nНапиши ДА:")
        return

    # ── Шаги диалога ─────────────────────────────────────────────────────────
    if step == "niche":
        ctx.user_data["niche"] = text
        ctx.user_data["step"]  = "city"
        await update.message.reply_text("🏙 Введи город:")
        return

    if step == "city":
        ctx.user_data["city"] = text
        ctx.user_data["step"] = "limit"
        await update.message.reply_text("📊 Сколько строк спарсить? (число, макс 500):")
        return

    if step == "limit":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Введи число больше 0:")
            return
        limit = min(int(text), 500)
        niche = ctx.user_data.get("niche", "")
        city  = ctx.user_data.get("city", "")
        ctx.user_data["step"] = None
        ctx.application.create_task(run_parse_and_send(update, ctx, niche, city, limit))
        return

    if step == "set_text":
        set_s("broadcast_text", text)
        ctx.user_data["step"] = None
        await update.message.reply_text("✅ Текст сохранён.")
        return

    if step == "set_delay":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Введи целое число >= 1:")
            return
        set_s("msg_delay", text)
        ctx.user_data["step"] = None
        await update.message.reply_text(f"✅ Задержка: {text} сек.")
        return

    if step == "confirm_clear":
        ctx.user_data["step"] = None
        if text.upper() == "ДА":
            conn = sqlite3.connect(DB_FILE)
            conn.execute("DELETE FROM contacted")
            conn.commit(); conn.close()
            await update.message.reply_text("✅ База очищена.")
        else:
            await update.message.reply_text("Отменено.")
        return

    # ── Auth flow ─────────────────────────────────────────────────────────────
    if step == "auth_phone":
        phone = text.strip().replace(' ', '').replace('-', '')
        if not phone.startswith('+'):
            phone = '+' + phone
        session_name = f"session_{phone.replace('+', '')}"
        # Удаляем старый session файл чтобы начать чисто
        if os.path.exists(f"{session_name}.session"):
            os.remove(f"{session_name}.session")
            log.info(f"Удалён старый session файл: {session_name}.session")
        try:
            await update.message.reply_text("⏳ Подключаюсь к Telegram...")
            client = TelegramClient(session_name, API_ID, API_HASH,
                                    connection_retries=1, retry_delay=1, timeout=15)
            await client.connect()
            log.info(f"Подключено к Telegram DC, отправляю код на {phone}")
            await update.message.reply_text("📡 Отправляю код...")
            sent = await client.send_code_request(phone)
            code_type = type(sent.type).__name__
            log.info(f"Код отправлен: тип={code_type}, номер={phone}")
            ctx.user_data["auth_phone"] = phone
            ctx.user_data["auth_client"] = client
            ctx.user_data["sent_obj"] = sent
            ctx.user_data["step"] = "auth_code"
            type_ru = {
                "SentCodeTypeApp": "в приложение Telegram (открой Telegram на телефоне)",
                "SentCodeTypeSms": "по SMS",
                "SentCodeTypeCall": "звонком",
            }.get(code_type, code_type)
            ctx.user_data["code_input"] = ""
            await update.message.reply_text(
                f"📨 Код отправлен {type_ru}.\n\nВведи код цифрами:",
                reply_markup=_code_keyboard("")
            )
        except Exception as e:
            log.error(f"auth_phone error: {traceback.format_exc()}")
            ctx.user_data["step"] = None
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    # auth_code теперь обрабатывается через inline callback

    if step == "auth_2fa":
        client: TelegramClient = ctx.user_data.get("auth_client")
        try:
            me = await client.sign_in(password=text.strip())
            tg_client = client
            ctx.user_data["step"] = None
            await update.message.reply_text(f"✅ Авторизован: {me.first_name or 'аккаунт'}")
        except Exception as e:
            await update.message.reply_text(f"❌ Неверный пароль: {e}")
        return

# ── ЗАПУСК ────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    global tg_client
    for name in ["userbot_session", "session_79872582765"]:
        if os.path.exists(f"{name}.session"):
            try:
                client = TelegramClient(name, API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    tg_client = client
                    me = await client.get_me()
                    log.info(f"Telethon: {me.first_name}")
                    for uid in ADMIN_IDS:
                        try: await app.bot.send_message(uid, f"🚀 Бот запущен\n👤 Telethon: {me.first_name}")
                        except Exception: pass
                    return
                await client.disconnect()
            except Exception as e:
                log.warning(f"Сессия {name}: {e}")
    for uid in ADMIN_IDS:
        try: await app.bot.send_message(uid, "🚀 Бот запущен\n⚠️ Telethon не авторизован — нажми «🔑 Авторизация»")
        except Exception: pass

async def post_shutdown(app: Application):
    if tg_client and tg_client.is_connected():
        await tg_client.disconnect()

def main():
    init_db()
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_code, pattern="^code_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    log.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
