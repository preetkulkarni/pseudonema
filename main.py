import os
import logging
import random
import asyncio
from typing import List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from scout_agent import ScoutAgent

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

ptb_application = None

TRENDING_TOPICS = [
    "Agentic AI Patterns",
    "Rust vs C++ Performance",
    "RAG Optimization",
    "Post-Quantum Cryptography",
    "Kubernetes Security",
    "WebAssembly (Wasm)",
    "Local LLM Deployment",
    "Zero Trust Architecture",
    "Graph Neural Networks",
    "Platform Engineering"
]

# --- Helper Functions ---

def get_trending_keyboard():
    """
    Creates an interactive inline keyboard with 4 random trending topics.
    """
    # Select 4 random topics to keep the interface fresh
    selected_topics = random.sample(TRENDING_TOPICS, min(4, len(TRENDING_TOPICS)))
    
    keyboard = []
    # Create rows of 2 buttons each
    for i in range(0, len(selected_topics), 2):
        row = [
            InlineKeyboardButton(text=topic, callback_data=f"scout_{topic}") 
            for topic in selected_topics[i:i+2]
        ]
        keyboard.append(row)
    
    # Add a "Refresh" button at the bottom
    keyboard.append([InlineKeyboardButton("🔄 Refresh Topics", callback_data="refresh_trending")])
    
    return InlineKeyboardMarkup(keyboard)

async def execute_scouting_mission(update: Update, topic: str):
    """
    The core logic that runs the ScoutAgent and updates the user.
    Used by both /scout command and button clicks.
    """
    # Determine where to send the message (works for both Message and CallbackQuery updates)
    message = update.effective_message
    
    # Send an initial "Searching..." status message
    status_msg = await message.reply_text(
        f"🕵️ *Scout Agent Deployed*\nTarget: `{topic}`\n\nConnecting to feed sources...", 
        parse_mode="Markdown"
    )

    try:
        # Initialize the agent (this triggers the Google Sheet fetch internally)
        agent = ScoutAgent()
        
        # Run the heavy lifting (returns session_id or 0/None on failure)
        session_id = await agent.run_scout(topic)
        
        if session_id:
            await status_msg.edit_text(
                f"✅ *Mission Complete!*\n\n"
                f"**Topic:** {topic}\n"
                f"**Session ID:** `{session_id}`\n\n"
                f"I have saved the relevant articles to the database.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ Scout finished, but found no articles matching *{topic}* in the provided feeds.", 
                parse_mode="Markdown"
            )
            
    except Exception as e:
        logger.error(f"Scouting error for topic '{topic}': {e}", exc_info=True)
        await status_msg.edit_text(f"❌ *System Error*: {str(e)}", parse_mode="Markdown")

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - Shows welcome message and trending buttons.
    """
    user_name = update.effective_user.first_name
    welcome_text = (
        f"👋 *Hello, {user_name}!* \n\n"
        "I am your AI News Scout. I gather and summarize tech news "
        "from your configured RSS feeds and Reddit.\n\n"
        "👇 *Tap a topic below* to start scouting, or use `/scout <topic>`."
    )
    
    await update.message.reply_text(
        welcome_text, 
        reply_markup=get_trending_keyboard(), 
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help - Explains bot usage.
    """
    text = (
        "🤖 *Bot Commands:*\n"
        "`/start` - Main menu & trending topics\n"
        "`/scout <topic>` - Manual search (e.g., `/scout Docker`)\n"
        "`/trending` - Refresh the topic list"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /trending - Explicitly shows the trending menu.
    """
    await update.message.reply_text(
        "🔥 *Trending Tech Topics:*",
        reply_markup=get_trending_keyboard(),
        parse_mode="Markdown"
    )

async def scout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /scout <topic> - Handles manual text input.
    """
    if not context.args:
        await update.message.reply_text("⚠️ Please provide a topic.\nExample: `/scout Generative AI`", parse_mode="Markdown")
        return

    topic = " ".join(context.args)
    await execute_scouting_mission(update, topic)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all inline button clicks.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    
    if data.startswith("scout_"):
        # Extract topic from "scout_TopicName"
        topic = data.replace("scout_", "")
        await execute_scouting_mission(update, topic)
        
    elif data == "refresh_trending":
        # Edit the message with a new random set of buttons
        await query.edit_message_reply_markup(reply_markup=get_trending_keyboard())

# --- Application Lifecycle (FastAPI + Telegram) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the startup and shutdown of the Telegram bot alongside FastAPI.
    """
    global ptb_application
    
    if not TOKEN:
        logger.error("❌ No TELEGRAM_BOT_TOKEN found in environment variables!")
        yield
        return

    # 1. Initialize Bot
    ptb_application = Application.builder().token(TOKEN).build()

    # 2. Register Handlers
    ptb_application.add_handler(CommandHandler("start", start_command))
    ptb_application.add_handler(CommandHandler("help", help_command))
    ptb_application.add_handler(CommandHandler("trending", trending_command))
    ptb_application.add_handler(CommandHandler("scout", scout_command))
    ptb_application.add_handler(CallbackQueryHandler(button_handler))

    # 3. Start Bot
    await ptb_application.initialize()
    await ptb_application.start()
    
    # 4. Webhook Setup (Production) vs Polling (Local)
    if WEBHOOK_URL:
        webhook_path = f"{WEBHOOK_URL}/webhook"
        logger.info(f"🌍 Setting webhook to: {webhook_path}")
        await ptb_application.bot.set_webhook(url=webhook_path)
    else:
        logger.info("⚠️ No WEBHOOK_URL found. Ensure you are running in polling mode or setting it manually.")

    yield  # FastAPI app runs while this yields

    # 5. Shutdown Logic
    if ptb_application:
        logger.info("🛑 Stopping Telegram Bot...")
        await ptb_application.stop()
        await ptb_application.shutdown()

# --- FastAPI App Definition ---

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Endpoint that receives updates from Telegram servers.
    """
    try:
        data = await request.json()
        # Verify application is initialized
        if not ptb_application:
            return Response(status_code=500, content="Bot not initialized")
            
        update = Update.de_json(data, ptb_application.bot)
        await ptb_application.process_update(update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status_code=500)

@app.get("/")
async def health_check():
    """
    Simple health check for uptime monitoring.
    """
    return {"status": "active", "service": "Scout Agent Bot v1.0"}