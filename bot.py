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
from pymongo import MongoClient
from datetime import datetime, timezone

# Configuration
TOKEN = os.getenv("TOKEN")
OWNER_ID = os.getenv("OWNER_ID", "OWNER_IDD")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
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

# MongoDB setup
try:
    client = MongoClient(MONGO_URI)
    db = client.telegram_bot
    connections_collection = db.connections
    stats_collection = db.stats
    # Test connection
    client.admin.command('ping')
    logger.info("✅ MongoDB connection successful")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    raise

# Store message mappings for reply functionality
message_mappings = {}
# Store active connections and group info
active_groups = {}

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

# MongoDB Storage Functions
def save_connection(owner_id: int, group_id: int, group_name: str, group_username: str = ""):
    """Save connection to MongoDB"""
    connection_data = {
        "owner_id": owner_id,
        "group_id": group_id,
        "group_name": group_name,
        "group_username": group_username,
        "connected_at": datetime.now(timezone.utc),
        "is_active": True
    }
    
    # Update or insert
    connections_collection.update_one(
        {"owner_id": owner_id, "group_id": group_id},
        {"$set": connection_data},
        upsert=True
    )
    
    # Update stats
    update_stats(owner_id, "connection_added")

def remove_connection(owner_id: int, group_id: int):
    """Remove connection from MongoDB"""
    result = connections_collection.update_one(
        {"owner_id": owner_id, "group_id": group_id},
        {"$set": {"is_active": False, "disconnected_at": datetime.now(timezone.utc)}}
    )
    
    if result.modified_count > 0:
        update_stats(owner_id, "connection_removed")
        return True
    return False

def get_all_connections(owner_id: int):
    """Get all active connections for owner"""
    connections = {}
    cursor = connections_collection.find({
        "owner_id": owner_id,
        "is_active": True
    })
    
    for doc in cursor:
        connections[doc["group_id"]] = {
            "name": doc["group_name"],
            "username": doc.get("group_username", ""),
            "connected_at": doc.get("connected_at")
        }
    
    return connections

def update_stats(owner_id: int, action: str):
    """Update statistics in MongoDB"""
    # Use datetime for today's date at midnight UTC
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    update_fields = {
        "messages_sent": 1 if action == "message_sent" else 0,
        "connections_added": 1 if action == "connection_added" else 0,
        "connections_removed": 1 if action == "connection_removed" else 0,
        "replies_handled": 1 if action == "reply_handled" else 0
    }
    
    stats_collection.update_one(
        {
            "owner_id": owner_id,
            "date": today
        },
        {
            "$inc": update_fields
        },
        upsert=True
    )

def get_bot_stats(owner_id: int):
    """Get comprehensive bot statistics"""
    # Total connections
    total_connections = connections_collection.count_documents({
        "owner_id": owner_id,
        "is_active": True
    })
    
    # Today's stats - use datetime for today at midnight UTC
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_stats = stats_collection.find_one({
        "owner_id": owner_id,
        "date": today
    }) or {}
    
    # All-time stats
    pipeline = [
        {"$match": {"owner_id": owner_id}},
        {"$group": {
            "_id": None,
            "total_messages": {"$sum": "$messages_sent"},
            "total_replies": {"$sum": "$replies_handled"},
            "total_connections_added": {"$sum": "$connections_added"},
            "total_connections_removed": {"$sum": "$connections_removed"}
        }}
    ]
    
    all_time_stats = list(stats_collection.aggregate(pipeline))
    all_time_stats = all_time_stats[0] if all_time_stats else {}
    
    return {
        "total_connections": total_connections,
        "today": today_stats,
        "all_time": all_time_stats
    }

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
        
        # Try to get group info to verify the bot is in the group
        try:
            chat = await context.bot.get_chat(group_id)
            group_name = chat.title
            group_type = "Supergroup" if chat.type == "supergroup" else "Group"
            group_username = f"@{chat.username}" if chat.username else ""
        except Exception as e:
            await update.message.reply_text(
                f"❌ Cannot access group {group_id}. Make sure:\n"
                "1. The group ID is correct\n"
                "2. I'm added to the group\n"
                "3. The group exists\n\n"
                f"Error: {str(e)}"
            )
            return
        
        save_connection(update.message.from_user.id, group_id, group_name, group_username)
        
        # Store in active groups cache
        active_groups[group_id] = {
            'name': group_name,
            'type': group_type,
            'member_count': getattr(chat, 'member_count', 'Unknown'),
            'username': group_username
        }
        
        await update.message.reply_text(
            f"✅ Connected to {group_type}: {group_name} (ID: {group_id})!\n\n"
            "🔄 Features:\n"
            "- Send any message to me and I'll forward it to all connected groups\n"
            "- When someone replies to my messages, I'll forward them to you\n"
            "- Reply to those messages and I'll send your response back!\n"
            "- Use /stats to see all connected groups\n"
            "- Use /disconnect <group_id> to remove a group\n"
            "- Use /botstats for detailed statistics"
        )
    except ValueError:
        await update.message.reply_text("Invalid group ID. Must be an integer.")

async def disconnect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /disconnect command"""
    if update.message.chat.type != "private":
        return
    
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    args = context.args
    
    if not args:
        # Show current connections and disconnect instructions
        connections = get_all_connections(update.message.from_user.id)
        if not connections:
            await update.message.reply_text("❌ You are not connected to any groups!")
            return
        
        message = "📋 Your Connected Groups:\n\n"
        for group_id, group_info in connections.items():
            message += f"• {group_info['name']} (ID: {group_id})\n"
        
        message += "\nTo disconnect from a group, use: /disconnect <group_id>"
        await update.message.reply_text(message)
        return
    
    try:
        group_id = int(args[0])
        connections = get_all_connections(update.message.from_user.id)
        
        if group_id not in connections:
            await update.message.reply_text(f"❌ You are not connected to group {group_id}!")
            return
        
        group_name = connections[group_id]['name']
        success = remove_connection(update.message.from_user.id, group_id)
        
        if success:
            # Remove from active groups cache
            if group_id in active_groups:
                del active_groups[group_id]
            
            await update.message.reply_text(f"✅ Disconnected from group: {group_name} (ID: {group_id})")
        else:
            await update.message.reply_text(f"❌ Failed to disconnect from group {group_id}")
        
    except ValueError:
        await update.message.reply_text("Invalid group ID. Must be an integer.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command to show group details"""
    if update.message.chat.type != "private":
        return
    
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    connections = get_all_connections(update.message.from_user.id)
    
    if not connections:
        await update.message.reply_text("❌ You are not connected to any groups!")
        return
    
    message = "📊 Connected Groups\n\n"
    total_groups = len(connections)
    message += f"📈 Total Groups Connected: {total_groups}\n\n"
    
    # Get fresh info for each group
    for group_id, group_info in connections.items():
        try:
            # Try to get updated group info
            chat = await context.bot.get_chat(group_id)
            member_count = getattr(chat, 'member_count', 'Unknown')
            group_type = "Supergroup" if chat.type == "supergroup" else "Group"
            username = f"@{chat.username}" if chat.username else "No username"
            
            # Update cache and database with fresh info
            active_groups[group_id] = {
                'name': chat.title,
                'type': group_type,
                'member_count': member_count,
                'username': username
            }
            
            # Update database with fresh info
            connections_collection.update_one(
                {"owner_id": update.message.from_user.id, "group_id": group_id},
                {"$set": {
                    "group_name": chat.title,
                    "group_username": username
                }}
            )
            
            message += (
                f"🏷️ **{chat.title}**\n"
                f"   📝 Type: {group_type}\n"
                f"   🆔 ID: `{group_id}`\n"
                f"   👥 Members: {member_count}\n"
                f"   🔗 {username}\n"
                f"   ➖ /disconnect_{group_id}\n\n"
            )
            
        except Exception as e:
            # Use cached info if available
            if group_id in active_groups:
                group_data = active_groups[group_id]
                message += (
                    f"🏷️ **{group_data['name']}**\n"
                    f"   📝 Type: {group_data['type']}\n"
                    f"   🆔 ID: `{group_id}`\n"
                    f"   👥 Members: {group_data.get('member_count', 'Unknown')}\n"
                    f"   🔗 {group_data.get('username', 'No username')}\n"
                    f"   ⚠️ Could not refresh info\n"
                    f"   ➖ /disconnect_{group_id}\n\n"
                )
            else:
                message += (
                    f"🏷️ **{group_info['name']}**\n"
                    f"   🆔 ID: `{group_id}`\n"
                    f"   ⚠️ Could not fetch group info\n"
                    f"   ➖ /disconnect_{group_id}\n\n"
                )
    
    message += "💡 Use /disconnect <group_id> to remove a group"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def botstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /botstats command for detailed statistics"""
    if update.message.chat.type != "private":
        return
    
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    stats = get_bot_stats(update.message.from_user.id)
    
    message = "🤖 **Bot Statistics**\n\n"
    
    # Overall stats
    message += "📈 **Overall Statistics**\n"
    message += f"• Total Active Groups: `{stats['total_connections']}`\n"
    
    if stats['all_time']:
        message += f"• Total Messages Sent: `{stats['all_time'].get('total_messages', 0)}`\n"
        message += f"• Total Replies Handled: `{stats['all_time'].get('total_replies', 0)}`\n"
        message += f"• Total Connections Added: `{stats['all_time'].get('total_connections_added', 0)}`\n"
        message += f"• Total Connections Removed: `{stats['all_time'].get('total_connections_removed', 0)}`\n"
    
    # Today's stats
    message += "\n📊 **Today's Statistics**\n"
    if stats['today']:
        message += f"• Messages Sent: `{stats['today'].get('messages_sent', 0)}`\n"
        message += f"• Replies Handled: `{stats['today'].get('replies_handled', 0)}`\n"
        message += f"• Connections Added: `{stats['today'].get('connections_added', 0)}`\n"
        message += f"• Connections Removed: `{stats['today'].get('connections_removed', 0)}`\n"
    else:
        message += "• No activity today\n"
    
    # Database info
    total_db_connections = connections_collection.count_documents({
        "owner_id": update.message.from_user.id
    })
    active_db_connections = connections_collection.count_documents({
        "owner_id": update.message.from_user.id,
        "is_active": True
    })
    
    message += f"\n💾 **Database**\n"
    message += f"• Total Records: `{total_db_connections}`\n"
    message += f"• Active Connections: `{active_db_connections}`\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming private messages from owner"""
    # Owner check
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    user_id = update.message.from_user.id
    connections = get_all_connections(user_id)
    
    if not connections:
        await update.message.reply_text(
            "⚠️ You're not connected to any groups! "
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
            target_group_id = mapping['group_id']
            
            # Verify the group is still connected
            if target_group_id not in connections:
                await update.message.reply_text("❌ You are no longer connected to that group!")
                return
            
            try:
                # Send reply back to the group as a direct reply to the user's message
                if update.message.sticker:
                    await context.bot.send_sticker(
                        chat_id=target_group_id,
                        sticker=update.message.sticker.file_id,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.text:
                    await context.bot.send_message(
                        chat_id=target_group_id,
                        text=update.message.text,
                        reply_to_message_id=original_group_msg_id
                    )
                else:
                    # For other media types
                    await context.bot.copy_message(
                        chat_id=target_group_id,
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id,
                        reply_to_message_id=original_group_msg_id
                    )
                
                # Update stats
                update_stats(user_id, "reply_handled")
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
    
    # Normal message forwarding (not a reply) - send to ALL connected groups
    successful_forwards = 0
    failed_forwards = []
    
    for group_id, group_info in connections.items():
        try:
            if update.message.sticker:
                await context.bot.send_sticker(
                    chat_id=group_id,
                    sticker=update.message.sticker.file_id
                )
            else:
                await context.bot.copy_message(
                    chat_id=group_id,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id
                )
            successful_forwards += 1
        except Exception as e:
            logger.error(f"Failed to forward to group {group_id}: {e}")
            failed_forwards.append(f"{group_info['name']} (ID: {group_id})")
    
    # Update stats for successful sends
    if successful_forwards > 0:
        for _ in range(successful_forwards):
            update_stats(user_id, "message_sent")
    
    # Send summary to owner
    if successful_forwards > 0 and not failed_forwards:
        await update.message.reply_text(f"✅ Message sent to {successful_forwards} group(s)!")
    elif successful_forwards > 0 and failed_forwards:
        summary = f"✅ Sent to {successful_forwards} group(s)\n❌ Failed in {len(failed_forwards)} group(s):\n"
        summary += "\n".join(failed_forwards)
        await update.message.reply_text(summary)
    else:
        await update.message.reply_text(
            "❌ Failed to send message to all groups. Make sure:\n"
            "1. I'm added to all groups\n"
            "2. I have 'Send Messages' permission\n"
            "3. Try reconnecting with /connect"
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
        
        # Get owner's connections to verify this is a connected group
        owner_connections = get_all_connections(int(OWNER_ID))
        if group_id not in owner_connections:
            return
        
        try:
            # Forward the group reply to the owner with mapping information
            forwarded_msg = await context.bot.forward_message(
                chat_id=OWNER_ID,
                from_chat_id=group_id,
                message_id=update.message.message_id
            )
            
            # Store mapping for reply functionality
            message_mappings[forwarded_msg.message_id] = {
                'original_group_message_id': update.message.message_id,
                'sender': update.message.from_user,
                'group_id': group_id
            }
            
            # Send info message to owner
            group_name = owner_connections[group_id]['name']
            user_name = update.message.from_user.first_name
            if update.message.from_user.username:
                user_name = f"@{update.message.from_user.username}"
            
            info_msg = await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"💬 Reply from {user_name} in {group_name}.\n"
                     f"📝 Reply to this message to respond to them directly!",
                reply_to_message_id=forwarded_msg.message_id
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
        "🤖 Owner-Only Forward Bot is running!\n\n"
        "🔧 Available Commands:\n"
        "• /connect <group_id> - Connect to a group\n"
        "• /disconnect [group_id] - Disconnect from group(s)\n"
        "• /stats - Show all connected groups with details\n"
        "• /botstats - Detailed bot statistics and analytics\n\n"
        "🔄 Features:\n"
        "- Connect to multiple groups simultaneously\n"
        "- Messages are forwarded to ALL connected groups\n"
        "- Reply to group messages and bot will respond\n"
        "- View detailed group statistics\n"
        "- MongoDB database for reliable storage\n\n"
        "⚠️ Note: Only you (the owner) can use this bot."
    )

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("connect", connect_command))
application.add_handler(CommandHandler("disconnect", disconnect_command))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(CommandHandler("botstats", botstats_command))

# Handle quick disconnect commands (e.g., /disconnect_123456789)
async def quick_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick disconnect commands like /disconnect_123456789"""
    if update.message.chat.type != "private":
        return
    
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    command = update.message.text[1:]  # Remove the leading '/'
    if command.startswith("disconnect_"):
        try:
            group_id = int(command.split("_")[1])
            # Set the group_id in context args for disconnect_command
            context.args = [str(group_id)]
            await disconnect_command(update, context)
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid quick disconnect format!")

application.add_handler(MessageHandler(
    filters.Regex(r'^/disconnect_\-?\d+$') & filters.ChatType.PRIVATE,
    quick_disconnect
))

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
    logger.info(f"MongoDB URI: {MONGO_URI}")
    application.run_polling()

if __name__ == "__main__":
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start bot in main thread
    logger.info("Starting bot...")
    start_bot()