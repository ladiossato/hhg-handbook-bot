# HHG Handbook Acknowledgment Bot

Telegram bot that records employee handbook acknowledgments to Supabase with timestamps and audit trail.

## Setup

1. Copy `.env.example` to `.env` and fill in your values
2. Deploy to Railway and add environment variables in dashboard

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_URL` | PostgreSQL connection string |
| `ALLOWED_CHAT_ID` | Telegram group chat ID |

## Employee Acknowledgment Format

Employees send this in the group chat:

```
I, [Full Name], acknowledge and agree to the HHG Employee Handbook v2026-01-20
```

## What It Does

- Watches group for acknowledgment messages
- Links Telegram user to employee record
- Logs acknowledgment with timestamp + raw message JSON
- Replies with confirmation
