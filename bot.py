import os
import logging
import threading
import html
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    MessageReactionHandler
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
# Store reaction mappings - track both directions
reaction_mappings = {}
# Store group message to private message mappings
group_to_private_mappings = {}
# Store active connections and group info
active_groups = {}
# Store pending messages for group selection
pending_messages = {}
# Store edit mappings - track messages that can be edited
edit_mappings = {}

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
        "replies_handled": 1 if action == "reply_handled" else 0,
        "reactions_handled": 1 if action == "reaction_handled" else 0,
        "edits_handled": 1 if action == "edit_handled" else 0
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
            "total_connections_removed": {"$sum": "$connections_removed"},
            "total_reactions": {"$sum": "$reactions_handled"},
            "total_edits": {"$sum": "$edits_handled"}
        }}
    ]
    
    all_time_stats = list(stats_collection.aggregate(pipeline))
    all_time_stats = all_time_stats[0] if all_time_stats else {}
    
    return {
        "total_connections": total_connections,
        "today": today_stats,
        "all_time": all_time_stats
    }

def create_group_selection_keyboard(owner_id: int, selected_groups: list = None):
    """Create inline keyboard for group selection"""
    if selected_groups is None:
        selected_groups = []
    
    connections = get_all_connections(owner_id)
    keyboard = []
    
    for group_id, group_info in connections.items():
        is_selected = group_id in selected_groups
        emoji = "✅" if is_selected else "⬜"
        button_text = f"{emoji} {group_info['name']}"
        
        # Truncate long group names
        if len(button_text) > 50:
            button_text = button_text[:47] + "..."
        
        keyboard.append([InlineKeyboardButton(
            button_text,
            callback_data=f"select_group_{group_id}"
        )])
    
    # Add action buttons
    if connections:
        keyboard.append([
            InlineKeyboardButton("🚀 Send to Selected", callback_data="send_to_selected"),
            InlineKeyboardButton("✅ Select All", callback_data="select_all")
        ])
        keyboard.append([
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_send")
        ])
    
    return InlineKeyboardMarkup(keyboard)

async def handle_group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle group selection inline buttons"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    callback_data = query.data
    
    if not is_owner(user_id):
        await query.edit_message_text("❌ You are not authorized to use this bot.")
        return
    
    # Get current pending message data
    pending_data = pending_messages.get(user_id)
    if not pending_data:
        await query.edit_message_text("❌ Message data expired. Please send the message again.")
        return
    
    selected_groups = pending_data.get("selected_groups", [])
    
    if callback_data.startswith("select_group_"):
        group_id = int(callback_data.split("_")[2])
        
        # Toggle selection
        if group_id in selected_groups:
            selected_groups.remove(group_id)
        else:
            selected_groups.append(group_id)
        
        # Update pending data
        pending_data["selected_groups"] = selected_groups
        pending_messages[user_id] = pending_data
        
        # Update keyboard
        keyboard = create_group_selection_keyboard(user_id, selected_groups)
        selected_count = len(selected_groups)
        
        await query.edit_message_text(
            f"📤 **Select Groups to Send Message**\n\n"
            f"📍 **Message Preview:**\n"
            f"{pending_data.get('preview', 'Media message')}\n\n"
            f"✅ **Selected:** {selected_count} group(s)\n"
            f"👇 Tap groups to select/deselect",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    
    elif callback_data == "select_all":
        # Select all groups
        connections = get_all_connections(user_id)
        selected_groups = list(connections.keys())
        pending_data["selected_groups"] = selected_groups
        pending_messages[user_id] = pending_data
        
        keyboard = create_group_selection_keyboard(user_id, selected_groups)
        await query.edit_message_text(
            f"📤 **Select Groups to Send Message**\n\n"
            f"📍 **Message Preview:**\n"
            f"{pending_data.get('preview', 'Media message')}\n\n"
            f"✅ **Selected:** {len(selected_groups)} group(s)\n"
            f"👇 Tap groups to select/deselect",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    
    elif callback_data == "send_to_selected":
        if not selected_groups:
            await query.answer("❌ Please select at least one group!", show_alert=True)
            return
        
        # Send message to selected groups
        successful_forwards = 0
        failed_forwards = []
        
        message_data = pending_data["message_data"]
        connections = get_all_connections(user_id)
        
        for group_id in selected_groups:
            try:
                if message_data["type"] == "text":
                    sent_message = await context.bot.send_message(
                        chat_id=group_id,
                        text=message_data["text"]
                    )
                elif message_data["type"] == "sticker":
                    sent_message = await context.bot.send_sticker(
                        chat_id=group_id,
                        sticker=message_data["sticker_id"]
                    )
                elif message_data["type"] == "photo":
                    sent_message = await context.bot.send_photo(
                        chat_id=group_id,
                        photo=message_data["photo_id"],
                        caption=message_data.get("caption", "")
                    )
                elif message_data["type"] == "video":
                    sent_message = await context.bot.send_video(
                        chat_id=group_id,
                        video=message_data["video_id"],
                        caption=message_data.get("caption", "")
                    )
                elif message_data["type"] == "document":
                    sent_message = await context.bot.send_document(
                        chat_id=group_id,
                        document=message_data["document_id"],
                        caption=message_data.get("caption", "")
                    )
                elif message_data["type"] == "audio":
                    sent_message = await context.bot.send_audio(
                        chat_id=group_id,
                        audio=message_data["audio_id"],
                        caption=message_data.get("caption", "")
                    )
                elif message_data["type"] == "voice":
                    sent_message = await context.bot.send_voice(
                        chat_id=group_id,
                        voice=message_data["voice_id"]
                    )
                elif message_data["type"] == "animation":
                    sent_message = await context.bot.send_animation(
                        chat_id=group_id,
                        animation=message_data["animation_id"],
                        caption=message_data.get("caption", "")
                    )
                else:
                    # Fallback to copy_message for other types
                    sent_message = await context.bot.copy_message(
                        chat_id=group_id,
                        from_chat_id=message_data["chat_id"],
                        message_id=message_data["message_id"]
                    )
                
                successful_forwards += 1
                
                # Store mapping for reactions - group message to private message
                mapping_key = f"{group_id}_{sent_message.message_id}"
                group_to_private_mappings[mapping_key] = {
                    "private_chat_id": message_data["chat_id"],
                    "private_message_id": message_data["message_id"]
                }
                
                # Store edit mapping - private message to group message
                edit_key = f"{message_data['chat_id']}_{message_data['message_id']}"
                if edit_key not in edit_mappings:
                    edit_mappings[edit_key] = []
                edit_mappings[edit_key].append({
                    "group_id": group_id,
                    "group_message_id": sent_message.message_id
                })
                
                logger.info(f"📝 Stored group-to-private mapping: {mapping_key} -> {message_data['chat_id']}_{message_data['message_id']}")
                logger.info(f"📝 Stored edit mapping: {edit_key} -> {group_id}_{sent_message.message_id}")
                
                # Update stats for each successful send
                update_stats(user_id, "message_sent")
                
            except Exception as e:
                logger.error(f"Failed to forward to group {group_id}: {e}")
                group_name = connections.get(group_id, {}).get('name', f'ID: {group_id}')
                failed_forwards.append(f"{group_name} (ID: {group_id})")
        
        # Send summary to owner
        summary_message = f"✅ Message sent to {successful_forwards} group(s)!"
        if failed_forwards:
            summary_message += f"\n❌ Failed in {len(failed_forwards)} group(s):\n" + "\n".join(failed_forwards)
        
        await query.edit_message_text(summary_message)
        
        # Clean up pending message
        if user_id in pending_messages:
            del pending_messages[user_id]
    
    elif callback_data == "cancel_send":
        await query.edit_message_text("❌ Message sending cancelled.")
        # Clean up pending message
        if user_id in pending_messages:
            del pending_messages[user_id]

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
            "- Send any message and select groups to send to\n"
            "- When someone replies to BOT'S messages in connected groups, I'll forward them to you\n"
            "- When someone mentions/tags the bot in groups, I'll forward those messages to you\n"
            "- Reply to those messages and I'll send your response back!\n"
            "- Edit your messages and I'll update them in groups automatically\n"
            "- React to messages and I'll mirror reactions in groups\n"
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
    
    # Start building the message
    message_parts = []
    total_groups = len(connections)
    total_members = 0
    
    # Add summary at the top
    message_parts.append("📊 *Connected Groups*")
    message_parts.append("")
    message_parts.append(f"📈 *Total Groups Connected:* {total_groups}")
    
    # Get fresh info for each group
    for group_id, group_info in connections.items():
        try:
            # Try to get updated group info
            chat = await context.bot.get_chat(group_id)
            member_count = getattr(chat, 'member_count', 'Unknown')
            if isinstance(member_count, int):
                total_members += member_count
            
            group_type = "Supergroup" if chat.type == "supergroup" else "Group"
            username = f"@{chat.username}" if chat.username else "No username"
            
            # Update cache and database with fresh info
            active_groups[group_id] = {
                'name': chat.title,
                'type': group_type,
                'member_count': member_count,
                'username': username
            }
            
            # Update database with fresh info - FIXED: Added missing closing brace
            connections_collection.update_one(
                {"owner_id": update.message.from_user.id, "group_id": group_id},
                {"$set": {
                    "group_name": chat.title,
                    "group_username": username
                }}
            )
            
            # Escape any Markdown characters in the group name
            group_name_escaped = chat.title.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[')
            
            message_parts.append(f"🏷️ *{group_name_escaped}*")
            message_parts.append(f"   📝 Type: {group_type}")
            message_parts.append(f"   🆔 ID: `{group_id}`")
            message_parts.append(f"   👥 Members: {member_count}")
            message_parts.append(f"   🔗 {username}")
            message_parts.append(f"   ➖ /disconnect_{group_id}")
            message_parts.append("")
            
        except Exception as e:
            logger.error(f"Error getting chat info for group {group_id}: {e}")
            # Use cached info if available
            if group_id in active_groups:
                group_data = active_groups[group_id]
                member_count = group_data.get('member_count', 'Unknown')
                if isinstance(member_count, int):
                    total_members += member_count
                
                # Escape any Markdown characters in the group name
                group_name_escaped = group_data['name'].replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[')
                
                message_parts.append(f"🏷️ *{group_name_escaped}*")
                message_parts.append(f"   📝 Type: {group_data['type']}")
                message_parts.append(f"   🆔 ID: `{group_id}`")
                message_parts.append(f"   👥 Members: {member_count}")
                message_parts.append(f"   🔗 {group_data.get('username', 'No username')}")
                message_parts.append(f"   ⚠️ Could not refresh info")
                message_parts.append(f"   ➖ /disconnect_{group_id}")
                message_parts.append("")
            else:
                # Escape any Markdown characters in the group name
                group_name_escaped = group_info['name'].replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[')
                
                message_parts.append(f"🏷️ *{group_name_escaped}*")
                message_parts.append(f"   🆔 ID: `{group_id}`")
                message_parts.append(f"   ⚠️ Could not fetch group info")
                message_parts.append(f"   ➖ /disconnect_{group_id}")
                message_parts.append("")
    
    # Add total members to summary if we have the data
    if total_members > 0:
        message_parts.insert(2, f"👥 *Total Members:* {total_members}")
        message_parts.insert(3, "")  # Add empty line for spacing
    
    message_parts.append("💡 Use /disconnect <group_id> to remove a group")
    
    # Join all parts and send
    final_message = "\n".join(message_parts)
    
    # If message is too long, split it
    if len(final_message) > 4000:
        # Split into chunks of 4000 characters
        chunks = [final_message[i:i+4000] for i in range(0, len(final_message), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode='Markdown')
    else:
        await update.message.reply_text(final_message, parse_mode='Markdown')

async def botstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /botstats command for detailed statistics"""
    if update.message.chat.type != "private":
        return
    
    if not is_owner(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    stats = get_bot_stats(update.message.from_user.id)
    
    message_parts = []
    message_parts.append("🤖 *Bot Statistics*")
    message_parts.append("")
    
    # Overall stats
    message_parts.append("📈 *Overall Statistics*")
    message_parts.append(f"• Total Active Groups: `{stats['total_connections']}`")
    
    if stats['all_time']:
        message_parts.append(f"• Total Messages Sent: `{stats['all_time'].get('total_messages', 0)}`")
        message_parts.append(f"• Total Replies Handled: `{stats['all_time'].get('total_replies', 0)}`")
        message_parts.append(f"• Total Reactions Handled: `{stats['all_time'].get('total_reactions', 0)}`")
        message_parts.append(f"• Total Edits Handled: `{stats['all_time'].get('total_edits', 0)}`")
        message_parts.append(f"• Total Connections Added: `{stats['all_time'].get('total_connections_added', 0)}`")
        message_parts.append(f"• Total Connections Removed: `{stats['all_time'].get('total_connections_removed', 0)}`")
    else:
        message_parts.append("• No historical data available")
    
    # Today's stats
    message_parts.append("")
    message_parts.append("📊 *Today's Statistics*")
    if stats['today']:
        message_parts.append(f"• Messages Sent: `{stats['today'].get('messages_sent', 0)}`")
        message_parts.append(f"• Replies Handled: `{stats['today'].get('replies_handled', 0)}`")
        message_parts.append(f"• Reactions Handled: `{stats['today'].get('reactions_handled', 0)}`")
        message_parts.append(f"• Edits Handled: `{stats['today'].get('edits_handled', 0)}`")
        message_parts.append(f"• Connections Added: `{stats['today'].get('connections_added', 0)}`")
        message_parts.append(f"• Connections Removed: `{stats['today'].get('connections_removed', 0)}`")
    else:
        message_parts.append("• No activity today")
    
    # Database info
    total_db_connections = connections_collection.count_documents({
        "owner_id": update.message.from_user.id
    })
    active_db_connections = connections_collection.count_documents({
        "owner_id": update.message.from_user.id,
        "is_active": True
    })
    
    message_parts.append("")
    message_parts.append("💾 *Database*")
    message_parts.append(f"• Total Records: `{total_db_connections}`")
    message_parts.append(f"• Active Connections: `{active_db_connections}`")
    
    final_message = "\n".join(message_parts)
    
    await update.message.reply_text(final_message, parse_mode='Markdown')

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
                    sent_message = await context.bot.send_sticker(
                        chat_id=target_group_id,
                        sticker=update.message.sticker.file_id,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.text:
                    sent_message = await context.bot.send_message(
                        chat_id=target_group_id,
                        text=update.message.text,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.photo:
                    sent_message = await context.bot.send_photo(
                        chat_id=target_group_id,
                        photo=update.message.photo[-1].file_id,
                        caption=update.message.caption,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.video:
                    sent_message = await context.bot.send_video(
                        chat_id=target_group_id,
                        video=update.message.video.file_id,
                        caption=update.message.caption,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.document:
                    sent_message = await context.bot.send_document(
                        chat_id=target_group_id,
                        document=update.message.document.file_id,
                        caption=update.message.caption,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.audio:
                    sent_message = await context.bot.send_audio(
                        chat_id=target_group_id,
                        audio=update.message.audio.file_id,
                        caption=update.message.caption,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.voice:
                    sent_message = await context.bot.send_voice(
                        chat_id=target_group_id,
                        voice=update.message.voice.file_id,
                        reply_to_message_id=original_group_msg_id
                    )
                elif update.message.animation:
                    sent_message = await context.bot.send_animation(
                        chat_id=target_group_id,
                        animation=update.message.animation.file_id,
                        caption=update.message.caption,
                        reply_to_message_id=original_group_msg_id
                    )
                else:
                    # For other media types
                    sent_message = await context.bot.copy_message(
                        chat_id=target_group_id,
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id,
                        reply_to_message_id=original_group_msg_id
                    )
                
                # Store mapping for reactions - your reply in group to your private message
                mapping_key = f"{target_group_id}_{sent_message.message_id}"
                group_to_private_mappings[mapping_key] = {
                    "private_chat_id": update.message.chat_id,
                    "private_message_id": update.message.message_id
                }
                
                # Store edit mapping for the reply
                edit_key = f"{update.message.chat_id}_{update.message.message_id}"
                if edit_key not in edit_mappings:
                    edit_mappings[edit_key] = []
                edit_mappings[edit_key].append({
                    "group_id": target_group_id,
                    "group_message_id": sent_message.message_id
                })
                
                logger.info(f"📝 Stored reply mapping: {mapping_key} -> {update.message.chat_id}_{update.message.message_id}")
                logger.info(f"📝 Stored edit mapping for reply: {edit_key} -> {target_group_id}_{sent_message.message_id}")
                
                # Also store the reverse mapping for the original group message that was replied to
                original_mapping_key = f"{target_group_id}_{original_group_msg_id}"
                if original_mapping_key not in group_to_private_mappings:
                    group_to_private_mappings[original_mapping_key] = {
                        "private_chat_id": original_private_msg.chat_id,
                        "private_message_id": original_private_msg.message_id
                    }
                    logger.info(f"📝 Stored original message mapping: {original_mapping_key}")
                
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
    
    # Normal message forwarding (not a reply) - show group selection
    message_data = {
        "type": "text",
        "chat_id": update.message.chat_id,
        "message_id": update.message.message_id,
        "text": update.message.text if update.message.text else "Media message"
    }
    
    # Determine message type and prepare data
    if update.message.sticker:
        message_data = {
            "type": "sticker",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "sticker_id": update.message.sticker.file_id,
            "preview": "🎨 Sticker"
        }
    elif update.message.photo:
        message_data = {
            "type": "photo",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "photo_id": update.message.photo[-1].file_id,
            "caption": update.message.caption,
            "preview": "🖼️ Photo" + (f" - {update.message.caption}" if update.message.caption else "")
        }
    elif update.message.video:
        message_data = {
            "type": "video",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "video_id": update.message.video.file_id,
            "caption": update.message.caption,
            "preview": "🎥 Video" + (f" - {update.message.caption}" if update.message.caption else "")
        }
    elif update.message.document:
        message_data = {
            "type": "document",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "document_id": update.message.document.file_id,
            "caption": update.message.caption,
            "preview": "📄 Document" + (f" - {update.message.caption}" if update.message.caption else "")
        }
    elif update.message.audio:
        message_data = {
            "type": "audio",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "audio_id": update.message.audio.file_id,
            "caption": update.message.caption,
            "preview": "🎵 Audio" + (f" - {update.message.caption}" if update.message.caption else "")
        }
    elif update.message.voice:
        message_data = {
            "type": "voice",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "voice_id": update.message.voice.file_id,
            "preview": "🎤 Voice Message"
        }
    elif update.message.animation:
        message_data = {
            "type": "animation",
            "chat_id": update.message.chat_id,
            "message_id": update.message.message_id,
            "animation_id": update.message.animation.file_id,
            "caption": update.message.caption,
            "preview": "🎬 Animation" + (f" - {update.message.caption}" if update.message.caption else "")
        }
    elif update.message.text:
        # Truncate long text for preview
        preview = update.message.text
        if len(preview) > 100:
            preview = preview[:97] + "..."
        message_data["preview"] = preview
    
    # Store pending message
    pending_messages[user_id] = {
        "message_data": message_data,
        "selected_groups": []  # Start with no groups selected
    }
    
    # Create and send group selection keyboard
    keyboard = create_group_selection_keyboard(user_id)
    
    await update.message.reply_text(
        f"📤 **Select Groups to Send Message**\n\n"
        f"📍 **Message Preview:**\n"
        f"{message_data.get('preview', 'Media message')}\n\n"
        f"✅ **Selected:** 0 group(s)\n"
        f"👇 Tap groups to select/deselect",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def handle_private_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edited private messages from owner"""
    # Owner check
    if not is_owner(update.edited_message.from_user.id):
        return
    
    user_id = update.edited_message.from_user.id
    private_chat_id = update.edited_message.chat_id
    private_message_id = update.edited_message.message_id
    
    # Check if this edited message has group mappings
    edit_key = f"{private_chat_id}_{private_message_id}"
    
    logger.info(f"🔍 Checking edit mappings for key: {edit_key}")
    logger.info(f"🔍 Available edit keys: {list(edit_mappings.keys())}")
    
    if edit_key in edit_mappings:
        group_messages = edit_mappings[edit_key]
        successful_edits = 0
        failed_edits = []
        
        logger.info(f"🔍 Found {len(group_messages)} group messages to edit")
        
        for group_msg in group_messages:
            group_id = group_msg["group_id"]
            group_message_id = group_msg["group_message_id"]
            
            try:
                # Edit the corresponding group message
                if update.edited_message.text:
                    await context.bot.edit_message_text(
                        chat_id=group_id,
                        message_id=group_message_id,
                        text=update.edited_message.text
                    )
                    successful_edits += 1
                    logger.info(f"✅ Edited group message: {group_id}_{group_message_id}")
                # Note: Currently only text editing is supported
                # For other message types, we'd need different edit methods
                
            except Exception as e:
                logger.error(f"Failed to edit group message {group_id}_{group_message_id}: {e}")
                failed_edits.append(f"Group {group_id} (Message {group_message_id})")
        
        # Update stats
        if successful_edits > 0:
            update_stats(user_id, "edit_handled")
            logger.info(f"✅ Successfully edited {successful_edits} group message(s)")
            
            # Send confirmation to owner
            await context.bot.send_message(
                chat_id=private_chat_id,
                text=f"✅ Message updated in {successful_edits} group(s)!"
            )
        
        if failed_edits:
            logger.warning(f"❌ Failed to edit {len(failed_edits)} group message(s): {failed_edits}")
            await context.bot.send_message(
                chat_id=private_chat_id,
                text=f"❌ Failed to update message in {len(failed_edits)} group(s)"
            )
    else:
        logger.info(f"ℹ️ No edit mapping found for private message: {edit_key}")
        await context.bot.send_message(
            chat_id=private_chat_id,
            text="❌ No edit mapping found for this message. Only messages sent through the bot can be edited."
        )

async def handle_bot_related_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ONLY messages in connected groups that are replies to bot OR mention the bot"""
    # Only process messages in connected groups
    group_id = update.message.chat.id
    
    # Get owner's connections to verify this is a connected group
    owner_connections = get_all_connections(int(OWNER_ID))
    if group_id not in owner_connections:
        return
    
    # Get bot info
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    bot_id = bot_info.id
    
    # Check if this message is related to the bot
    is_bot_related = False
    reason = ""
    
    # Case 1: Message is a reply to a message sent by the bot
    if update.message.reply_to_message:
        replied_to_user = update.message.reply_to_message.from_user
        if replied_to_user and replied_to_user.id == bot_id:
            is_bot_related = True
            reason = "reply to bot's message"
    
    # Case 2: Message mentions the bot (via username or direct mention)
    if not is_bot_related and update.message.text:
        # Check for bot username mention
        if bot_username and f"@{bot_username}" in update.message.text:
            is_bot_related = True
            reason = f"mentioned @{bot_username}"
    
    # Case 3: Check for bot mention in caption (for media messages)
    if not is_bot_related and update.message.caption and bot_username:
        if f"@{bot_username}" in update.message.caption:
            is_bot_related = True
            reason = f"mentioned @{bot_username} in caption"
    
    # Case 4: Check if bot is directly mentioned in entities
    if not is_bot_related and update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention" and bot_username:
                mention_text = update.message.text[entity.offset:entity.offset + entity.length]
                if mention_text == f"@{bot_username}":
                    is_bot_related = True
                    reason = "direct mention"
                    break
    
    # If not bot-related, ignore the message
    if not is_bot_related:
        return
    
    user_id = update.message.from_user.id
    group_name = owner_connections[group_id]['name']
    
    try:
        # Get info about who sent the message
        user_name = update.message.from_user.first_name
        if update.message.from_user.username:
            user_name = f"@{update.message.from_user.username}"
        
        # If this is a reply to bot's message, first forward the message that was replied to
        if update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            try:
                # Forward the message that was replied to (for context)
                replied_msg = await context.bot.forward_message(
                    chat_id=OWNER_ID,
                    from_chat_id=group_id,
                    message_id=update.message.reply_to_message.message_id
                )
                
                # Add a context message to show this is what the user replied to
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"↩️ **User replied to this message:**",
                    reply_to_message_id=replied_msg.message_id
                )
            except Exception as e:
                logger.error(f"Failed to forward replied-to message: {e}")
        
        # Forward the actual message that the user sent in the group
        forwarded_msg = await context.bot.forward_message(
            chat_id=OWNER_ID,
            from_chat_id=group_id,
            message_id=update.message.message_id
        )
        
        # Store mapping for reply functionality - use the forwarded message ID
        message_mappings[forwarded_msg.message_id] = {
            'original_group_message_id': update.message.message_id,
            'sender': update.message.from_user,
            'group_id': group_id
        }
        
        # Also store for reactions - group message to private forwarded message
        reaction_mappings[f"{group_id}_{update.message.message_id}"] = {
            "private_chat_id": OWNER_ID,
            "private_message_id": forwarded_msg.message_id
        }
        
        logger.info(f"📩 Forwarded bot-related message from {user_name} in {group_name} ({reason})")
        
    except Exception as e:
        logger.error(f"Failed to forward bot-related message to owner: {e}")

async def handle_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle message reactions in both private and group chats"""
    if not update.message_reaction:
        return
    
    user_id = update.message_reaction.user.id
    chat_id = update.message_reaction.chat.id
    message_id = update.message_reaction.message_id
    new_reaction = update.message_reaction.new_reaction
    
    logger.info(f"🔔 Reaction detected: User {user_id}, Chat {chat_id}, Message {message_id}, Reactions: {new_reaction}")
    
    # Handle reactions in private chat (from owner)
    if update.message_reaction.chat.type == "private":
        if not is_owner(user_id):
            return
        
        logger.info(f"👤 Owner reaction in private chat on message {message_id}")
        
        # Check if this is a reaction to a forwarded group message (reply)
        if message_id in message_mappings:
            mapping = message_mappings[message_id]
            group_id = mapping['group_id']
            group_message_id = mapping['original_group_message_id']
            
            try:
                # Set the same reaction in the group on the original message
                await context.bot.set_message_reaction(
                    chat_id=group_id,
                    message_id=group_message_id,
                    reaction=new_reaction
                )
                logger.info(f"✅ Mirrored reaction from private to group reply: {group_id}_{group_message_id}")
                update_stats(user_id, "reaction_handled")
                
            except Exception as e:
                logger.error(f"❌ Failed to set reaction in group: {e}")
        
        # Check if this is a reaction to a message that was sent to groups
        # Look in group_to_private_mappings for any group messages that correspond to this private message
        mapping_found = False
        for key, mapping in group_to_private_mappings.items():
            if (mapping["private_chat_id"] == chat_id and 
                mapping["private_message_id"] == message_id):
                
                # Extract group_id and group_message_id from key
                group_id, group_message_id = key.split("_")
                group_id = int(group_id)
                group_message_id = int(group_message_id)
                
                try:
                    # Set the same reaction in the group
                    await context.bot.set_message_reaction(
                        chat_id=group_id,
                        message_id=group_message_id,
                        reaction=new_reaction
                    )
                    logger.info(f"✅ Mirrored reaction from private to group message: {group_id}_{group_message_id}")
                    update_stats(user_id, "reaction_handled")
                    mapping_found = True
                    
                except Exception as e:
                    logger.error(f"❌ Failed to set reaction in group for sent message: {e}")
        
        if not mapping_found:
            logger.warning(f"⚠️ No mapping found for private message {chat_id}_{message_id}")
    
    # Handle reactions in group (from users to bot's messages)
    elif update.message_reaction.chat.type in ["group", "supergroup"]:
        logger.info(f"👥 User reaction in group {chat_id} on message {message_id}")
        
        # Check if this is a reaction to a message that we have mapped
        # We don't need to check if the bot sent it - we rely on our mappings
        owner_connections = get_all_connections(int(OWNER_ID))
        if chat_id not in owner_connections:
            logger.warning(f"⚠️ Group {chat_id} not in connected groups")
            return
        
        reaction_mirrored = False
        
        # Check if this is a reply that we have mapped
        reaction_key = f"{chat_id}_{message_id}"
        if reaction_key in reaction_mappings:
            mapping = reaction_mappings[reaction_key]
            try:
                # Set the same reaction in private chat
                await context.bot.set_message_reaction(
                    chat_id=mapping["private_chat_id"],
                    message_id=mapping["private_message_id"],
                    reaction=new_reaction
                )
                logger.info(f"✅ Mirrored reaction from group reply to private: {reaction_key}")
                update_stats(int(OWNER_ID), "reaction_handled")
                reaction_mirrored = True
            except Exception as e:
                logger.error(f"❌ Failed to set reaction in private for reply: {e}")
        
        # Check if this is a regular message we sent to the group
        group_key = f"{chat_id}_{message_id}"
        if group_key in group_to_private_mappings:
            mapping = group_to_private_mappings[group_key]
            try:
                # Set the same reaction in private chat
                await context.bot.set_message_reaction(
                    chat_id=mapping["private_chat_id"],
                    message_id=mapping["private_message_id"],
                    reaction=new_reaction
                )
                logger.info(f"✅ Mirrored reaction from group to private: {group_key}")
                update_stats(int(OWNER_ID), "reaction_handled")
                reaction_mirrored = True
            except Exception as e:
                logger.error(f"❌ Failed to set reaction in private for sent message: {e}")
        
        if not reaction_mirrored:
            logger.warning(f"⚠️ No mapping found for group message {chat_id}_{message_id}")

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
        "- Send messages and select specific groups to send to\n"
        "- When someone replies to BOT'S messages in connected groups, I'll forward them to you\n"
        "- When someone mentions/tags the bot in groups, I'll forward those messages to you\n"
        "- Reply to those messages and I'll send your response back!\n"
        "- Edit your messages and I'll update them in groups automatically\n"
        "- React to messages and I'll mirror reactions in groups\n"
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

# Handle group selection callbacks
application.add_handler(CallbackQueryHandler(handle_group_selection, pattern="^(select_group_|send_to_selected|select_all|cancel_send)"))

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

# Handle edited private messages from owner - SIMPLIFIED FILTER
application.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & filters.TEXT,
    handle_private_edit
))

# Handle ONLY group messages that are replies to bot OR mention the bot
application.add_handler(MessageHandler(
    filters.ChatType.GROUPS & ~filters.COMMAND,
    handle_bot_related_group_messages
))

# Handle message reactions with dedicated handler
application.add_handler(MessageReactionHandler(handle_message_reaction))

def start_bot():
    """Start Telegram bot in polling mode"""
    logger.info("Starting Telegram bot in polling mode...")
    logger.info(f"Owner ID: {OWNER_ID}")
    logger.info(f"MongoDB URI: {MONGO_URI}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start bot in main thread
    logger.info("Starting bot...")
    start_bot()