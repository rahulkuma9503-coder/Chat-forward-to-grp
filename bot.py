import os
import logging
import threading
import html
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

# Store message mappings for reply functionality
message_mappings = {}

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

def get_user_mention(user) -> str:
    """Generate user mention for tagging"""
    if user.username:
        return f"@{user.username}"
    else:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        # Escape HTML characters to prevent issues
        safe_name = html.escape(full_name)
        return f'<a href="tg://user?id={user.id}">{safe_name}</a>'

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
            "Send any message to me (in private) and I'll forward it there.\n\n"
            "🔄 New Feature:\n"
            "- When someone replies to my messages in group, I'll forward them to you\n"
            "- Reply to those messages and I'll send your response back to the same person!\n"
            "- The bot will tag (mention) the user when responding in group"
        )
    except ValueError:
        await update.message.reply_text("Invalid group ID. Must be an integer.")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming private messages from owner"""
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
    
    # Check if this is a reply to a forwarded group message
    if update.message.reply_to_message:
        original_private_msg = update.message.reply_to_message
        
        # Check if this was a forwarded group message that has mapping
        if original_private_msg.message_id in message_mappings:
            
            mapping = message_mappings[original_private_msg.message_id]
            original_group_msg_id = mapping['original_group_message_id']
            original_sender = mapping['sender']
            
            try:
                # Get user mention for tagging
                user_mention = get_user_mention(original_sender)
                
                # Send reply back to the group as a reply to the original user's message
                if update.message.sticker:
                    # Send the mention first, then the sticker as a reply to the mention
                    mention_msg = await context.bot.send_message(
                        chat_id=group_id,
                        text=f"👤 {user_mention}",
                        parse_mode='HTML'
                    )
                    await context.bot.send_sticker(
                        chat_id=group_id,
                        sticker=update.message.sticker.file_id,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.text:
                    # For text messages, send directly as reply with mention
                    await context.bot.send_message(
                        chat_id=group_id,
                        text=f"👤 {user_mention}\n{update.message.text}",
                        reply_to_message_id=original_group_msg_id,
                        parse_mode='HTML'
                    )
                else:
                    # For other media types
                    mention_msg = await context.bot.send_message(
                        chat_id=group_id,
                        text=f"👤 {user_mention}",
                        reply_to_message_id=original_group_msg_id,
                        parse_mode='HTML'
                    )
                    await context.bot.copy_message(
                        chat_id=group_id,
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id,
                        reply_to_message_id=original_group_msg_id
                    )
                
                await update.message.reply_text("✅ Your response has been sent to the group!")
                
            except Exception as e:
                logger.error(f"Failed to send reply to group: {e}")
                await update.message.reply_text(
                    f"❌ Failed to send response: {str(e)}\n"
                    "Make sure:\n"
                    "1. I'm still in the group\n"
                    "2. I have 'Send Messages' permission\n"
                    "3. The original message still exists"
                )
            return
    
    # Normal message forwarding (not a reply)
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

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming group messages (only replies to bot's messages)"""
    # Only process replies to the bot's messages
    if not update.message.reply_to_message:
        return
    
    # Check if the replied-to message is from the bot
    if (update.message.reply_to_message and 
        update.message.reply_to_message.from_user and 
        update.message.reply_to_message.from_user.id == context.bot.id):
        
        user_id = update.message.from_user.id
        group_id = update.message.chat.id
        
        # Get owner's connection to verify this is the connected group
        owner_connection = get_connection(int(OWNER_ID))
        if owner_connection != group_id:
            return
        
        try:
            # Forward the group reply to the owner with mapping information
            forwarded_msg = await context.bot.forward_message(
                chat_id=OWNER_ID,
                from_chat_id=group_id,
                message_id=update.message.message_id
            )
            
            # Store mapping for reply functionality - store the ORIGINAL group message ID (the user's reply)
            message_mappings[forwarded_msg.message_id] = {
                'original_group_message_id': update.message.message_id,  # This is the user's reply message ID in group
                'sender': update.message.from_user,
                'group_id': group_id
            }
            
            # Send info message to owner with user mention
            user_mention = get_user_mention(update.message.from_user)
            
            info_msg = await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"💬 Reply from {user_mention} in group.\n"
                     f"📝 You can reply to this message to respond to them!\n"
                     f"🔔 They will be tagged when you reply.",
                reply_to_message_id=forwarded_msg.message_id,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Failed to forward group reply to owner: {e}")

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
        "🔄 New Features:\n"
        "- When someone replies to my messages in group, I'll forward them to you\n"
        "- Reply to those messages and I'll send your response back!\n"
        "- The bot will tag (mention) users when responding in group\n\n"
        "⚠️ Note: Only you (the owner) can use this bot."
    )

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("connect", connect_command))

# Handle private messages from owner
application.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & ~filters.COMMAND,
    handle_private_message
))

# Handle group messages that are replies to bot's messages
application.add_handler(MessageHandler(
    filters.ChatType.GROUPS & filters.REPLY & ~filters.COMMAND,
    handle_group_message
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
