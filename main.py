import os
import logging
import asyncio
import random
import string
import tempfile
import subprocess
import urllib.parse
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
)
import psycopg2
from psycopg2 import pool

# ---------- تنظیمات اولیه ----------
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@teazvpn"
ADMIN_ID = 5542927340
BANK_CARD = "6219 8614 2845 2139"
CONFIG_PRICE = 950000  # قیمت هر کانفیگ 950,000 تومان

RENDER_BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RAILWAY_STATIC_URL") or "https://teazvpn.railway.app"
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"{RENDER_BASE_URL}{WEBHOOK_PATH}"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8") if os.path.exists("/tmp") else logging.StreamHandler()
    ]
)

app = FastAPI()

# ---------- endpoint سلامت ----------
@app.get("/")
async def health_check():
    return {"status": "up", "message": "Bot is running!", "timestamp": datetime.now().isoformat()}

@app.get("/health")
async def health():
    try:
        await db_execute("SELECT 1", fetchone=True)
        return {"status": "healthy", "database": "connected", "bot": "running", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e), "timestamp": datetime.now().isoformat()}

@app.get("/ping")
async def ping():
    return {"pong": True, "timestamp": datetime.now().isoformat()}

# ---------- مدیریت application ----------
application = Application.builder().token(TOKEN).build()

# ---------- PostgreSQL connection pool ----------
DATABASE_URL = os.getenv("DATABASE_URL")
db_pool: pool.ThreadedConnectionPool = None

def init_db_pool():
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL, sslmode='require')
        logging.info("Database pool initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize database pool: {e}")
        raise

def close_db_pool():
    global db_pool
    if db_pool:
        db_pool.closeall()
        db_pool = None
        logging.info("Database pool closed")

def _db_execute_sync(query, params=(), fetch=False, fetchone=False, returning=False):
    conn = None
    cur = None
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        cur.execute(query, params)
        result = None
        if returning:
            result = cur.fetchone()[0] if cur.rowcount > 0 else None
        elif fetchone:
            result = cur.fetchone()
        elif fetch:
            result = cur.fetchall()
        if not query.strip().lower().startswith("select"):
            conn.commit()
        return result
    except Exception as e:
        logging.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            db_pool.putconn(conn)

async def db_execute(query, params=(), fetch=False, fetchone=False, returning=False):
    try:
        return await asyncio.to_thread(_db_execute_sync, query, params, fetch, fetchone, returning)
    except Exception as e:
        logging.error(f"Async database error: {e}")
        raise

# ---------- ساخت جداول ----------
CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    balance BIGINT DEFAULT 0,
    invited_by BIGINT,
    phone TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_agent BOOLEAN DEFAULT FALSE,
    is_new_user BOOLEAN DEFAULT TRUE
)
"""
CREATE_PAYMENTS_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    amount BIGINT,
    status TEXT,
    type TEXT,
    payment_method TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
CREATE_SUBSCRIPTIONS_SQL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id BIGINT,
    payment_id INTEGER,
    plan TEXT,
    config TEXT,
    status TEXT DEFAULT 'pending',
    start_date TIMESTAMP,
    duration_days INTEGER
)
"""
CREATE_COUPONS_SQL = """
CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    discount_percent INTEGER,
    user_id BIGINT,
    is_used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expiry_date TIMESTAMP GENERATED ALWAYS AS (created_at + INTERVAL '3 days') STORED
)
"""

MIGRATE_SUBSCRIPTIONS_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_new_user') THEN
        ALTER TABLE users ADD COLUMN is_new_user BOOLEAN DEFAULT TRUE;
    END IF;
    UPDATE users SET is_new_user = FALSE WHERE is_new_user IS NULL;
END $$;

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_date TIMESTAMP;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS duration_days INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_agent BOOLEAN DEFAULT FALSE;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_method TEXT;

UPDATE subscriptions SET start_date = COALESCE(start_date, CURRENT_TIMESTAMP), duration_days = 30
WHERE start_date IS NULL OR duration_days IS NULL;
"""

async def create_tables():
    try:
        await db_execute(CREATE_USERS_SQL)
        await db_execute(CREATE_PAYMENTS_SQL)
        await db_execute(CREATE_SUBSCRIPTIONS_SQL)
        await db_execute(CREATE_COUPONS_SQL)
        await db_execute(MIGRATE_SUBSCRIPTIONS_SQL)
        logging.info("Database tables created and migrated successfully")
    except Exception as e:
        logging.error(f"Error creating or migrating tables: {e}")

# ---------- وضعیت کاربر ----------
user_states = {}

def generate_coupon_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ---------- کیبوردها ----------
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("💰 موجودی"), KeyboardButton("💳 خرید اشتراک")],
        [KeyboardButton("☎️ پشتیبانی")],
        [KeyboardButton("📂 اشتراک‌های من"), KeyboardButton("💡 راهنمای اتصال")],
        [KeyboardButton("🧑‍💼 درخواست نمایندگی")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_balance_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("نمایش موجودی"), KeyboardButton("افزایش موجودی")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)

def get_back_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)

def get_subscription_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("⭐️ کانفیگ تانل ویژه | گیگی ۹۵۰")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)

def get_payment_method_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("🏦 کارت به کارت")], [KeyboardButton("💰 پرداخت با موجودی")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)

def get_connection_guide_keyboard():
    keyboard = [[KeyboardButton("📗 اندروید")], [KeyboardButton("📕 آیفون/مک")], [KeyboardButton("📘 ویندوز")], [KeyboardButton("📙 لینوکس")], [KeyboardButton("⬅️ بازگشت به منو")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_coupon_recipient_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("📢 برای همه")], [KeyboardButton("👤 برای یک نفر")], [KeyboardButton("🎯 درصد خاصی از کاربران")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)

# ---------- توابع کمکی ----------
async def send_long_message(chat_id, text, context, reply_markup=None, parse_mode=None):
    max_len = 4000
    if len(text) <= max_len:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    messages = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            messages.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        messages.append(current)
    for i, msg in enumerate(messages):
        await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=reply_markup if i == len(messages)-1 else None, parse_mode=parse_mode)

# ---------- توابع DB ----------
async def is_user_member(user_id):
    try:
        member = await application.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def notify_admin_new_user(user_id, username, invited_by=None):
    try:
        total_users = await db_execute("SELECT COUNT(*) FROM users", fetchone=True)
        msg = f"🆕 کاربر جدید:\n🆔 {user_id}\n📛 @{username if username else 'بدون یوزرنیم'}\n📊 کل کاربران: {total_users[0] if total_users else 0}"
        await application.bot.send_message(chat_id=ADMIN_ID, text=msg)
    except:
        pass

async def ensure_user(user_id, username, invited_by=None):
    try:
        row = await db_execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        if not row:
            await db_execute("INSERT INTO users (user_id, username, invited_by, is_agent, is_new_user) VALUES (%s, %s, %s, FALSE, TRUE)", (user_id, username, invited_by))
            await notify_admin_new_user(user_id, username, invited_by)
            if invited_by and invited_by != user_id:
                await add_balance(invited_by, 10000)
        else:
            await db_execute("UPDATE users SET is_new_user = FALSE WHERE user_id = %s", (user_id,))
    except Exception as e:
        logging.error(f"Error ensuring user: {e}")

async def add_balance(user_id, amount):
    try:
        await db_execute("UPDATE users SET balance = COALESCE(balance,0) + %s WHERE user_id = %s", (amount, user_id))
    except Exception as e:
        logging.error(f"Error adding balance: {e}")

async def deduct_balance(user_id, amount):
    try:
        await db_execute("UPDATE users SET balance = COALESCE(balance,0) - %s WHERE user_id = %s", (amount, user_id))
    except Exception as e:
        logging.error(f"Error deducting balance: {e}")

async def get_balance(user_id):
    try:
        row = await db_execute("SELECT balance FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        return int(row[0]) if row and row[0] is not None else 0
    except:
        return 0

async def is_user_agent(user_id):
    try:
        row = await db_execute("SELECT is_agent FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        return row[0] if row and row[0] is not None else False
    except:
        return False

async def set_user_agent(user_id):
    try:
        await db_execute("UPDATE users SET is_agent = TRUE WHERE user_id = %s", (user_id,))
    except:
        pass

async def unset_user_agent(user_id):
    try:
        await db_execute("UPDATE users SET is_agent = FALSE WHERE user_id = %s", (user_id,))
    except:
        pass

async def add_payment(user_id, amount, ptype, payment_method, description="", coupon_code=None):
    try:
        query = "INSERT INTO payments (user_id, amount, status, type, payment_method, description) VALUES (%s, %s, 'pending', %s, %s, %s) RETURNING id"
        new_id = await db_execute(query, (user_id, amount, ptype, payment_method, description), returning=True)
        if coupon_code:
            await mark_coupon_used(coupon_code)
        return int(new_id) if new_id is not None else None
    except:
        return None

async def add_subscription(user_id, payment_id, plan):
    try:
        await db_execute("INSERT INTO subscriptions (user_id, payment_id, plan, status, start_date, duration_days) VALUES (%s, %s, %s, 'pending', CURRENT_TIMESTAMP, 30)", (user_id, payment_id, plan))
    except Exception as e:
        logging.error(f"Error adding subscription: {e}")

async def update_subscription_config(payment_id, config):
    try:
        await db_execute("UPDATE subscriptions SET config = %s, status = 'active' WHERE payment_id = %s", (config, payment_id))
    except:
        pass

async def update_payment_status(payment_id, status):
    try:
        await db_execute("UPDATE payments SET status = %s WHERE id = %s", (status, payment_id))
    except:
        pass

async def get_user_subscriptions(user_id):
    try:
        rows = await db_execute("SELECT id, plan, config, status, payment_id, start_date, duration_days FROM subscriptions WHERE user_id = %s ORDER BY status DESC, start_date DESC", (user_id,), fetch=True)
        current_time = datetime.now()
        subs = []
        for row in rows:
            sub_id, plan, config, status, payment_id, start_date, duration_days = row
            start_date = start_date or current_time
            duration_days = duration_days or 30
            if status == "active":
                end_date = start_date + timedelta(days=duration_days)
                if current_time > end_date:
                    await db_execute("UPDATE subscriptions SET status = 'inactive' WHERE id = %s", (sub_id,))
                    status = "inactive"
            subs.append({'id': sub_id, 'plan': plan, 'config': config, 'status': status, 'payment_id': payment_id, 'start_date': start_date, 'duration_days': duration_days, 'end_date': start_date + timedelta(days=duration_days)})
        return subs
    except:
        return []

async def create_coupon(code, discount_percent, user_id=None):
    try:
        await db_execute("INSERT INTO coupons (code, discount_percent, user_id, is_used) VALUES (%s, %s, %s, FALSE)", (code, discount_percent, user_id))
    except:
        pass

async def validate_coupon(code, user_id):
    try:
        row = await db_execute("SELECT discount_percent, user_id, is_used, expiry_date FROM coupons WHERE code = %s", (code,), fetchone=True)
        if not row:
            return None, "کد تخفیف نامعتبر است."
        discount_percent, coupon_user_id, is_used, expiry_date = row
        if is_used:
            return None, "این کد قبلاً استفاده شده است."
        if datetime.now() > expiry_date:
            return None, "این کد منقضی شده است."
        if coupon_user_id is not None and coupon_user_id != user_id:
            return None, "این کد برای شما نیست."
        if await is_user_agent(user_id):
            return None, "نمایندگان نمی‌توانند از کد تخفیف استفاده کنند."
        return discount_percent, None
    except:
        return None, "خطا در بررسی کد تخفیف."

async def mark_coupon_used(code):
    try:
        await db_execute("UPDATE coupons SET is_used = TRUE WHERE code = %s", (code,))
    except:
        pass

async def remove_user_from_db(user_id):
    try:
        await db_execute("DELETE FROM coupons WHERE user_id = %s", (user_id,))
        await db_execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
        await db_execute("DELETE FROM payments WHERE user_id = %s", (user_id,))
        await db_execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        return True
    except:
        return False

async def send_notification_to_users(context, user_ids, notification_text):
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id[0], text=f"📢 اطلاعیه:\n\n{notification_text}")
            sent += 1
        except:
            failed += 1
    return sent, failed, []

# ---------- دستورات ادمین ----------
async def set_bot_commands():
    try:
        public_commands = [BotCommand(command="/start", description="شروع ربات")]
        admin_commands = [
            BotCommand(command="/start", description="شروع ربات"),
            BotCommand(command="/stats", description="آمار ربات"),
            BotCommand(command="/user_info", description="اطلاعات کاربران"),
            BotCommand(command="/coupon", description="ایجاد کد تخفیف"),
            BotCommand(command="/notification", description="ارسال اطلاعیه"),
            BotCommand(command="/backup", description="تهیه بکاپ"),
            BotCommand(command="/restore", description="بازیابی بکاپ"),
            BotCommand(command="/remove_user", description="حذف کاربر"),
            BotCommand(command="/cleardb", description="پاک کردن دیتابیس"),
            BotCommand(command="/debug_subscriptions", description="تشخیص اشتراک‌ها")
        ]
        await application.bot.set_my_commands(public_commands)
        await application.bot.set_my_commands(admin_commands, scope={"type": "chat", "chat_id": ADMIN_ID})
    except:
        pass

async def stats_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    total = await db_execute("SELECT COUNT(*) FROM users", fetchone=True)
    await update.message.reply_text(f"📊 تعداد کل کاربران: {total[0] if total else 0} نفر")

async def user_info_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    users = await db_execute("SELECT user_id, username, balance, is_agent FROM users ORDER BY created_at DESC", fetch=True)
    if not users:
        await update.message.reply_text("📂 هیچ کاربری یافت نشد.")
        return
    response = "👥 لیست کاربران:\n\n"
    for u in users:
        uid, uname, bal, agent = u
        response += f"🆔 {uid} | @{uname if uname else 'بدون یوزرنیم'} | {bal:,} تومان | {'نماینده' if agent else 'ساده'}\n"
    await send_long_message(ADMIN_ID, response, context)

async def coupon_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    await update.message.reply_text("💵 درصد تخفیف را وارد کنید (مثال: 20):")
    user_states[update.effective_user.id] = "awaiting_coupon_discount"

async def notification_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    kb = [[KeyboardButton("📢 پیام به همه کاربران")], [KeyboardButton("🧑‍💼 پیام به نمایندگان")], [KeyboardButton("👤 پیام به یک نفر")], [KeyboardButton("⬅️ بازگشت به منو")]]
    await update.message.reply_text("📢 نوع اطلاع‌رسانی را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    user_states[update.effective_user.id] = "awaiting_notification_type"

async def backup_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    await update.message.reply_text("✅ بکاپ تهیه شد.")

async def restore_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    await update.message.reply_text("📤 فایل بکاپ را ارسال کنید:")
    user_states[update.effective_user.id] = "awaiting_backup_file"

async def remove_user_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    await update.message.reply_text("🆔 ایدی کاربر را وارد کنید:")
    user_states[update.effective_user.id] = "awaiting_user_id_for_removal"

async def clear_db(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    await update.message.reply_text("✅ دیتابیس پاک شد.")

async def debug_subscriptions(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی ندارید.")
        return
    await update.message.reply_text("📂 بررسی اشتراک‌ها انجام شد.")

# ---------- هندلرهای اصلی ----------
async def start(update, context):
    user = update.effective_user
    if not await is_user_member(user.id):
        kb = [[InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{CHANNEL_USERNAME.replace('@','')}")]]
        await update.message.reply_text("❌ ابتدا در کانال عضو شوید.", reply_markup=InlineKeyboardMarkup(kb))
        return
    invited_by = context.user_data.get("invited_by")
    await ensure_user(user.id, user.username or "", invited_by)
    await update.message.reply_text("🌐 به فروشگاه تیز VPN خوش آمدید!", reply_markup=get_main_keyboard())
    user_states.pop(user.id, None)

async def start_with_param(update, context):
    args = context.args
    if args and len(args) > 0:
        try:
            invited_by = int(args[0])
            if invited_by != update.effective_user.id:
                context.user_data["invited_by"] = invited_by
        except:
            pass
    await start(update, context)

async def handle_config_count(update, context, user_id, state, text):
    try:
        count = int(text)
        if count < 1:
            await update.message.reply_text("⚠️ تعداد باید حداقل 1 باشد.", reply_markup=get_back_keyboard())
            return
        total_amount = CONFIG_PRICE * count
        plan_name = f"⭐️ کانفیگ تانل ویژه | گیگی ۹۵۰ (x{count})"
        await update.message.reply_text(
            f"✅ {count} عدد کانفیگ با قیمت {total_amount:,} تومان\n\nبرای ادامه روی 'ادامه' کلیک کنید:",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("ادامه")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)
        )
        user_states[user_id] = f"awaiting_coupon_code_{total_amount}_{plan_name}"
    except ValueError:
        await update.message.reply_text("⚠️ لطفا یک عدد صحیح وارد کنید:", reply_markup=get_back_keyboard())

async def handle_coupon_code(update, context, user_id, state, text):
    parts = state.split("_")
    amount = int(parts[3])
    plan = "_".join(parts[4:])
    if text == "ادامه":
        user_states[user_id] = f"awaiting_payment_method_{amount}_{plan}"
        await update.message.reply_text("💳 روش خرید را انتخاب کنید:", reply_markup=get_payment_method_keyboard())
        return
    discount_percent, error = await validate_coupon(text.strip(), user_id)
    if error:
        await update.message.reply_text(f"⚠️ {error}\nبرای ادامه روی 'ادامه' کلیک کنید:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("ادامه")]], resize_keyboard=True))
        return
    discounted_amount = int(amount * (1 - discount_percent / 100))
    user_states[user_id] = f"awaiting_payment_method_{discounted_amount}_{plan}_{text.strip()}"
    await update.message.reply_text(f"✅ کد تخفیف اعمال شد! مبلغ با {discount_percent}% تخفیف: {discounted_amount:,} تومان\nروش خرید را انتخاب کنید:", reply_markup=get_payment_method_keyboard())

async def handle_payment_method(update, context, user_id, text):
    state = user_states.get(user_id)
    try:
        parts = state.split("_")
        amount = int(parts[3])
        plan = "_".join(parts[4:]) if len(parts) <= 5 else "_".join(parts[4:-1])
        coupon_code = parts[-1] if len(parts) > 5 else None
        if text == "🏦 کارت به کارت":
            payment_id = await add_payment(user_id, amount, "buy_subscription", "card_to_card", description=plan, coupon_code=coupon_code)
            if payment_id:
                await add_subscription(user_id, payment_id, plan)
                await update.message.reply_text(f"لطفا {amount:,} تومان واریز کنید:\n\n🏦 شماره کارت:\n`{BANK_CARD}`\nبحق", reply_markup=get_back_keyboard(), parse_mode="MarkdownV2")
                user_states[user_id] = f"awaiting_subscription_receipt_{payment_id}"
            else:
                await update.message.reply_text("⚠️ خطا در ثبت پرداخت.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
        elif text == "💰 پرداخت با موجودی":
            balance = await get_balance(user_id)
            if balance >= amount:
                payment_id = await add_payment(user_id, amount, "buy_subscription", "balance", description=plan, coupon_code=coupon_code)
                if payment_id:
                    await add_subscription(user_id, payment_id, plan)
                    await deduct_balance(user_id, amount)
                    await update_payment_status(payment_id, "approved")
                    await update.message.reply_text("✅ خرید انجام شد. کانفیگ ارسال خواهد شد.", reply_markup=get_main_keyboard())
                    await context.bot.send_message(ADMIN_ID, f"📢 کاربر {user_id} با موجودی خود سرویس {plan} خریداری کرد.")
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🟣 ارسال کانفیگ", callback_data=f"send_config_{payment_id}")]])
                    await context.bot.send_message(ADMIN_ID, f"✅ پرداخت برای اشتراک ({plan}) تایید شد.", reply_markup=kb)
                    user_states.pop(user_id, None)
            else:
                await update.message.reply_text(f"⚠️ موجودی شما ({balance:,} تومان) کافی نیست.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
    except Exception as e:
        logging.error(f"Error in payment method: {e}")
        await update.message.reply_text("⚠️ خطا در پردازش.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)

async def process_payment_receipt(update, context, user_id, payment_id, receipt_type):
    try:
        payment = await db_execute("SELECT amount, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
        if not payment:
            await update.message.reply_text("⚠️ پرداخت یافت نشد.", reply_markup=get_main_keyboard())
            return
        amount, description = payment
        caption = f"💳 فیش پرداختی از کاربر {user_id}:\nمبلغ: {amount:,}\nنوع: خرید اشتراک"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تایید", callback_data=f"approve_{payment_id}"), InlineKeyboardButton("❌ رد", callback_data=f"reject_{payment_id}")]])
        if update.message.photo:
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=caption, reply_markup=kb)
        elif update.message.document:
            await context.bot.send_document(chat_id=ADMIN_ID, document=update.message.document.file_id, caption=caption, reply_markup=kb)
        else:
            await update.message.reply_text("⚠️ لطفا فیش را به صورت عکس ارسال کنید.", reply_markup=get_back_keyboard())
            return
        await update.message.reply_text("✅ فیش برای ادمین ارسال شد.", reply_markup=get_main_keyboard())
    except Exception as e:
        logging.error(f"Error processing receipt: {e}")
        await update.message.reply_text("⚠️ خطا در ارسال فیش.", reply_markup=get_main_keyboard())

async def process_config(update, context, user_id, payment_id):
    try:
        payment = await db_execute("SELECT user_id, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
        if not payment:
            await update.message.reply_text("⚠️ پرداخت یافت نشد.")
            return
        buyer_id, description = payment
        if update.message.text:
            await update_subscription_config(payment_id, update.message.text)
            await context.bot.send_message(buyer_id, f"✅ کانفیگ اشتراک شما:\n```\n{update.message.text}\n```", parse_mode="Markdown")
            await update.message.reply_text("✅ کانفیگ ارسال شد.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("⚠️ لطفا کانفیگ را به صورت متن ارسال کنید.", reply_markup=get_back_keyboard())
    except Exception as e:
        logging.error(f"Error processing config: {e}")
        await update.message.reply_text("⚠️ خطا.", reply_markup=get_main_keyboard())

async def handle_coupon_recipient(update, context, user_id, state, text):
    parts = state.split("_")
    coupon_code = parts[3]
    discount_percent = int(parts[4])
    if text == "📢 برای همه":
        await create_coupon(coupon_code, discount_percent)
        users = await db_execute("SELECT user_id FROM users", fetch=True)
        sent = 0
        for u in users:
            try:
                await context.bot.send_message(u[0], f"🎉 کد تخفیف `{coupon_code}` با {discount_percent}% تخفیف!")
                sent += 1
            except:
                pass
        await update.message.reply_text(f"✅ کد تخفیف برای {sent} کاربر ارسال شد.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
    elif text == "👤 برای یک نفر":
        user_states[user_id] = f"awaiting_single_coupon_user_{coupon_code}_{discount_percent}"
        await update.message.reply_text("🆔 ایدی کاربر را وارد کنید:", reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text("⚠️ گزینه نامعتبر.", reply_markup=get_coupon_recipient_keyboard())

async def handle_remove_user(update, context, user_id, text):
    try:
        target = int(text)
        success = await remove_user_from_db(target)
        if success:
            await update.message.reply_text(f"✅ کاربر {target} حذف شد.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("⚠️ خطا در حذف کاربر.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
    except:
        await update.message.reply_text("⚠️ ایدی نامعتبر.", reply_markup=get_back_keyboard())

async def handle_normal_commands(update, context, user_id, text):
    if text == "💰 موجودی":
        await update.message.reply_text("💰 بخش موجودی:", reply_markup=get_balance_keyboard())
    elif text == "نمایش موجودی":
        bal = await get_balance(user_id)
        await update.message.reply_text(f"💰 موجودی شما: {bal:,} تومان", reply_markup=get_balance_keyboard())
    elif text == "افزایش موجودی":
        await update.message.reply_text("💳 مبلغ را وارد کنید:", reply_markup=get_back_keyboard())
        user_states[user_id] = "awaiting_deposit_amount"
    elif user_states.get(user_id) == "awaiting_deposit_amount" and text.isdigit():
        amount = int(text)
        payment_id = await add_payment(user_id, amount, "increase_balance", "card_to_card")
        if payment_id:
            await update.message.reply_text(f"لطفا {amount:,} تومان واریز کنید:\n\n🏦 شماره کارت:\n`{BANK_CARD}`\nبحق", reply_markup=get_back_keyboard(), parse_mode="MarkdownV2")
            user_states[user_id] = f"awaiting_deposit_receipt_{payment_id}"
        else:
            await update.message.reply_text("⚠️ خطا.", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
    elif text == "💳 خرید اشتراک":
        await update.message.reply_text("💳 پلن را انتخاب کنید:", reply_markup=get_subscription_keyboard())
    elif text == "⭐️ کانفیگ تانل ویژه | گیگی ۹۵۰":
        await update.message.reply_text(f"🔢 تعداد کانفیگ را وارد کنید (مثال: 2):\n💰 هر کانفیگ: {CONFIG_PRICE:,} تومان", reply_markup=get_back_keyboard())
        user_states[user_id] = f"awaiting_config_count_{CONFIG_PRICE}_⭐️ کانفیگ تانل ویژه | گیگی ۹۵۰"
    elif user_states.get(user_id, "").startswith("awaiting_payment_method_"):
        await handle_payment_method(update, context, user_id, text)
    elif text == "☎️ پشتیبانی":
        await update.message.reply_text("📞 پشتیبانی: @teazadmin", reply_markup=get_main_keyboard())
    elif text == "📂 اشتراک‌های من":
        subs = await get_user_subscriptions(user_id)
        if not subs:
            await update.message.reply_text("📂 شما اشتراکی ندارید.", reply_markup=get_main_keyboard())
            return
        response = "📂 اشتراک‌های شما:\n\n"
        for s in subs:
            response += f"🔹 {s['plan']}\n📊 وضعیت: {'✅ فعال' if s['status'] == 'active' else '⏳ در انتظار'}\n"
            if s['status'] == 'active' and s['config']:
                response += f"🔐 کانفیگ:\n```\n{s['config']}\n```\n"
            response += "--------------------\n"
        await send_long_message(user_id, response, context, parse_mode="Markdown")
    elif text == "💡 راهنمای اتصال":
        await update.message.reply_text("راهنمای اتصال\nدستگاه خود را انتخاب کنید:", reply_markup=get_connection_guide_keyboard())
    elif text in ["📗 اندروید", "📕 آیفون/مک", "📘 ویندوز", "📙 لینوکس"]:
        guides = {"📗 اندروید": "V2RayNG یا Hiddify", "📕 آیفون/مک": "Singbox یا V2box", "📘 ویندوز": "V2rayN", "📙 لینوکس": "V2rayN"}
        await update.message.reply_text(guides[text], reply_markup=get_connection_guide_keyboard())
    elif text == "🧑‍💼 درخواست نمایندگی":
        await update.message.reply_text("👨‍💼 برای نمایندگی به پشتیبانی مراجعه کنید:\n@teazadmin", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("⚠️ از دکمه‌ها استفاده کنید.", reply_markup=get_main_keyboard())

async def admin_callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        await query.edit_message_text("⚠️ شما اجازه ندارید.")
        return
    data = query.data
    try:
        if data.startswith("approve_"):
            payment_id = int(data.split("_")[1])
            payment = await db_execute("SELECT user_id, amount, type, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
            if not payment:
                await query.edit_message_text("⚠️ پرداخت یافت نشد.")
                return
            uid, amt, ptype, desc = payment
            await update_payment_status(payment_id, "approved")
            if ptype == "increase_balance":
                await add_balance(uid, amt)
                await context.bot.send_message(uid, f"💰 {amt:,} تومان به موجودی شما اضافه شد.")
                await query.edit_message_text("✅ پرداخت تایید شد.")
            elif ptype == "buy_subscription":
                await context.bot.send_message(uid, f"✅ پرداخت تایید شد. کد خرید: #{payment_id}")
                await query.edit_message_reply_markup(reply_markup=None)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🟣 ارسال کانفیگ", callback_data=f"send_config_{payment_id}")]])
                await context.bot.send_message(ADMIN_ID, f"✅ پرداخت #{payment_id} تایید شد.\n👤 کاربر: {uid}\n📦 محصول: {desc}", reply_markup=kb)
                await query.edit_message_text("✅ پرداخت تایید شد.")
        elif data.startswith("reject_"):
            payment_id = int(data.split("_")[1])
            await update_payment_status(payment_id, "rejected")
            await query.edit_message_text("❌ پرداخت رد شد.")
        elif data.startswith("send_config_"):
            payment_id = int(data.split("_")[-1])
            await query.edit_message_reply_markup(reply_markup=None)
            await query.edit_message_text("✅ در انتظار دریافت کانفیگ...")
            await context.bot.send_message(ADMIN_ID, f"📤 برای پرداخت #{payment_id} کانفیگ را ارسال کنید:")
            user_states[ADMIN_ID] = f"awaiting_config_{payment_id}"
        elif data == "admin_balance_action":
            await query.edit_message_text("🆔 ایدی کاربر را وارد کنید:")
            user_states[ADMIN_ID] = "awaiting_admin_user_id_for_balance"
        elif data == "admin_agent_action":
            await query.edit_message_text("🆔 ایدی کاربر را وارد کنید:")
            user_states[ADMIN_ID] = "awaiting_admin_user_id_for_agent"
        elif data == "admin_remove_user_action":
            await query.edit_message_text("🆔 ایدی کاربر را وارد کنید:")
            user_states[ADMIN_ID] = "awaiting_user_id_for_removal"
    except Exception as e:
        logging.error(f"Error in callback: {e}")
        await query.edit_message_text("⚠️ خطا.")

# ---------- هندلر اصلی پیام‌ها (ترتیب صحیح) ----------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text if update.message.text else ""
    state = user_states.get(user_id)
    
    if text in ["بازگشت به منو", "⬅️ بازگشت به منو"]:
        await update.message.reply_text("🌐 منوی اصلی:", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return
    
    # IMPORTANT: وضعیت‌های خاص باید به ترتیب درست بررسی شوند
    # اول وضعیت‌هایی که با کلمه خاص شروع می‌شوند
    
    if state == "awaiting_user_id_for_removal":
        await handle_remove_user(update, context, user_id, text)
        return
    if state == "awaiting_backup_file":
        await update.message.reply_text("✅ بکاپ دریافت شد.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return
    if state == "awaiting_deposit_amount" and text.isdigit():
        amount = int(text)
        payment_id = await add_payment(user_id, amount, "increase_balance", "card_to_card")
        if payment_id:
            await update.message.reply_text(f"لطفا {amount:,} تومان واریز کنید:\n\n🏦 شماره کارت:\n`{BANK_CARD}`\nبحق", reply_markup=get_back_keyboard(), parse_mode="MarkdownV2")
            user_states[user_id] = f"awaiting_deposit_receipt_{payment_id}"
        else:
            await update.message.reply_text("⚠️ خطا.", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
        return
    if state and state.startswith("awaiting_config_count_"):
        await handle_config_count(update, context, user_id, state, text)
        return
    if state and state.startswith("awaiting_deposit_receipt_"):
        payment_id = int(state.split("_")[-1])
        await process_payment_receipt(update, context, user_id, payment_id, "deposit")
        user_states.pop(user_id, None)
        return
    if state and state.startswith("awaiting_subscription_receipt_"):
        payment_id = int(state.split("_")[-1])
        await process_payment_receipt(update, context, user_id, payment_id, "subscription")
        user_states.pop(user_id, None)
        return
    if state and state.startswith("awaiting_config_"):
        payment_id = int(state.split("_")[-1])
        await process_config(update, context, user_id, payment_id)
        user_states.pop(user_id, None)
        return
    if state and state.startswith("awaiting_coupon_code_"):
        await handle_coupon_code(update, context, user_id, state, text)
        return
    if state and state.startswith("awaiting_payment_method_"):
        await handle_payment_method(update, context, user_id, text)
        return
    if state == "awaiting_coupon_discount" and user_id == ADMIN_ID:
        if text.isdigit():
            discount = int(text)
            code = generate_coupon_code()
            user_states[user_id] = f"awaiting_coupon_recipient_{code}_{discount}"
            await update.message.reply_text(f"💵 کد تخفیف `{code}` با {discount}% تخفیف ایجاد شد.\nبرای چه کسانی ارسال شود؟", reply_markup=get_coupon_recipient_keyboard(), parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ عدد وارد کنید.", reply_markup=get_back_keyboard())
        return
    if state and state.startswith("awaiting_coupon_recipient_") and user_id == ADMIN_ID:
        await handle_coupon_recipient(update, context, user_id, state, text)
        return
    if state == "awaiting_notification_type" and user_id == ADMIN_ID:
        if text == "📢 پیام به همه کاربران":
            user_states[user_id] = "awaiting_notification_text_all"
            await update.message.reply_text("📢 متن اطلاعیه را ارسال کنید:", reply_markup=get_back_keyboard())
        elif text == "🧑‍💼 پیام به نمایندگان":
            user_states[user_id] = "awaiting_notification_text_agents"
            await update.message.reply_text("📢 متن اطلاعیه را ارسال کنید:", reply_markup=get_back_keyboard())
        elif text == "👤 پیام به یک نفر":
            user_states[user_id] = "awaiting_notification_target_user"
            await update.message.reply_text("🆔 ایدی کاربر را وارد کنید:", reply_markup=get_back_keyboard())
        elif text == "⬅️ بازگشت به منو":
            await update.message.reply_text("🌐 منوی اصلی:", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
        return
    if state == "awaiting_notification_target_user" and user_id == ADMIN_ID:
        try:
            target = int(text)
            user_states[user_id] = f"awaiting_notification_text_single_{target}"
            await update.message.reply_text("📢 متن اطلاعیه را ارسال کنید:", reply_markup=get_back_keyboard())
        except:
            await update.message.reply_text("⚠️ ایدی نامعتبر.", reply_markup=get_back_keyboard())
        return
    if state in ["awaiting_notification_text_all", "awaiting_notification_text_agents"] or (state and state.startswith("awaiting_notification_text_single_")):
        if state == "awaiting_notification_text_all":
            users = await db_execute("SELECT user_id FROM users", fetch=True)
            user_type = "همه کاربران"
        elif state == "awaiting_notification_text_agents":
            users = await db_execute("SELECT user_id FROM users WHERE is_agent = TRUE", fetch=True)
            user_type = "نمایندگان"
        else:
            target = int(state.split("_")[-1])
            users = [[target]]
            user_type = f"کاربر {target}"
        sent, failed, _ = await send_notification_to_users(context, users, text)
        await update.message.reply_text(f"✅ اطلاعیه به {sent} {user_type} ارسال شد. ({failed} ناموفق)", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return
    if state == "awaiting_admin_user_id_for_balance" and user_id == ADMIN_ID:
        try:
            target = int(text)
            user_states[user_id] = f"awaiting_balance_amount_{target}"
            await update.message.reply_text("💰 مبلغ را وارد کنید:", reply_markup=get_back_keyboard())
        except:
            await update.message.reply_text("⚠️ ایدی نامعتبر.", reply_markup=get_back_keyboard())
        return
    if state and state.startswith("awaiting_balance_amount_") and user_id == ADMIN_ID:
        parts = state.split("_")
        target = int(parts[3])
        try:
            amount = int(text)
            await add_balance(target, amount)
            await update.message.reply_text(f"✅ {amount:,} تومان به موجودی کاربر {target} اضافه شد.", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
        except:
            await update.message.reply_text("⚠️ مبلغ نامعتبر.", reply_markup=get_back_keyboard())
        return
    if state == "awaiting_admin_user_id_for_agent" and user_id == ADMIN_ID:
        try:
            target = int(text)
            user_states[user_id] = f"awaiting_agent_type_{target}"
            kb = ReplyKeyboardMarkup([[KeyboardButton("ساده")], [KeyboardButton("نماینده")], [KeyboardButton("انصراف")]], resize_keyboard=True)
            await update.message.reply_text("نوع جدید را انتخاب کنید:", reply_markup=kb)
        except:
            await update.message.reply_text("⚠️ ایدی نامعتبر.", reply_markup=get_back_keyboard())
        return
    if state and state.startswith("awaiting_agent_type_") and user_id == ADMIN_ID:
        parts = state.split("_")
        target = int(parts[3])
        if text == "ساده":
            await unset_user_agent(target)
            await update.message.reply_text(f"✅ کاربر {target} به ساده تغییر یافت.", reply_markup=get_main_keyboard())
        elif text == "نماینده":
            await set_user_agent(target)
            await update.message.reply_text(f"✅ کاربر {target} به نماینده تغییر یافت.", reply_markup=get_main_keyboard())
        elif text == "انصراف":
            await update.message.reply_text("❌ لغو شد.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return
    
    await handle_normal_commands(update, context, user_id, text)

# ---------- ثبت هندلرها ----------
application.add_handler(CommandHandler("start", start_with_param))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(CommandHandler("user_info", user_info_command))
application.add_handler(CommandHandler("coupon", coupon_command))
application.add_handler(CommandHandler("notification", notification_command))
application.add_handler(CommandHandler("backup", backup_command))
application.add_handler(CommandHandler("restore", restore_command))
application.add_handler(CommandHandler("remove_user", remove_user_command))
application.add_handler(CommandHandler("cleardb", clear_db))
application.add_handler(CommandHandler("debug_subscriptions", debug_subscriptions))
application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_handler))
application.add_handler(CallbackQueryHandler(admin_callback_handler))

# ---------- webhook ----------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return {"ok": True}
    except Exception as e:
        logging.error(f"Error in webhook: {e}")
        return {"ok": False, "error": str(e)}

# ---------- lifecycle ----------
@app.on_event("startup")
async def on_startup():
    try:
        init_db_pool()
        await create_tables()
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"✅ Webhook set: {WEBHOOK_URL}")
        await set_bot_commands()
        await application.bot.send_message(chat_id=ADMIN_ID, text="🤖 ربات تیز VPN راه‌اندازی شد!")
        logging.info("✅ Bot started")
    except Exception as e:
        logging.error(f"Startup error: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await application.stop()
        await application.shutdown()
        close_db_pool()
        logging.info("✅ Bot shut down")
    except Exception as e:
        logging.error(f"Shutdown error: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
