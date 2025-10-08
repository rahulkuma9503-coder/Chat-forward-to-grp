import os
import logging
import threading
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from storage import save_connection, get_connection

# Configuration
TOKEN = os.getenv("TOKEN")
OWNER_ID = os.getenv("OWNER_ID", "OWNER_IDD")
if not TOKEN:
    raise ValueError("Missing TOKEN environment variable")
if not OWNER_ID:
    raise ValueError("Missing OWNER_ID environment variable")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)

@app.route('/')
def health_check():
    return "🤖 Bot is running! Only the owner can use me.", 200

def run_flask():
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting Flask health check on port {port}")
    app.run(host='0.0.0.0', port=port)

# Initialize Telegram application
application = Application.builder().token(TOKEN).build()

def is_owner(user_id: int) -> bool:
    """Check if user is the owner"""
    return str(user_id) == OWNER_ID

# Command handlers
async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /connect command"""
    # Ignore group messages completely
    if update.message.chat.type != "private":
        return
    
    # Owner check
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    args = context.args
    
    if not args:
        await update.message.reply_text("Please provide a group ID. Usage: /connect <group_id>")
        return
    
    try:
        group_id = int(args[0])
        save_connection(update.message.from_user.id, group_id)
        await update.message.reply_text(
            f"✅ Connected to group {group_id}!\n"
            "Send any message to me (in private) and I'll forward it there."
        )
    except ValueError:
        await update.message.reply_text("Invalid group ID. Must be an integer.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages"""
    # Ignore group messages completely
    if update.message.chat.type != "private":
        return
    
    # Owner check
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    user_id = update.message.from_user.id
    group_id = get_connection(user_id)
    
    if not group_id:
        await update.message.reply_text(
            "⚠️ You're not connected to any group! "
            "Use /connect <group_id> first."
        )
        return
    
    try:
        # Handle stickers specifically
        if update.message.sticker:
            await context.bot.send_sticker(
                chat_id=group_id,
                sticker=update.message.sticker.file_id
            )
        # Handle all other media types
        else:
            await context.bot.copy_message(
                chat_id=group_id,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
    except Exception as e:
        logger.error(f"Forwarding failed: {e}")
        await update.message.reply_text(
            "❌ Failed to send message. Make sure:\n"
            "1. I'm added to the group\n"
            "2. The group ID is correct (use negative ID for supergroups)\n"
            "3. I have 'Send Messages' permission\n"
            "4. Try reconnecting with /connect"
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    # Ignore group messages completely
    if update.message.chat.type != "private":
        return
    
    # Owner check
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    await update.message.reply_text(
        "🤖 Owner-Only Forward Bot is running!\n"
        "Use /connect <group_id> to start forwarding messages\n\n"
        "⚠️ Note: Only you (the owner) can use this bot."
    )

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("connect", connect_command))

# Handle all non-command messages
application.add_handler(MessageHandler(
    filters.ALL & ~filters.COMMAND,
    handle_message
))

def start_bot():
    """Start Telegram bot in polling mode"""
    logger.info("Starting Telegram bot in polling mode...")
    logger.info(f"Owner ID: {OWNER_ID}")
    application.run_polling()

if __name__ == "__main__":
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start bot in main thread
    logger.info("Starting bot...")
    start_bot()
