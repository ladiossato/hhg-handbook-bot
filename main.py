import os
import re
import json
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import psycopg
from psycopg.rows import dict_row

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
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def find_employee_by_name(conn, full_name: str):
    """Find employee by exact full_name match (case-insensitive). Return employee record or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, full_name, telegram_user_id FROM employees WHERE LOWER(full_name) = LOWER(%s)",
            (full_name,)
        )
        return cur.fetchone()

def find_similar_names(conn, full_name: str, threshold: float = 0.6):
    """Find employees with similar names. Returns list of potential matches."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, full_name FROM employees WHERE full_name IS NOT NULL")
        all_employees = cur.fetchall()
    
    input_lower = full_name.lower()
    input_parts = input_lower.split()
    
    matches = []
    for emp in all_employees:
        emp_name = emp["full_name"]
        emp_lower = emp_name.lower()
        emp_parts = emp_lower.split()
        
        # Check full name similarity
        ratio = SequenceMatcher(None, input_lower, emp_lower).ratio()
        
        # Boost score if first or last name matches closely
        part_bonus = 0
        for input_part in input_parts:
            for emp_part in emp_parts:
                part_ratio = SequenceMatcher(None, input_part, emp_part).ratio()
                if part_ratio > 0.8:
                    part_bonus = 0.15
                    break
        
        final_score = ratio + part_bonus
        
        if final_score >= threshold:
            matches.append({
                "id": emp["id"],
                "full_name": emp_name,
                "score": final_score
            })
    
    # Sort by score descending, return top 3
    matches.sort(key=lambda x: x["score"], reverse=True)
    return matches[:3]

def update_employee_telegram(conn, employee_id: int, telegram_user_id: str, telegram_username: str):
    """Update employee's Telegram info."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE employees 
            SET telegram_user_id = %s,
                telegram_username = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (telegram_user_id, telegram_username, employee_id))
        conn.commit()

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

def check_existing_acknowledgment(conn, employee_id: int, version: str) -> bool:
    """Check if employee already acknowledged this version."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM acknowledgments
            WHERE employee_id = %s AND handbook_version = %s
            LIMIT 1
        """, (employee_id, version))
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
    telegram_user_id = str(user.id)
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
        
        # Find employee by full_name
        employee = find_employee_by_name(conn, full_name)
        
        if not employee:
            # Look for similar names
            similar = find_similar_names(conn, full_name)
            
            if similar:
                suggestions = "\n".join([f"• {s['full_name']}" for s in similar])
                await message.reply_text(
                    f"⚠️ Name not found: \"{full_name}\"\n\n"
                    f"Did you mean one of these?\n{suggestions}\n\n"
                    f"Please resend using your exact name as shown above.\n\n"
                    f"Example:\nI, {similar[0]['full_name']}, acknowledge and agree to the HHG Employee Handbook {version}"
                )
            else:
                await message.reply_text(
                    f"⚠️ Name not found: \"{full_name}\"\n\n"
                    f"Please use your full name exactly as it appears in our system.\n\n"
                    f"Contact your manager if you need help."
                )
            conn.close()
            return
        
        employee_id = employee["id"]
        db_full_name = employee["full_name"]
        
        # Check if already acknowledged
        if check_existing_acknowledgment(conn, employee_id, version):
            await message.reply_text(
                f"✓ {db_full_name}, you've already acknowledged handbook {version}."
            )
            conn.close()
            return
        
        # Update employee's Telegram info
        update_employee_telegram(conn, employee_id, telegram_user_id, telegram_username)
        
        # Insert acknowledgment
        insert_acknowledgment(conn, employee_id, version, message.text, message_data)
        
        conn.close()
        
        logger.info(f"Recorded: {db_full_name} (@{telegram_username}) acknowledged {version}")
        
        await message.reply_text(
            f"✓ Recorded: {db_full_name} acknowledged HHG Employee Handbook {version}\n"
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