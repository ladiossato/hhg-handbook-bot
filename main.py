import os
import re
import json
import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import psycopg2
from psycopg2.extras import RealDictCursor

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", 0))

# Acknowledgment pattern
# Matches: "I, [Name], acknowledge and agree to the HHG Employee Handbook v2026-01-20"
ACK_PATTERN = re.compile(
    r"I,?\s+(.+?),?\s+acknowledge\s+and\s+agree\s+to\s+the\s+HHG\s+Employee\s+Handbook\s+(v[\d\-]+)",
    re.IGNORECASE
)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def find_or_create_employee(conn, telegram_user_id: str, telegram_username: str, full_name: str):
    """Find employee by telegram_user_id, or create if not exists. Return employee ID (integer)."""
    with conn.cursor() as cur:
        # Check if employee exists
        cur.execute(
            "SELECT id FROM employees WHERE telegram_user_id = %s",
            (telegram_user_id,)
        )
        row = cur.fetchone()
        
        if row:
            # Update username/name if changed
            cur.execute("""
                UPDATE employees 
                SET telegram_username = %s,
                    full_name = COALESCE(NULLIF(%s, ''), full_name),
                    updated_at = NOW()
                WHERE telegram_user_id = %s
            """, (telegram_username, full_name, telegram_user_id))
            conn.commit()
            return row["id"]
        else:
            # Create new employee
            cur.execute("""
                INSERT INTO employees (telegram_user_id, telegram_username, full_name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'pending', NOW(), NOW())
                RETURNING id
            """, (telegram_user_id, telegram_username, full_name))
            conn.commit()
            return cur.fetchone()["id"]

def insert_acknowledgment(conn, employee_id: int, version: str, ack_text: str, message: dict):
    """Insert acknowledgment record."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO acknowledgments (
                employee_id,
                handbook_version,
                ack_text,
                acknowledged_at,
                telegram_chat_id,
                telegram_message_id,
                telegram_message_date,
                raw_telegram_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            employee_id,
            version,
            ack_text,
            datetime.now(timezone.utc),
            message["chat_id"],
            message["message_id"],
            datetime.fromtimestamp(message["date"], timezone.utc),
            json.dumps(message["raw"])
        ))
        conn.commit()
        return cur.fetchone()["id"]

def check_existing_acknowledgment(conn, telegram_user_id: str, version: str) -> bool:
    """Check if user already acknowledged this version."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM acknowledgments a
            JOIN employees e ON e.id = a.employee_id
            WHERE e.telegram_user_id = %s AND a.handbook_version = %s
            LIMIT 1
        """, (telegram_user_id, version))
        return cur.fetchone() is not None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process incoming messages for acknowledgment phrases."""
    message = update.message
    if not message or not message.text:
        return
    
    # Restrict to specific group
    if ALLOWED_CHAT_ID and message.chat_id != ALLOWED_CHAT_ID:
        return
    
    # Check for acknowledgment pattern
    match = ACK_PATTERN.search(message.text)
    if not match:
        return
    
    full_name = match.group(1).strip()
    version = match.group(2).strip()
    
    user = message.from_user
    telegram_user_id = str(user.id)  # varchar in DB
    telegram_username = user.username or ""
    
    # Prepare message data for storage
    message_data = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "date": message.date.timestamp(),
        "raw": {
            "message_id": message.message_id,
            "from": {
                "id": user.id,
                "is_bot": user.is_bot,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": user.username,
            },
            "chat": {
                "id": message.chat_id,
                "type": message.chat.type,
                "title": getattr(message.chat, "title", None),
            },
            "date": int(message.date.timestamp()),
            "text": message.text,
        }
    }
    
    try:
        conn = get_db_connection()
        
        # Check if already acknowledged
        if check_existing_acknowledgment(conn, telegram_user_id, version):
            await message.reply_text(
                f"✓ {full_name}, you've already acknowledged handbook {version}."
            )
            conn.close()
            return
        
        # Find or create employee
        employee_id = find_or_create_employee(conn, telegram_user_id, telegram_username, full_name)
        
        # Insert acknowledgment
        insert_acknowledgment(conn, employee_id, version, message.text, message_data)
        
        conn.close()
        
        logger.info(f"Recorded: {full_name} (@{telegram_username}) acknowledged {version}")
        
        await message.reply_text(
            f"✓ Recorded: {full_name} acknowledged HHG Employee Handbook {version}\n"
            f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        
    except Exception as e:
        logger.error(f"Error processing acknowledgment: {e}")
        await message.reply_text(
            "⚠️ Error recording acknowledgment. Please try again or contact admin."
        )

def main():
    """Start the bot."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("HHG Handbook Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
