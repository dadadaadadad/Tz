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

RENDER_BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RAILWAY_STATIC_URL") or "https://teazvpn.railway.app"
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"{RENDER_BASE_URL}{WEBHOOK_PATH}"

# تنظیمات لاگینگ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8") if os.path.exists("/tmp") else logging.StreamHandler()
    ]
)

app = FastAPI()

# ---------- endpoint سلامت برای Railway ----------
@app.get("/")
async def health_check():
    return {"status": "up", "message": "Bot is running!", "timestamp": datetime.now().isoformat()}

@app.get("/health")
async def health():
    try:
        await db_execute("SELECT 1", fetchone=True)
        return {
            "status": "healthy",
            "database": "connected",
            "bot": "running",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

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
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            sslmode='require'
        )
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
        logging.error(f"Database error in query '{query}' with params {params}: {e}")
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
        logging.error(f"Async database error in query '{query}' with params {params}: {e}")
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

UPDATE subscriptions SET start_date = COALESCE(start_date, CURRENT_TIMESTAMP),
                        duration_days = CASE
                            WHEN plan = '⭐️ کانفیگ تانل ویژه | گیگی ۸۵۰' THEN 30
                            ELSE 30
                        END
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

# ---------- دستورات ادمین ----------
async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    await update.message.reply_text("🆔 ایدی عددی کاربری که می‌خواهید حذف کنید را وارد کنید:")
    user_states[update.effective_user.id] = "awaiting_user_id_for_removal"

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    try:
        await update.message.reply_text("🔄 در حال تهیه بکاپ از دیتابیس...")
        with tempfile.NamedTemporaryFile(suffix='.sql', delete=False) as tmp_file:
            backup_file = tmp_file.name
        parsed_url = urllib.parse.urlparse(DATABASE_URL)
        db_name = parsed_url.path[1:]
        db_user = parsed_url.username
        db_password = parsed_url.password
        db_host = parsed_url.hostname
        db_port = parsed_url.port or 5432
        env = os.environ.copy()
        env['PGPASSWORD'] = db_password
        cmd = f'pg_dump -h {db_host} -p {db_port} -U {db_user} -d {db_name} -f {backup_file}'
        process = subprocess.Popen(cmd, shell=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            error_msg = stderr.decode('utf-8') if stderr else "Unknown error"
            raise Exception(f"Backup failed: {error_msg}")
        with open(backup_file, 'rb') as file:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=file,
                filename=f"teazvpn_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql",
                caption="✅ بکاپ از دیتابیس با موفقیت تهیه شد."
            )
        os.unlink(backup_file)
        await update.message.reply_text("✅ بکاپ با موفقیت تهیه و ارسال شد.")
    except Exception as e:
        logging.error(f"Error in backup command: {e}")
        await update.message.reply_text(f"⚠️ خطا در تهیه بکاپ: {str(e)}")

async def restore_database_from_backup(file_path: str):
    try:
        parsed_url = urllib.parse.urlparse(DATABASE_URL)
        db_name = parsed_url.path[1:]
        db_user = parsed_url.username
        db_password = parsed_url.password
        db_host = parsed_url.hostname
        db_port = parsed_url.port or 5432
        env = os.environ.copy()
        env['PGPASSWORD'] = db_password
        cmd = f'psql -h {db_host} -p {db_port} -U {db_user} -d {db_name} -f {file_path}'
        process = subprocess.Popen(cmd, shell=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            error_msg = stderr.decode('utf-8') if stderr else "Unknown error"
            raise Exception(f"Restore failed: {error_msg}")
        return True, "✅ دیتابیس با موفقیت بازیابی شد."
    except Exception as e:
        logging.error(f"Error restoring database: {e}")
        return False, f"⚠️ خطا در بازیابی دیتابیس: {str(e)}"

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    await update.message.reply_text("📤 لطفا فایل بکاپ دیتابیس را ارسال کنید:")
    user_states[update.effective_user.id] = "awaiting_backup_file"

async def send_notification_to_users(context, user_ids, notification_text):
    sent_count = 0
    failed_count = 0
    failed_users = []
    tasks = []
    for user_id in user_ids:
        task = context.bot.send_message(
            chat_id=user_id[0],
            text=f"📢 اطلاعیه از مدیریت:\n\n{notification_text}"
        )
        tasks.append(task)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        user_id = user_ids[i][0]
        if isinstance(result, Exception):
            failed_count += 1
            failed_users.append(user_id)
            logging.error(f"Error sending notification to user_id {user_id}: {result}")
        else:
            sent_count += 1
    return sent_count, failed_count, failed_users

async def notify_admin_new_user(user_id, username, invited_by=None):
    try:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username_display = f"@{username}" if username else "بدون یوزرنیم"
        invited_by_text = f"با دعوت کاربر {invited_by}" if invited_by and invited_by != user_id else "مستقیم"
        total_users = await db_execute("SELECT COUNT(*) FROM users", fetchone=True)
        total_users_count = total_users[0] if total_users else 0
        message = (
            "🆕 **کاربر جدید به ربات اضافه شد!**\n\n"
            f"🆔 ایدی عددی: `{user_id}`\n"
            f"📛 یوزرنیم: {username_display}\n"
            f"🕒 زمان عضویت: {current_time}\n"
            f"🎯 روش ورود: {invited_by_text}\n"
            f"📊 تعداد کل کاربران: {total_users_count} نفر"
        )
        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            parse_mode="Markdown"
        )
        logging.info(f"Admin notified about new user: {user_id} (@{username})")
    except Exception as e:
        logging.error(f"Error notifying admin about new user {user_id}: {e}")

async def notification_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    keyboard = [
        [KeyboardButton("📢 پیام به همه کاربران")],
        [KeyboardButton("🧑‍💼 پیام به نمایندگان")],
        [KeyboardButton("👤 پیام به یک نفر")],
        [KeyboardButton("⬅️ بازگشت به منو")]
    ]
    await update.message.reply_text(
        "📢 نوع اطلاع‌رسانی را انتخاب کنید:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    user_states[update.effective_user.id] = "awaiting_notification_type"

async def coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    await update.message.reply_text("💵 مقدار تخفیف را به درصد وارد کنید (مثال: 20):")
    user_states[update.effective_user.id] = "awaiting_coupon_discount"

async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    try:
        users = await db_execute(
            "SELECT user_id, username, phone, balance, is_agent, created_at, is_new_user FROM users ORDER BY created_at DESC",
            fetch=True
        )
        if not users:
            await update.message.reply_text("📂 هیچ کاربری یافت نشد.")
            return
        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 افزایش/کاهش موجودی", callback_data="admin_balance_action")],
            [InlineKeyboardButton("🧑‍💼 تغییر نوع اکانت", callback_data="admin_agent_action")],
            [InlineKeyboardButton("🗑️ حذف کاربر", callback_data="admin_remove_user_action")]
        ])
        response = "👥 لیست کامل اطلاعات کاربران:\n\n"
        max_length = 4000
        parts = []
        current_part = response
        for user in users:
            user_id, username, phone, balance, is_agent, created_at, is_new_user = user
            invited_count = await db_execute(
                "SELECT COUNT(*) FROM users WHERE invited_by = %s",
                (user_id,), fetchone=True
            )
            invited_count = invited_count[0] if invited_count else 0
            agent_status = "نماینده" if is_agent else "ساده"
            user_status = "🆕 جدید" if is_new_user else "قدیمی"
            phone_display = phone if phone else "نامشخص"
            username_display = f"@{username}" if username else "بدون یوزرنیم"
            created_at_str = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "نامشخص"
            user_info = (
                f"🆔 ایدی عددی: {user_id}\n"
                f"📛 یوزرنیم: {username_display}\n"
                f"📞 شماره تلفن: {phone_display}\n"
                f"💰 موجودی: {balance:,} تومان\n"
                f"🆙 نوع اکانت: {agent_status}\n"
                f"📊 وضعیت: {user_status}\n"
                f"📅 تاریخ ایجاد: {created_at_str}\n"
                f"👥 دعوت شدگان: {invited_count} نفر\n"
                "--------------------\n\n"
            )
            if len(current_part + user_info) > max_length:
                parts.append(current_part)
                current_part = user_info
            else:
                current_part += user_info
        if current_part:
            parts.append(current_part)
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=part,
                    reply_markup=inline_kb
                )
            else:
                await context.bot.send_message(chat_id=ADMIN_ID, text=part)
    except Exception as e:
        logging.error(f"Error in user_info_command: {e}")
        await update.message.reply_text("⚠️ خطایی در نمایش اطلاعات کاربران رخ داد. لطفاً دوباره تلاش کنید.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    try:
        total_users = await db_execute("SELECT COUNT(*) FROM users", fetchone=True)
        new_users = await db_execute("SELECT COUNT(*) FROM users WHERE is_new_user = TRUE", fetchone=True)
        active_users = await db_execute("SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE status = 'active' AND config IS NOT NULL", fetchone=True)
        inactive_users = total_users[0] - active_users[0] if total_users and active_users else 0
        today_users = await db_execute("SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE", fetchone=True)
        today_income = await db_execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'approved' AND created_at >= CURRENT_DATE", fetchone=True)
        month_income = await db_execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'approved' AND created_at >= DATE_TRUNC('month', CURRENT_DATE)", fetchone=True)
        total_income = await db_execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'approved'", fetchone=True)
        plan_stats = await db_execute("SELECT plan, COUNT(*) as count FROM subscriptions WHERE config IS NOT NULL AND status = 'active' GROUP BY plan ORDER BY count DESC", fetch=True)
        best_selling_plan = plan_stats[0] if plan_stats else ("هیچ پلنی", 0)
        payment_methods = await db_execute("SELECT payment_method, COUNT(*) as count FROM payments WHERE status = 'approved' GROUP BY payment_method", fetch=True)
        total_payments = sum([pm[1] for pm in payment_methods]) if payment_methods else 1
        payment_methods_percent = [
            (pm[0], round((pm[1] / total_payments) * 100, 1)) 
            for pm in payment_methods
            if pm[0] in ["card_to_card", "balance"]
        ] if payment_methods else [("کارت به کارت", 0), ("موجودی", 0)]
        method_names = {
            "card_to_card": "🏦 کارت به کارت",
            "balance": "💰 موجودی"
        }
        total_subs = await db_execute("SELECT COUNT(*) FROM subscriptions", fetchone=True)
        active_subs = await db_execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND config IS NOT NULL", fetchone=True)
        pending_subs = await db_execute("SELECT COUNT(*) FROM payments WHERE status = 'pending' AND type = 'buy_subscription'", fetchone=True)
        total_transactions = await db_execute("SELECT COUNT(*) FROM payments", fetchone=True)
        invited_users = await db_execute("SELECT COUNT(*) FROM users WHERE invited_by IS NOT NULL", fetchone=True)
        stats_message = "🌟 گزارش عملکرد تیز VPN 🚀\n\n"
        stats_message += "👥 کاربران:\n"
        stats_message += f"  • کل کاربران: {total_users[0] if total_users else 0:,} نفر 🧑‍💻\n"
        stats_message += f"  • کاربران جدید: {new_users[0] if new_users else 0:,} نفر 🆕\n"
        stats_message += f"  • کاربران فعال: {active_users[0] if active_users else 0:,} نفر ✅\n"
        stats_message += f"  • کاربران غیرفعال: {inactive_users:,} نفر ❎\n"
        stats_message += f"  • کاربران جدید امروز: {today_users[0] if today_users else 0:,} نفر 📈\n"
        stats_message += f"  • کاربران دعوت‌شده: {invited_users[0] if invited_users else 0:,} نفر 🤝\n\n"
        stats_message += "💸 درآمد:\n"
        stats_message += f"  • امروز: {today_income[0] if today_income else 0:,} تومان 💰\n"
        stats_message += f"  • این ماه: {month_income[0] if month_income else 0:,} تومان 📈\n"
        stats_message += f"  • کل درآمد: {total_income[0] if total_income else 0:,} تومان 🔥\n\n"
        stats_message += "📦 اشتراک‌ها:\n"
        stats_message += f"  • کل اشتراک‌ها: {total_subs[0] if total_subs else 0:,} عدد 📋\n"
        stats_message += f"  • اشتراک‌های فعال: {active_subs[0] if active_subs else 0:,} عدد 🟢\n"
        stats_message += f"  • اشتراک‌های در انتظار: {pending_subs[0] if pending_subs else 0:,} عدد ⏳\n"
        stats_message += f"  • پرفروش‌ترین پلن: {best_selling_plan[0]} ({best_selling_plan[1]:,} عدد) 🏆\n\n"
        stats_message += "💳 روش‌های پرداخت:\n"
        for method, percent in payment_methods_percent:
            display_name = method_names.get(method, method)
            stats_message += f"  • {display_name}: {percent}% 💸\n"
        stats_message += f"  • کل تراکنش‌ها: {total_transactions[0] if total_transactions else 0:,} عدد 🔄\n"
        await update.message.reply_text(stats_message)
    except Exception as e:
        logging.error(f"Error generating stats: {e}")
        await update.message.reply_text("⚠️ خطایی در نمایش آمار رخ داد. لطفاً دوباره تلاش کنید.")

async def clear_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    try:
        await db_execute("DELETE FROM coupons")
        await db_execute("DELETE FROM subscriptions")
        await db_execute("DELETE FROM payments")
        await db_execute("DELETE FROM users")
        logging.info("Database cleared successfully by admin")
        await update.message.reply_text("✅ دیتابیس با موفقیت پاک شد.")
    except Exception as e:
        logging.error(f"Error clearing database: {e}")
        await update.message.reply_text(f"⚠️ خطا در پاک کردن دیتابیس: {str(e)}")

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
    keyboard = [
        [KeyboardButton("نمایش موجودی"), KeyboardButton("افزایش موجودی")],
        [KeyboardButton("بازگشت به منو")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_back_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)

def get_subscription_keyboard():
    keyboard = [
        [KeyboardButton("⭐️ کانفیگ تانل ویژه | گیگی ۸۵۰")],
        [KeyboardButton("⬅️ بازگشت به منو")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_payment_method_keyboard():
    keyboard = [
        [KeyboardButton("🏦 کارت به کارت")],
        [KeyboardButton("💰 پرداخت با موجودی")],
        [KeyboardButton("⬅️ بازگشت به منو")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_connection_guide_keyboard():
    keyboard = [
        [KeyboardButton("📗 اندروید")],
        [KeyboardButton("📕 آیفون/مک")],
        [KeyboardButton("📘 ویندوز")],
        [KeyboardButton("📙 لینوکس")],
        [KeyboardButton("⬅️ بازگشت به منو")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_coupon_recipient_keyboard():
    keyboard = [
        [KeyboardButton("📢 برای همه")],
        [KeyboardButton("👤 برای یک نفر")],
        [KeyboardButton("🎯 درصد خاصی از کاربران")],
        [KeyboardButton("⬅️ بازگشت به منو")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_notification_type_keyboard():
    keyboard = [
        [KeyboardButton("📢 پیام به همه کاربران")],
        [KeyboardButton("🧑‍💼 پیام به نمایندگان")],
        [KeyboardButton("👤 پیام به یک نفر")],
        [KeyboardButton("⬅️ بازگشت به منو")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------- تابع کمکی برای ارسال پیام‌های طولانی ----------
async def send_long_message(chat_id, text, context, reply_markup=None, parse_mode=None):
    max_message_length = 4000
    if len(text) <= max_message_length:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    messages = []
    current_message = ""
    for line in text.split("\n"):
        if len(current_message) + len(line) + 1 > max_message_length:
            messages.append(current_message)
            current_message = line + "\n"
        else:
            current_message += line + "\n"
    if current_message:
        messages.append(current_message)
    for i, msg in enumerate(messages):
        if i == len(messages) - 1:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                reply_markup=reply_markup if i == len(messages) - 1 else None,
                parse_mode=parse_mode
            )
        else:
            await context.bot.send_message(chat_id=chat_id, text=msg)

# ---------- توابع DB برای کوپن‌ها ----------
async def create_coupon(code, discount_percent, user_id=None):
    try:
        await db_execute(
            "INSERT INTO coupons (code, discount_percent, user_id, is_used) VALUES (%s, %s, %s, FALSE)",
            (code, discount_percent, user_id)
        )
        logging.info(f"Coupon {code} created with {discount_percent}% discount for user_id {user_id or 'all'}")
    except Exception as e:
        logging.error(f"Error creating coupon {code}: {e}")
        raise

async def validate_coupon(code, user_id):
    try:
        row = await db_execute(
            "SELECT discount_percent, user_id, is_used, expiry_date FROM coupons WHERE code = %s",
            (code,), fetchone=True
        )
        if not row:
            return None, "کد تخفیف نامعتبر است."
        discount_percent, coupon_user_id, is_used, expiry_date = row
        if is_used:
            return None, "این کد تخفیف قبلاً استفاده شده است."
        if datetime.now() > expiry_date:
            return None, "این کد تخفیف منقضی شده است."
        if coupon_user_id is not None and coupon_user_id != user_id:
            return None, "این کد تخفیف برای شما نیست."
        if await is_user_agent(user_id):
            return None, "نمایندگان نمی‌توانند از کد تخفیف استفاده کنند."
        return discount_percent, None
    except Exception as e:
        logging.error(f"Error validating coupon {code} for user_id {user_id}: {e}")
        return None, "خطا در بررسی کد تخفیف."

async def mark_coupon_used(code):
    try:
        await db_execute("UPDATE coupons SET is_used = TRUE WHERE code = %s", (code,))
        logging.info(f"Coupon {code} marked as used")
    except Exception as e:
        logging.error(f"Error marking coupon {code} as used: {e}")

# ---------- تابع برای حذف کامل کاربر از دیتابیس ----------
async def remove_user_from_db(user_id):
    try:
        await db_execute("DELETE FROM coupons WHERE user_id = %s", (user_id,))
        await db_execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
        await db_execute("DELETE FROM payments WHERE user_id = %s", (user_id,))
        await db_execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        logging.info(f"User {user_id} completely removed from database")
        return True
    except Exception as e:
        logging.error(f"Error removing user {user_id} from database: {e}")
        return False

# ---------- توابع DB موجود ----------
async def is_user_member(user_id):
    try:
        member = await application.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Error checking channel membership for user {user_id}: {e}")
        return False

async def ensure_user(user_id, username, invited_by=None):
    try:
        row = await db_execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        if not row:
            await db_execute(
                "INSERT INTO users (user_id, username, invited_by, is_agent, is_new_user) VALUES (%s, %s, %s, FALSE, TRUE)",
                (user_id, username, invited_by)
            )
            await notify_admin_new_user(user_id, username, invited_by)
            if invited_by and invited_by != user_id:
                inviter = await db_execute("SELECT user_id FROM users WHERE user_id = %s", (invited_by,), fetchone=True)
                if inviter:
                    await add_balance(invited_by, 10000)
            logging.info(f"NEW user {user_id} registered in database")
        elif row:
            await db_execute("UPDATE users SET is_new_user = FALSE WHERE user_id = %s", (user_id,))
            logging.info(f"Existing user {user_id} marked as non-new")
    except Exception as e:
        logging.error(f"Error ensuring user {user_id}: {e}")

async def set_user_agent(user_id):
    try:
        await db_execute("UPDATE users SET is_agent = TRUE WHERE user_id = %s", (user_id,))
        logging.info(f"User {user_id} set as agent")
    except Exception as e:
        logging.error(f"Error setting user {user_id} as agent: {e}")

async def unset_user_agent(user_id):
    try:
        await db_execute("UPDATE users SET is_agent = FALSE WHERE user_id = %s", (user_id,))
        logging.info(f"User {user_id} unset as agent")
    except Exception as e:
        logging.error(f"Error unsetting user {user_id} as agent: {e}")

async def is_user_agent(user_id):
    try:
        row = await db_execute("SELECT is_agent FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        return row[0] if row and row[0] is not None else False
    except Exception as e:
        logging.error(f"Error checking agent status for user_id {user_id}: {e}")
        return False

async def save_user_phone(user_id, phone):
    try:
        await db_execute("UPDATE users SET phone = %s WHERE user_id = %s", (phone, user_id))
        logging.info(f"Phone saved for user_id {user_id}")
    except Exception as e:
        logging.error(f"Error saving user phone for user_id {user_id}: {e}")
        return None

async def get_user_phone(user_id):
    try:
        row = await db_execute("SELECT phone FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        return row[0] if row else None
    except Exception as e:
        logging.error(f"Error getting user phone for user_id {user_id}: {e}")
        return None

async def add_balance(user_id, amount):
    try:
        await db_execute("UPDATE users SET balance = COALESCE(balance,0) + %s WHERE user_id = %s", (amount, user_id))
        logging.info(f"Added {amount} to balance for user_id {user_id}")
    except Exception as e:
        logging.error(f"Error adding balance for user_id {user_id}: {e}")

async def deduct_balance(user_id, amount):
    try:
        await db_execute("UPDATE users SET balance = COALESCE(balance,0) - %s WHERE user_id = %s", (amount, user_id))
        logging.info(f"Deducted {amount} from balance for user_id {user_id}")
    except Exception as e:
        logging.error(f"Error deducting balance for user_id {user_id}: {e}")

async def get_balance(user_id):
    try:
        row = await db_execute("SELECT balance FROM users WHERE user_id = %s", (user_id,), fetchone=True)
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logging.error(f"Error getting balance for user_id {user_id}: {e}")
        return 0

async def add_payment(user_id, amount, ptype, payment_method, description="", coupon_code=None):
    try:
        query = "INSERT INTO payments (user_id, amount, status, type, payment_method, description) VALUES (%s, %s, 'pending', %s, %s, %s) RETURNING id"
        new_id = await db_execute(query, (user_id, amount, ptype, payment_method, description), returning=True)
        if coupon_code:
            await mark_coupon_used(coupon_code)
        logging.info(f"Payment added for user_id {user_id}, amount: {amount}, type: {ptype}, payment_method: {payment_method}, id: {new_id}")
        return int(new_id) if new_id is not None else None
    except Exception as e:
        logging.error(f"Error adding payment for user_id {user_id}: {e}")
        return None

async def add_subscription(user_id, payment_id, plan):
    try:
        duration_mapping = {
            "⭐️ کانفیگ تانل ویژه | گیگی ۸۵۰": 30
        }
        duration_days = duration_mapping.get(plan, 30)
        await db_execute(
            "INSERT INTO subscriptions (user_id, payment_id, plan, status, start_date, duration_days) VALUES (%s, %s, %s, 'pending', CURRENT_TIMESTAMP, %s)",
            (user_id, payment_id, plan, duration_days)
        )
        logging.info(f"Subscription added for user_id {user_id}, payment_id: {payment_id}, plan: {plan}, duration: {duration_days} days, status: pending")
    except Exception as e:
        logging.error(f"Error adding subscription for user_id {user_id}, payment_id: {payment_id}: {e}")
        raise

async def update_subscription_config(payment_id, config):
    try:
        await db_execute(
            "UPDATE subscriptions SET config = %s, status = 'active' WHERE payment_id = %s",
            (config, payment_id)
        )
        logging.info(f"Subscription config updated and set to active for payment_id {payment_id}")
    except Exception as e:
        logging.error(f"Error updating subscription config for payment_id {payment_id}: {e}")

async def update_payment_status(payment_id, status):
    try:
        await db_execute("UPDATE payments SET status = %s WHERE id = %s", (status, payment_id))
        logging.info(f"Payment status updated to {status} for payment_id {payment_id}")
    except Exception as e:
        logging.error(f"Error updating payment status for payment_id {payment_id}: {e}")

async def get_user_subscriptions(user_id):
    try:
        rows = await db_execute(
            """
            SELECT s.id, s.plan, s.config, s.status, s.payment_id, s.start_date, s.duration_days, u.username
            FROM subscriptions s
            LEFT JOIN users u ON s.user_id = u.user_id
            WHERE s.user_id = %s
            ORDER BY s.status DESC, s.start_date DESC
            """,
            (user_id,), fetch=True
        )
        logging.info(f"Fetched {len(rows)} subscriptions for user_id {user_id}")
        current_time = datetime.now()
        subscriptions = []
        for row in rows:
            try:
                sub_id, plan, config, status, payment_id, start_date, duration_days, username = row
                start_date = start_date or current_time
                duration_days = duration_days or 30
                username = username or str(user_id)
                if status == "active":
                    end_date = start_date + timedelta(days=duration_days)
                    if current_time > end_date:
                        await db_execute("UPDATE subscriptions SET status = 'inactive' WHERE id = %s", (sub_id,))
                        status = "inactive"
                subscriptions.append({
                    'id': sub_id,
                    'plan': plan,
                    'config': config,
                    'status': status,
                    'payment_id': payment_id,
                    'start_date': start_date,
                    'duration_days': duration_days,
                    'username': username,
                    'end_date': start_date + timedelta(days=duration_days)
                })
            except Exception as e:
                logging.error(f"Error processing subscription {sub_id} for user_id {user_id}: {e}")
                continue
        logging.info(f"Processed {len(subscriptions)} subscriptions for user_id {user_id}")
        return subscriptions
    except Exception as e:
        logging.error(f"Error in get_user_subscriptions for user_id {user_id}: {e}")
        return []

# ---------- دستور تشخیصی برای ادمین ----------
async def debug_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ شما اجازه دسترسی به این دستور را ندارید.")
        return
    try:
        rows = await db_execute(
            """
            SELECT s.user_id, u.username, s.plan, s.payment_id, s.start_date, s.duration_days, s.status
            FROM subscriptions s
            LEFT JOIN users u ON s.user_id = u.user_id
            ORDER BY s.status DESC, s.start_date DESC
            """,
            fetch=True
        )
        if not rows:
            await update.message.reply_text("📂 هیچ اشتراکی برای هیچ کاربری یافت نشد.")
            return
        response = "📂 لیست تمام اشتراک‌های کاربران:\n\n"
        current_time = datetime.now()
        for row in rows:
            user_id, username, plan, payment_id, start_date, duration_days, status = row
            username_display = f"@{username}" if username else f"@{user_id}"
            start_date = start_date if start_date else current_time
            duration_days = duration_days if duration_days else 30
            remaining_days = 0
            if status == "active":
                end_date = start_date + timedelta(days=duration_days)
                remaining_days = max(0, (end_date - current_time).days)
            response += f"کاربر: {username_display}\n"
            response += f"اشتراک: {plan}\n"
            response += f"کد خرید: #{payment_id}\n"
            response += f"وضعیت: {'فعال' if status == 'active' else 'غیرفعال'}\n"
            response += f"زمان باقی‌مانده: {remaining_days} روز\n"
            response += "--------------------\n"
        await send_long_message(update.effective_user.id, response, context)
    except Exception as e:
        logging.error(f"Error in debug_subscriptions: {e}")
        await update.message.reply_text(f"⚠️ خطا در بررسی اشتراک‌ها: {str(e)}")

# ---------- وضعیت کاربر در مموری ----------
user_states = {}

def generate_coupon_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ---------- دستورات و هندلرها ----------
async def set_bot_commands():
    try:
        public_commands = [
            BotCommand(command="/start", description="شروع ربات")
        ]
        admin_commands = [
            BotCommand(command="/start", description="شروع ربات"),
            BotCommand(command="/debug_subscriptions", description="تشخیص اشتراک‌ها (ادمین)"),
            BotCommand(command="/cleardb", description="پاک کردن دیتابیس (ادمین)"),
            BotCommand(command="/stats", description="آمار ربات (ادمین)"),
            BotCommand(command="/user_info", description="اطلاعات کاربران (ادمین)"),
            BotCommand(command="/coupon", description="ایجاد کد تخفیف (ادمین)"),
            BotCommand(command="/notification", description="ارسال اطلاعیه به کاربران (ادمین)"),
            BotCommand(command="/backup", description="تهیه بکاپ از دیتابیس (ادمین)"),
            BotCommand(command="/restore", description="بازیابی دیتابیس از بکاپ (ادمین)"),
            BotCommand(command="/remove_user", description="حذف کاربر از دیتابیس (ادمین)")
        ]
        await application.bot.set_my_commands(public_commands)
        await application.bot.set_my_commands(admin_commands, scope={"type": "chat", "chat_id": ADMIN_ID})
        logging.info("Bot commands set successfully")
    except Exception as e:
        logging.error(f"Error setting bot commands: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    if not await is_user_member(user_id):
        kb = [[InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{CHANNEL_USERNAME.replace('@','')}")]]
        await update.message.reply_text(
            "❌ برای استفاده از ربات، ابتدا در کانال ما عضو شوید و سپس مجدد /start را بزنید.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return
    invited_by = context.user_data.get("invited_by")
    await ensure_user(user_id, username, invited_by)
    await update.message.reply_text(
        "🌐 به فروشگاه تیز VPN خوش آمدید!\n\nیک گزینه را انتخاب کنید:",
        reply_markup=get_main_keyboard()
    )
    user_states.pop(user_id, None)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text if update.message.text else ""
    logging.info(f"User {user_id} sent: '{text}', current state: {user_states.get(user_id)}")
    if text in ["بازگشت به منو", "⬅️ بازگشت به منو"]:
        await update.message.reply_text("🌐 منوی اصلی:", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return
    if user_states.get(user_id) == "awaiting_user_id_for_removal":
        await handle_remove_user(update, context, user_id, text)
        return
    if user_states.get(user_id) == "awaiting_backup_file":
        if update.message.document:
            try:
                file = await context.bot.get_file(update.message.document.file_id)
                with tempfile.NamedTemporaryFile(suffix='.sql', delete=False) as tmp_file:
                    backup_file = tmp_file.name
                await file.download_to_drive(backup_file)
                await update.message.reply_text("🔄 در حال بازیابی دیتابیس...")
                success, message = await restore_database_from_backup(backup_file)
                os.unlink(backup_file)
                if success:
                    await update.message.reply_text(message, reply_markup=get_main_keyboard())
                else:
                    await update.message.reply_text(message, reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
                return
            except Exception as e:
                logging.error(f"Error in restore process: {e}")
                await update.message.reply_text(f"⚠️ خطا در بازیابی دیتابیس: {str(e)}", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
                return
        else:
            await update.message.reply_text("⚠️ لطفا یک فایل بکاپ ارسال کنید.", reply_markup=get_back_keyboard())
            return
    state = user_states.get(user_id)
    if state and state.startswith("awaiting_deposit_receipt_"):
        payment_id = int(state.split("_")[-1])
        await process_payment_receipt(update, context, user_id, payment_id, "deposit")
        user_states.pop(user_id, None)
        return
    elif state and state.startswith("awaiting_subscription_receipt_"):
        payment_id = int(state.split("_")[-1])
        await process_payment_receipt(update, context, user_id, payment_id, "subscription")
        user_states.pop(user_id, None)
        return
    elif state and state.startswith("awaiting_config_"):
        payment_id = int(state.split("_")[-1])
        await process_config(update, context, user_id, payment_id)
        user_states.pop(user_id, None)
        return
    elif state == "awaiting_coupon_discount" and user_id == ADMIN_ID:
        if text.isdigit():
            discount_percent = int(text)
            if 1 <= discount_percent <= 100:
                coupon_code = generate_coupon_code()
                user_states[user_id] = f"awaiting_coupon_recipient_{coupon_code}_{discount_percent}"
                await update.message.reply_text(
                    f"💵 کد تخفیف `{coupon_code}` با {discount_percent}% تخفیف ایجاد شد.\nبرای چه کسانی ارسال شود؟",
                    reply_markup=get_coupon_recipient_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("⚠️ درصد تخفیف باید بین 1 تا 100 باشد.", reply_markup=get_back_keyboard())
        else:
            await update.message.reply_text("⚠️ لطفا یک عدد معتبر وارد کنید.", reply_markup=get_back_keyboard())
        return
    elif state and state.startswith("awaiting_coupon_recipient_") and user_id == ADMIN_ID:
        await handle_coupon_recipient(update, context, user_id, state, text)
        return
    elif state and state.startswith("awaiting_coupon_percent_") and user_id == ADMIN_ID:
        await handle_coupon_percent(update, context, user_id, state, text)
        return
    elif state and state.startswith("awaiting_coupon_code_"):
        await handle_coupon_code(update, context, user_id, state, text)
        return
    elif state == "awaiting_notification_type" and user_id == ADMIN_ID:
        await handle_notification_type(update, context, user_id, text)
        return
    elif state == "awaiting_notification_target_user" and user_id == ADMIN_ID:
        await handle_notification_target_user(update, context, user_id, text)
        return
    elif (state in ["awaiting_notification_text_all", "awaiting_notification_text_agents"] or 
          (state and state.startswith("awaiting_notification_text_single_"))):
        await handle_notification_text(update, context, user_id, state, text)
        return
    elif state and state.startswith("confirm_notification_") and user_id == ADMIN_ID:
        await handle_confirm_notification(update, context, user_id, state, text)
        return
    elif state == "awaiting_admin_user_id_for_balance" and user_id == ADMIN_ID:
        await handle_admin_balance_user(update, context, user_id, text)
        return
    elif state and state.startswith("awaiting_balance_amount_") and user_id == ADMIN_ID:
        await handle_admin_balance_amount(update, context, user_id, state, text)
        return
    elif state == "awaiting_admin_user_id_for_agent" and user_id == ADMIN_ID:
        await handle_admin_agent_user(update, context, user_id, text)
        return
    elif state and state.startswith("awaiting_agent_type_") and user_id == ADMIN_ID:
        await handle_admin_agent_type(update, context, user_id, state, text)
        return
    await handle_normal_commands(update, context, user_id, text)

async def handle_remove_user(update, context, user_id, text):
    try:
        target_user_id = int(text)
        user_exists = await db_execute("SELECT user_id, username FROM users WHERE user_id = %s", (target_user_id,), fetchone=True)
        if not user_exists:
            await update.message.reply_text("⚠️ کاربری با این ایدی یافت نشد.", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
            return
        username = user_exists[1] or "بدون یوزرنیم"
        keyboard = ReplyKeyboardMarkup([
            [KeyboardButton(f"✅ بله، کاربر {target_user_id} را حذف کن")],
            [KeyboardButton("❌ خیر، انصراف")]
        ], resize_keyboard=True)
        await update.message.reply_text(
            f"⚠️ آیا مطمئن هستید که می‌خواهید کاربر زیر را حذف کنید؟\n\n"
            f"🆔 ایدی عددی: {target_user_id}\n"
            f"📛 یوزرنیم: @{username}\n\n"
            "این عمل تمام اطلاعات کاربر از جمله اشتراک‌ها، پرداخت‌ها و موجودی را حذف می‌کند و قابل بازگشت نیست!",
            reply_markup=keyboard
        )
        context.user_data["pending_removal_user_id"] = target_user_id
        user_states[user_id] = "confirm_user_removal"
    except ValueError:
        await update.message.reply_text("⚠️ لطفا یک ایدی عددی معتبر وارد کنید.", reply_markup=get_back_keyboard())

async def process_payment_receipt(update, context, user_id, payment_id, receipt_type):
    try:
        payment = await db_execute("SELECT amount, type, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
        if not payment:
            await update.message.reply_text("⚠️ پرداخت یافت نشد.", reply_markup=get_main_keyboard())
            return
        amount, ptype, description = payment
        caption = f"💳 فیش پرداختی از کاربر {user_id} (@{update.effective_user.username or 'NoUsername'}):\n"
        caption += f"مبلغ: {amount}\nنوع: {ptype if ptype != 'buy_subscription' else 'خرید اشتراک'}"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید", callback_data=f"approve_{payment_id}"),
                InlineKeyboardButton("❌ رد", callback_data=f"reject_{payment_id}")
            ]
        ])
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=caption, reply_markup=keyboard)
        elif update.message.document:
            doc_id = update.message.document.file_id
            await context.bot.send_document(chat_id=ADMIN_ID, document=doc_id, caption=caption, reply_markup=keyboard)
        else:
            await update.message.reply_text("⚠️ لطفا فیش پرداخت را به صورت عکس یا فایل ارسال کنید.", reply_markup=get_back_keyboard())
            return
        await update.message.reply_text("✅ فیش شما برای ادمین ارسال شد، لطفا منتظر تایید باشید.", reply_markup=get_main_keyboard())
    except Exception as e:
        logging.error(f"Error processing payment receipt: {e}")
        await update.message.reply_text("⚠️ خطا در پردازش فیش پرداخت.", reply_markup=get_main_keyboard())

async def process_config(update, context, user_id, payment_id):
    try:
        payment = await db_execute("SELECT user_id, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
        if not payment:
            await update.message.reply_text("⚠️ پرداخت یافت نشد.", reply_markup=get_main_keyboard())
            return
        buyer_id, description = payment
        if update.message.text:
            config = update.message.text
            await update_subscription_config(payment_id, config)
            await context.bot.send_message(
                chat_id=buyer_id,
                text=f"✅ کانفیگ اشتراک شما ({description})\nکد خرید: #{payment_id}\nدریافت شد:\n```\n{config}\n```",
                parse_mode="Markdown"
            )
            await update.message.reply_text("✅ کانفیگ با موفقیت به خریدار ارسال شد.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("⚠️ لطفا کانفیگ را به صورت متن ارسال کنید.", reply_markup=get_back_keyboard())
    except Exception as e:
        logging.error(f"Error processing config: {e}")
        await update.message.reply_text("⚠️ خطا در پردازش کانفیگ.", reply_markup=get_main_keyboard())

async def handle_coupon_recipient(update, context, user_id, state, text):
    parts = state.split("_")
    coupon_code = parts[3]
    discount_percent = int(parts[4])
    if text == "📢 برای همه":
        try:
            await create_coupon(coupon_code, discount_percent)
            users = await db_execute("SELECT user_id FROM users WHERE is_agent = FALSE", fetch=True)
            if not users:
                await update.message.reply_text("⚠️ هیچ کاربری (غیر از نمایندگان) یافت نشد.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
                return
            sent_count = 0
            for user in users:
                try:
                    await context.bot.send_message(
                        chat_id=user[0],
                        text=f"🎉 کد تخفیف `{coupon_code}` با {discount_percent}% تخفیف برای شما!\n⏳ این کد فقط تا ۳ روز اعتبار دارد.\nفقط یک بار قابل استفاده است.",
                        parse_mode="Markdown"
                    )
                    sent_count += 1
                except Exception as e:
                    logging.error(f"Error sending coupon to user_id {user[0]}: {e}")
                    continue
            await update.message.reply_text(
                f"✅ کد تخفیف `{coupon_code}` برای {sent_count} کاربر (غیر از نمایندگان) ارسال شد.",
                reply_markup=get_main_keyboard(),
                parse_mode="Markdown"
            )
            user_states.pop(user_id, None)
        except Exception as e:
            logging.error(f"Error sending coupons to all users: {e}")
            await update.message.reply_text("⚠️ خطا در ارسال کد تخفیف برای همه کاربران.", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
        return
    elif text == "👤 برای یک نفر":
        user_states[user_id] = f"awaiting_single_coupon_user_{coupon_code}_{discount_percent}"
        await update.message.reply_text("🆔 ایدی عددی کاربر را وارد کنید:", reply_markup=get_back_keyboard())
        return
    elif text == "🎯 درصد خاصی از کاربران":
        user_states[user_id] = f"awaiting_coupon_percent_{coupon_code}_{discount_percent}"
        await update.message.reply_text("📊 درصد کاربران را وارد کنید (مثال: 20):", reply_markup=get_back_keyboard())
        return
    else:
        await update.message.reply_text("⚠️ لطفا یکی از گزینه‌های بالا را انتخاب کنید.", reply_markup=get_coupon_recipient_keyboard())
        return

async def handle_coupon_percent(update, context, user_id, state, text):
    parts = state.split("_")
    coupon_code = parts[3]
    discount_percent = int(parts[4])
    if text.isdigit():
        percent = int(text)
        if 1 <= percent <= 100:
            try:
                users = await db_execute("SELECT user_id FROM users WHERE is_agent = FALSE", fetch=True)
                if not users:
                    await update.message.reply_text("⚠️ هیچ کاربری (غیر از نمایندگان) یافت نشد.", reply_markup=get_main_keyboard())
                    user_states.pop(user_id, None)
                    return
                total_users = len(users)
                num_users = max(1, round(total_users * (percent / 100)))
                selected_users = random.sample(users, min(num_users, total_users))
                await create_coupon(coupon_code, discount_percent)
                sent_count = 0
                for user in selected_users:
                    try:
                        await context.bot.send_message(
                            chat_id=user[0],
                            text=f"🎉 کد تخفیف `{coupon_code}` با {discount_percent}% تخفیف برای شما!\n⏳ این کد فقط تا ۳ روز اعتبار دارد.\nفقط یک بار قابل استفاده است.",
                            parse_mode="Markdown"
                        )
                        sent_count += 1
                    except Exception as e:
                        logging.error(f"Error sending coupon to user_id {user[0]}: {e}")
                        continue
                await update.message.reply_text(
                    f"✅ کد تخفیف `{coupon_code}` برای {sent_count} کاربر ({percent}% از کاربران غیر نماینده) ارسال شد.",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown"
                )
                user_states.pop(user_id, None)
            except Exception as e:
                logging.error(f"Error sending coupons to {percent}% of users: {e}")
                await update.message.reply_text("⚠️ خطا در ارسال کد تخفیف برای درصد مشخصی از کاربران.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
        else:
            await update.message.reply_text("⚠️ درصد باید بین 1 تا 100 باشد.", reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text("⚠️ لطفا یک عدد معتبر وارد کنید.", reply_markup=get_back_keyboard())

async def handle_coupon_code(update, context, user_id, state, text):
    parts = state.split("_")
    amount = int(parts[3])
    plan = "_".join(parts[4:]) if len(parts) <= 5 else "_".join(parts[4:-1])
    if text == "ادامه":
        user_states[user_id] = f"awaiting_payment_method_{amount}_{plan}"
        await update.message.reply_text("💳 روش خرید را انتخاب کنید:", reply_markup=get_payment_method_keyboard())
        return
    coupon_code = text.strip()
    discount_percent, error = await validate_coupon(coupon_code, user_id)
    if error:
        await update.message.reply_text(
            f"⚠️ {error}\nلطفا کد معتبر وارد کنید یا برای ادامه روی 'ادامه' کلیک کنید:",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("ادامه")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)
        )
        return
    discounted_amount = int(amount * (1 - discount_percent / 100))
    user_states[user_id] = f"awaiting_payment_method_{discounted_amount}_{plan}_{coupon_code}"
    await update.message.reply_text(
        f"✅ کد تخفیف اعمال شد! مبلغ با {discount_percent}% تخفیف: {discounted_amount} تومان\nروش خرید را انتخاب کنید:",
        reply_markup=get_payment_method_keyboard()
    )

async def handle_notification_type(update, context, user_id, text):
    if text == "📢 پیام به همه کاربران":
        user_states[user_id] = "awaiting_notification_text_all"
        await update.message.reply_text("📢 لطفا متن اطلاع‌رسانی را ارسال کنید:", reply_markup=get_back_keyboard())
    elif text == "🧑‍💼 پیام به نمایندگان":
        user_states[user_id] = "awaiting_notification_text_agents"
        await update.message.reply_text("🧑‍💼 لطفا متن اطلاع‌رسانی را ارسال کنید:", reply_markup=get_back_keyboard())
    elif text == "👤 پیام به یک نفر":
        user_states[user_id] = "awaiting_notification_target_user"
        await update.message.reply_text("🆔 ایدی عددی کاربر را وارد کنید:", reply_markup=get_back_keyboard())
    elif text == "⬅️ بازگشت به منو":
        await update.message.reply_text("🌐 منوی اصلی:", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)

async def handle_notification_target_user(update, context, user_id, text):
    try:
        target_user_id = int(text)
        user_exists = await db_execute("SELECT user_id FROM users WHERE user_id = %s", (target_user_id,), fetchone=True)
        if not user_exists:
            await update.message.reply_text("⚠️ کاربر یافت نشد. لطفا ایدی معتبر وارد کنید:", reply_markup=get_back_keyboard())
            return
        user_states[user_id] = f"awaiting_notification_text_single_{target_user_id}"
        await update.message.reply_text("📢 لطفا متن اطلاع‌رسانی را ارسال کنید:", reply_markup=get_back_keyboard())
    except ValueError:
        await update.message.reply_text("⚠️ ایدی عددی نامعتبر است. لطفا دوباره تلاش کنید:", reply_markup=get_back_keyboard())

async def handle_notification_text(update, context, user_id, state, text):
    notification_text = text
    if state == "awaiting_notification_text_all":
        notification_type = "all"
        user_type = "همه کاربران"
    elif state == "awaiting_notification_text_agents":
        notification_type = "agents"
        user_type = "نمایندگان"
    elif state.startswith("awaiting_notification_text_single_"):
        target_user_id = state.split("_")[-1]
        notification_type = f"single_{target_user_id}"
        user_type = f"کاربر {target_user_id}"
    else:
        notification_type = "unknown"
        user_type = "کاربران"
    context.user_data["notification_info"] = {
        "text": notification_text,
        "type": notification_type,
        "user_type": user_type
    }
    if state.startswith("awaiting_notification_text_single_"):
        context.user_data["notification_info"]["target_user_id"] = target_user_id
    await update.message.reply_text(
        f"📢 آیا مطمئن هستید که می‌خواهید این اطلاعیه را برای {user_type} ارسال کنید؟",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("✅ بله، ارسال کن")],
            [KeyboardButton("❌ خیر، انصراف")]
        ], resize_keyboard=True)
    )
    user_states[user_id] = f"confirm_notification_{notification_type}"

async def handle_confirm_notification(update, context, user_id, state, text):
    notification_type = state.replace("confirm_notification_", "")
    if text == "✅ بله، ارسال کن":
        notification_info = context.user_data.get("notification_info", {})
        notification_text = notification_info.get("text", "")
        user_type = notification_info.get("user_type", "کاربران")
        target_user_id = notification_info.get("target_user_id")
        if not notification_text:
            await update.message.reply_text("⚠️ متن اطلاعیه یافت نشد.", reply_markup=get_main_keyboard())
            user_states.pop(user_id, None)
            return
        await update.message.reply_text(f"🔄 در حال ارسال اطلاعیه به {user_type}...", reply_markup=None)
        try:
            if notification_type == "all":
                users = await db_execute("SELECT user_id FROM users", fetch=True)
            elif notification_type == "agents":
                users = await db_execute("SELECT user_id FROM users WHERE is_agent = TRUE", fetch=True)
            elif notification_type.startswith("single_"):
                if target_user_id:
                    users = [[int(target_user_id)]]
                else:
                    await update.message.reply_text("⚠️ ایدی کاربر یافت نشد.", reply_markup=get_main_keyboard())
                    user_states.pop(user_id, None)
                    return
            else:
                await update.message.reply_text("⚠️ نوع اطلاع‌رسانی نامعتبر است.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
                return
            if not users:
                await update.message.reply_text(f"⚠️ هیچ کاربری ({user_type}) یافت نشد.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
                return
            sent_count, failed_count, failed_users = await send_notification_to_users(context, users, notification_text)
            report_message = f"✅ اطلاعیه با موفقیت به {sent_count} {user_type} ارسال شد.\n"
            if failed_count > 0:
                report_message += f"❌ تعداد {user_type} که دریافت نکردند: {failed_count}\n"
                if failed_users:
                    failed_list = ", ".join(map(str, failed_users[:10]))
                    if len(failed_users) > 10:
                        failed_list += f" و {len(failed_users) - 10} کاربر دیگر"
                    report_message += f"کاربران ناموفق: {failed_list}"
            await update.message.reply_text(report_message, reply_markup=get_main_keyboard())
        except Exception as e:
            logging.error(f"Error in notification process: {e}")
            await update.message.reply_text(f"⚠️ خطا در ارسال اطلاعیه: {str(e)}", reply_markup=get_main_keyboard())
        if "notification_info" in context.user_data:
            del context.user_data["notification_info"]
    else:
        await update.message.reply_text("❌ ارسال اطلاعیه لغو شد.", reply_markup=get_main_keyboard())
    user_states.pop(user_id, None)

async def handle_admin_balance_user(update, context, user_id, text):
    try:
        target_user_id = int(text)
        user_exists = await db_execute("SELECT user_id FROM users WHERE user_id = %s", (target_user_id,), fetchone=True)
        if not user_exists:
            await update.message.reply_text("⚠️ کاربر یافت نشد.", reply_markup=get_back_keyboard())
            return
        user_states[user_id] = f"awaiting_balance_amount_{target_user_id}"
        await update.message.reply_text("💰 مبلغ را وارد کنید (مثبت برای افزایش، منفی برای کاهش):", reply_markup=get_back_keyboard())
    except ValueError:
        await update.message.reply_text("⚠️ ایدی عددی نامعتبر است.", reply_markup=get_back_keyboard())

async def handle_admin_balance_amount(update, context, user_id, state, text):
    try:
        parts = state.split("_")
        target_user_id = int(parts[3])
        amount = int(text)
        current_balance = await get_balance(target_user_id)
        if amount > 0:
            await add_balance(target_user_id, amount)
            await update.message.reply_text(f"✅ {amount:,} تومان به موجودی کاربر {target_user_id} اضافه شد. موجودی جدید: {current_balance + amount:,} تومان", reply_markup=get_main_keyboard())
        else:
            deduct_amount = abs(amount)
            if current_balance >= deduct_amount:
                await deduct_balance(target_user_id, deduct_amount)
                await update.message.reply_text(f"✅ {deduct_amount:,} تومان از موجودی کاربر {target_user_id} کسر شد. موجودی جدید: {current_balance - deduct_amount:,} تومان", reply_markup=get_main_keyboard())
            else:
                await update.message.reply_text(f"⚠️ موجودی کاربر {target_user_id} ({current_balance:,} تومان) کافی نیست.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
    except ValueError:
        await update.message.reply_text("⚠️ مبلغ نامعتبر است.", reply_markup=get_back_keyboard())
    except Exception as e:
        logging.error(f"Error in balance update: {e}")
        await update.message.reply_text("⚠️ خطا در به‌روزرسانی موجودی.", reply_markup=get_main_keyboard())

async def handle_admin_agent_user(update, context, user_id, text):
    try:
        target_user_id = int(text)
        user_exists = await db_execute("SELECT user_id, is_agent FROM users WHERE user_id = %s", (target_user_id,), fetchone=True)
        if not user_exists:
            await update.message.reply_text("⚠️ کاربر یافت نشد.", reply_markup=get_back_keyboard())
            return
        current_status = user_exists[1] if len(user_exists) > 1 else False
        status_text = "نماینده" if current_status else "ساده"
        user_states[user_id] = f"awaiting_agent_type_{target_user_id}"
        await update.message.reply_text(
            f"🆔 کاربر {target_user_id} در حال حاضر {status_text} است.\n"
            "نوع جدید اکانت را انتخاب کنید:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("ساده")],
                [KeyboardButton("نماینده")],
                [KeyboardButton("انصراف")]
            ], resize_keyboard=True)
        )
    except ValueError:
        await update.message.reply_text("⚠️ ایدی عددی نامعتبر است.", reply_markup=get_back_keyboard())

async def handle_admin_agent_type(update, context, user_id, state, text):
    parts = state.split("_")
    target_user_id = int(parts[3])
    if text == "ساده":
        await unset_user_agent(target_user_id)
        await update.message.reply_text(f"✅ نوع اکانت کاربر {target_user_id} به 'ساده' تغییر یافت.", reply_markup=get_main_keyboard())
    elif text == "نماینده":
        await set_user_agent(target_user_id)
        await update.message.reply_text(f"✅ نوع اکانت کاربر {target_user_id} به 'نماینده' تغییر یافت.", reply_markup=get_main_keyboard())
    elif text == "انصراف":
        await update.message.reply_text("❌ عملیات لغو شد.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("⚠️ گزینه نامعتبر.", reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("ساده")],
            [KeyboardButton("نماینده")],
            [KeyboardButton("انصراف")]
        ], resize_keyboard=True))
        return
    user_states.pop(user_id, None)

async def handle_normal_commands(update, context, user_id, text):
    if text in [
        "💰 موجودی", "💳 خرید اشتراک", "☎️ پشتیبانی",
        "📂 اشتراک‌های من", "💡 راهنمای اتصال", "🧑‍💼 درخواست نمایندگی"
    ]:
        user_states.pop(user_id, None)
    if text == "💰 موجودی":
        await update.message.reply_text("💰 بخش موجودی:\nیک گزینه را انتخاب کنید:", reply_markup=get_balance_keyboard())
        return
    if text == "نمایش موجودی":
        bal = await get_balance(user_id)
        await update.message.reply_text(f"💰 موجودی شما: {bal} تومان", reply_markup=get_balance_keyboard())
        return
    if text == "افزایش موجودی":
        await update.message.reply_text("💳 لطفا مبلغ واریزی را به تومان وارد کنید (مثال: 90000):", reply_markup=get_back_keyboard())
        user_states[user_id] = "awaiting_deposit_amount"
        return
    if user_states.get(user_id) == "awaiting_deposit_amount":
        if text.isdigit():
            amount = int(text)
            payment_id = await add_payment(user_id, amount, "increase_balance", "card_to_card")
            if payment_id:
                await update.message.reply_text(
                    f"لطفا {amount} تومان واریز کنید و فیش را ارسال کنید:\n\n"
                    f"🏦 شماره کارت بانکی:\n`{BANK_CARD}`\nبحق",
                    reply_markup=get_back_keyboard(),
                    parse_mode="MarkdownV2"
                )
                user_states[user_id] = f"awaiting_deposit_receipt_{payment_id}"
            else:
                await update.message.reply_text("⚠️ خطا در ثبت پرداخت. لطفا دوباره تلاش کنید.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
        else:
            await update.message.reply_text("⚠️ لطفا عدد وارد کنید.", reply_markup=get_back_keyboard())
        return
    if text == "💳 خرید اشتراک":
        await update.message.reply_text("💳 پلن را انتخاب کنید:", reply_markup=get_subscription_keyboard())
        return
    if text == "⭐️ کانفیگ تانل ویژه | گیگی ۸۵۰":
        amount = 850000
        await update.message.reply_text(
            f"💵 اگر کد تخفیف دارید، وارد کنید. در غیر این صورت برای ادامه روی 'ادامه' کلیک کنید:",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("ادامه")], [KeyboardButton("⬅️ بازگشت به منو")]], resize_keyboard=True)
        )
        user_states[user_id] = f"awaiting_coupon_code_{amount}_{text}"
        return
    if user_states.get(user_id, "").startswith("awaiting_payment_method_"):
        await handle_payment_method(update, context, user_id, text)
        return
    if text == "☎️ پشتیبانی":
        await update.message.reply_text("📞 پشتیبانی: https://t.me/teazadmin", reply_markup=get_main_keyboard())
        return
    if text == "📂 اشتراک‌های من":
        await show_user_subscriptions(update, context, user_id)
        return
    if text == "💡 راهنمای اتصال":
        await update.message.reply_text("راهنمای راه‌اندازی\nدستگاه خود را انتخاب کنید:", reply_markup=get_connection_guide_keyboard())
        return
    if text in ["📗 اندروید", "📕 آیفون/مک", "📘 ویندوز", "📙 لینوکس"]:
        guide_texts = {
            "📗 اندروید": "برای استفاده از کانفیگ، پیشنهاد ما استفاده از اپلیکیشن‌های V2RayNG یا Hiddify(پیشنهادی) است ✅\nبا این برنامه‌ها می‌تونی خیلی راحت و سریع کانفیگ رو وارد کنی و به اینترنت بدون محدودیت وصل بشی 🚀",
            "📕 آیفون/مک": "برای استفاده از کانفیگ، پیشنهاد ما استفاده از اپلیکیشن‌های Singbox(پیشنهادی) یا Streisand یا V2box(پیشنهادی) هست ✅\nبا این برنامه‌ها می‌تونی خیلی راحت و سریع کانفیگ رو وارد کنی و به اینترنت بدون محدودیت وصل بشی 🚀",
            "📘 ویندوز": "برای استفاده از کانفیگ، پیشنهاد ما استفاده از اپلیکیشن V2rayN هست ✅\nبا این برنامه‌ می‌تونی خیلی راحت و سریع کانفیگ رو وارد کنی و به اینترنت بدون محدودیت وصل بشی 🚀",
            "📙 لینوکس": "برای استفاده از کانفیگ، پیشنهاد ما استفاده از اپلیکیشن V2rayN هست ✅\nبا این برنامه‌ می‌تونی خیلی راحت و سریع کانفیگ رو وارد کنی و به اینترنت بدون محدودیت وصل بشی 🚀"
        }
        await update.message.reply_text(guide_texts[text], reply_markup=get_connection_guide_keyboard())
        return
    if text == "🧑‍💼 درخواست نمایندگی":
        await update.message.reply_text(
            "👨‍💼 برای اعطای نمایندگی، لطفاً به پشتیبانی مراجعه کنید:\n@teazadmin",
            reply_markup=get_main_keyboard()
        )
        return
    if user_states.get(user_id) == "confirm_user_removal":
        if text.startswith("✅ بله، کاربر"):
            target_user_id = context.user_data.get("pending_removal_user_id")
            if not target_user_id:
                await update.message.reply_text("⚠️ خطا در دریافت اطلاعات کاربر.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
                return
            success = await remove_user_from_db(target_user_id)
            if success:
                await update.message.reply_text(f"✅ کاربر با ایدی {target_user_id} به طور کامل از دیتابی حذف شد.", reply_markup=get_main_keyboard())
            else:
                await update.message.reply_text(f"⚠️ خطا در حذف کاربر {target_user_id} از دیتابیس.", reply_markup=get_main_keyboard())
            if "pending_removal_user_id" in context.user_data:
                del context.user_data["pending_removal_user_id"]
        elif text == "❌ خیر، انصراف":
            await update.message.reply_text("❌ عملیات حذف کاربر لغو شد.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return
    await update.message.reply_text("⚠️ دستور نامعتبر است. لطفا از دکمه‌ها استفاده کنید.", reply_markup=get_main_keyboard())
    user_states.pop(user_id, None)

async def handle_payment_method(update, context, user_id, text):
    state = user_states.get(user_id)
    logging.info(f"Processing payment method for user_id {user_id}, state: {state}")
    try:
        parts = state.split("_")
        amount = int(parts[3])
        plan = "_".join(parts[4:]) if len(parts) <= 5 else "_".join(parts[4:-1])
        coupon_code = parts[-1] if len(parts) > 5 else None
        if text == "🏦 کارت به کارت":
            payment_id = await add_payment(user_id, amount, "buy_subscription", "card_to_card", description=plan, coupon_code=coupon_code)
            if payment_id:
                await add_subscription(user_id, payment_id, plan)
                await update.message.reply_text(
                    f"لطفا {amount} تومان واریز کنید و فیش را ارسال کنید:\n\n"
                    f"🏦 شماره کارت بانکی:\n`{BANK_CARD}`\nبحق",
                    reply_markup=get_back_keyboard(),
                    parse_mode="MarkdownV2"
                )
                user_states[user_id] = f"awaiting_subscription_receipt_{payment_id}"
                logging.info(f"Set state to awaiting_subscription_receipt_{payment_id} for user_id {user_id}")
            else:
                await update.message.reply_text("⚠️ خطا در ثبت پرداخت. لطفا دوباره تلاش کنید.", reply_markup=get_main_keyboard())
                user_states.pop(user_id, None)
            return
        if text == "💰 پرداخت با موجودی":
            balance = await get_balance(user_id)
            if balance >= amount:
                payment_id = await add_payment(user_id, amount, "buy_subscription", "balance", description=plan, coupon_code=coupon_code)
                if payment_id:
                    await add_subscription(user_id, payment_id, plan)
                    await deduct_balance(user_id, amount)
                    await update_payment_status(payment_id, "approved")
                    await update.message.reply_text(
                        "✅ خرید شما با موفقیت انجام شد. حداکثر تا ۱ ساعت دیگر کانفیگ برای شما ارسال خواهد شد.",
                        reply_markup=get_main_keyboard()
                    )
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"📢 کاربر {user_id} (@{update.effective_user.username or 'NoUsername'}) با موجودی خود سرویس {plan} خریداری کرد."
                    )
                    config_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🟣 ارسال کانفیگ", callback_data=f"send_config_{payment_id}")]
                    ])
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"✅ پرداخت برای اشتراک ({plan}) تایید شد.",
                        reply_markup=config_keyboard
                    )
                    user_states.pop(user_id, None)
                    logging.info(f"Payment with balance successful for user_id {user_id}, payment_id: {payment_id}")
                else:
                    await update.message.reply_text("⚠️ خطا در ثبت پرداخت. لطفا دوباره تلاش کنید.", reply_markup=get_main_keyboard())
                    user_states.pop(user_id, None)
            else:
                await update.message.reply_text(
                    f"⚠️ موجودی شما ({balance} تومان) کافی نیست. لطفا ابتدا موجودی خود را افزایش دهید.",
                    reply_markup=get_main_keyboard()
                )
                user_states.pop(user_id, None)
            return
    except Exception as e:
        logging.error(f"Error processing payment method for user_id {user_id}, state: {state}, error: {e}")
        await update.message.reply_text("⚠️ خطا در پردازش. لطفا دوباره تلاش کنید.", reply_markup=get_main_keyboard())
        user_states.pop(user_id, None)
        return

async def show_user_subscriptions(update, context, user_id):
    try:
        subscriptions = await get_user_subscriptions(user_id)
        if not subscriptions:
            await update.message.reply_text("📂 شما هنوز اشتراکی ندارید.", reply_markup=get_main_keyboard())
            return
        current_time = datetime.now()
        response = "📂 لیست کامل اشتراک‌های شما:\n\n"
        for sub in subscriptions:
            try:
                response += f"🔹 اشتراک #{sub['id']}\n"
                response += f"📌 پلن: {sub['plan']}\n"
                response += f"🆔 کد خرید: #{sub['payment_id']}\n"
                response += f"📊 وضعیت: {'✅ فعال' if sub['status'] == 'active' else '⏳ در انتظار'}\n"
                if sub['status'] == "active":
                    remaining_days = max(0, (sub['end_date'] - current_time).days)
                    response += f"⏳ زمان باقی‌مانده: {remaining_days} روز\n"
                    response += f"📅 تاریخ شروع: {sub['start_date'].strftime('%Y-%m-%d %H:%M')}\n"
                    response += f"📅 تاریخ انقضا: {sub['end_date'].strftime('%Y-%m-%d %H:%M')}\n"
                if sub['config']:
                    response += f"🔐 کانفیگ:\n```\n{sub['config']}\n```\n"
                response += "------------------------\n\n"
            except Exception as e:
                logging.error(f"Error processing subscription {sub['id']} for user_id {user_id}: {e}")
                continue
        await send_long_message(user_id, response, context, reply_markup=get_main_keyboard(), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error displaying subscriptions for user_id {user_id}: {e}")
        await update.message.reply_text("⚠️ خطا در نمایش اشتراک‌ها. لطفا دوباره تلاش کنید.", reply_markup=get_main_keyboard())

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        await query.edit_message_text("⚠️ شما اجازه این کار را ندارید.")
        return
    try:
        if data.startswith("approve_"):
            payment_id = int(data.split("_")[1])
            payment = await db_execute("SELECT user_id, amount, type, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
            if not payment:
                await query.edit_message_text("⚠️ پرداخت یافت نشد.")
                return
            user_id, amount, ptype, description = payment
            await update_payment_status(payment_id, "approved")
            if ptype == "increase_balance":
                await add_balance(user_id, amount)
                await context.bot.send_message(user_id, f"💰 پرداخت تایید شد. موجودی {amount} تومان اضافه شد.")
                await query.edit_message_reply_markup(None)
                await query.edit_message_text("✅ پرداخت تایید شد.")
            elif ptype == "buy_subscription":
                await context.bot.send_message(user_id, f"✅ پرداخت تایید شد. اشتراک شما (کد خرید: #{payment_id}) ارسال خواهد شد.")
                await query.edit_message_reply_markup(None)
                await query.edit_message_text(f"✅ پرداخت #{payment_id} برای اشتراک ({description}) تایید شد.")
                # ارسال پیام جدید با دکمه ارسال کانفیگ
                config_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟣 ارسال کانفیگ", callback_data=f"send_config_{payment_id}")]
                ])
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"📢 برای پرداخت #{payment_id} (کاربر {user_id} - {description})\nلطفا کانفیگ را ارسال کنید:",
                    reply_markup=config_keyboard
                )
            elif ptype == "agency_request":
                await set_user_agent(user_id)
                await add_balance(user_id, amount)
                await context.bot.send_message(user_id, "✅ فیش شما تایید و نمایندگی به شما اعطا شد! ۱,۰۰۰,۰۰۰ تومان به موجودی شما اضافه شد.")
                await query.edit_message_reply_markup(None)
                await query.edit_message_text("✅ درخواست نمایندگی تایید شد.")
        elif data.startswith("reject_"):
            payment_id = int(data.split("_")[1])
            payment = await db_execute("SELECT user_id, amount, type FROM payments WHERE id = %s", (payment_id,), fetchone=True)
            if not payment:
                await query.edit_message_text("⚠️ پرداخت یافت نشد.")
                return
            user_id, amount, ptype = payment
            await update_payment_status(payment_id, "rejected")
            await context.bot.send_message(user_id, "❌ پرداخت شما رد شد. با پشتیبانی تماس بگیرید.")
            await query.edit_message_reply_markup(None)
            await query.edit_message_text("❌ پرداخت رد شد.")
        elif data.startswith("send_config_"):
            payment_id = int(data.split("_")[-1])
            payment = await db_execute("SELECT user_id, description FROM payments WHERE id = %s", (payment_id,), fetchone=True)
            if not payment:
                await query.edit_message_text("⚠️ پرداخت یافت نشد.")
                return
            user_id, description = payment
            await query.edit_message_reply_markup(None)
            await query.edit_message_text(f"✅ در انتظار دریافت کانفیگ برای پرداخت #{payment_id}\n👤 کاربر: {user_id}\n📦 محصول: {description}")
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📤 برای پرداخت #{payment_id} (کاربر {user_id}) لطفا کانفیگ را ارسال کنید:"
            )
            user_states[ADMIN_ID] = f"awaiting_config_{payment_id}"
        elif data == "admin_balance_action":
            await query.edit_message_reply_markup(None)
            await query.edit_message_text("🆔 ایدی عددی کاربر را وارد کنید:")
            user_states[ADMIN_ID] = "awaiting_admin_user_id_for_balance"
        elif data == "admin_agent_action":
            await query.edit_message_reply_markup(None)
            await query.edit_message_text("🆔 ایدی عددی کاربر را وارد کنید:")
            user_states[ADMIN_ID] = "awaiting_admin_user_id_for_agent"
        elif data == "admin_remove_user_action":
            await query.edit_message_reply_markup(None)
            await query.edit_message_text("🆔 ایدی عددی کاربری که می‌خواهید حذف کنید را وارد کنید:")
            user_states[ADMIN_ID] = "awaiting_user_id_for_removal"
        else:
            await query.edit_message_text("⚠️ دستور نامعتبر است.")
    except ValueError as e:
        logging.error(f"Error parsing callback data '{data}': {e}")
        await query.edit_message_text(f"⚠️ خطا در پردازش دستور: {data}")
    except Exception as e:
        logging.error(f"Error in admin_callback_handler: {e}")
        await query.edit_message_text("⚠️ خطا در پردازش درخواست.")

async def start_with_param(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and len(args) > 0:
        try:
            invited_by = int(args[0])
            if invited_by != update.effective_user.id:
                context.user_data["invited_by"] = invited_by
        except:
            context.user_data["invited_by"] = None
    await start(update, context)

# ---------- ثبت هندلرها ----------
application.add_handler(CommandHandler("start", start_with_param))
application.add_handler(CommandHandler("debug_subscriptions", debug_subscriptions))
application.add_handler(CommandHandler("cleardb", clear_db))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(CommandHandler("user_info", user_info_command))
application.add_handler(CommandHandler("coupon", coupon_command))
application.add_handler(CommandHandler("notification", notification_command))
application.add_handler(CommandHandler("backup", backup_command))
application.add_handler(CommandHandler("restore", restore_command))
application.add_handler(CommandHandler("remove_user", remove_user_command))
application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_handler))
application.add_handler(CallbackQueryHandler(admin_callback_handler))

# ---------- webhook endpoint ----------
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

# ---------- lifecycle events ----------
@app.on_event("startup")
async def on_startup():
    try:
        init_db_pool()
        await create_tables()
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"✅ Webhook set successfully: {WEBHOOK_URL}")
        await set_bot_commands()
        try:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text="🤖 ربات تیز VPN با موفقیت راه‌اندازی شد!\n"
                     f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"🌐 وب‌هوک: {RENDER_BASE_URL}\n\n"
                     "🆕 تغییرات اعمال شده:\n"
                     "1️⃣ حذف بخش تست رایگان\n"
                     "2️⃣ حذف بخش اعتبار رایگان\n"
                     "3️⃣ ساده‌سازی بخش درخواست نمایندگی (فقط لینک پشتیبانی)\n"
                     "4️⃣ اضافه شدن محصول جدید: ⭐️ کانفیگ تانل ویژه | گیگی ۸۵۰\n"
                     "5️⃣ رفع مشکل نمایش دکمه ارسال کانفیگ"
            )
        except Exception as e:
            logging.error(f"Error sending startup message to admin: {e}")
        logging.info("✅ Bot started successfully")
        print("✅ Bot started successfully!")
    except Exception as e:
        logging.error(f"❌ Error during startup: {e}")
        print(f"❌ Error during startup: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        try:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text="⚠️ ربات تیز VPN در حال خاموش شدن...\n"
                     f"⏰ زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception as e:
            logging.error(f"Error sending shutdown message to admin: {e}")
        await application.stop()
        await application.shutdown()
        close_db_pool()
        logging.info("✅ Bot shut down successfully")
    except Exception as e:
        logging.error(f"❌ Error during shutdown: {e}")

# ---------- اجرای محلی (برای debug) ----------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="info"
    )
