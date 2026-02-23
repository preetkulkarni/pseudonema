import os
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, cast, AsyncGenerator, Any

from fastapi import FastAPI, Request, Response, Header
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from supabase import create_async_client, AsyncClient
from tavily import AsyncTavilyClient
from google import genai

from config import ConfigManager, Trend
from trend_engine import TrendEngine

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.getenv("WEBHOOK_URL")
SECRET_TOKEN: Optional[str] = os.getenv("BOT_SECRET_TOKEN")

SUPABASE_URL: Optional[str] = os.getenv("SUPABASE_URL")
SUPABASE_KEY: Optional[str] = os.getenv("SUPABASE_KEY")
TAVILY_API_KEY: Optional[str] = os.getenv("TAVILY_API_KEY")
GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")

# Strict Type Handling for ADMIN_ID
ADMIN_ID: int
try:
    env_admin = os.getenv("ADMIN_ID")
    if not env_admin:
        raise ValueError("ADMIN_ID env var is empty")
    ADMIN_ID = int(env_admin)
except (TypeError, ValueError) as e:
    raise ValueError(f"CRITICAL: ADMIN_ID is missing or invalid! Application cannot start. Error: {e}")

# --- Global State ---
_ptb_app: Optional[Application] = None
_config_mgr: Optional[ConfigManager] = None
_trend_engine: Optional[TrendEngine] = None

# --- Helper Functions ---

def build_dynamic_keyboard(trend_names: List[str]) -> InlineKeyboardMarkup:
    """Creates an interactive inline keyboard from dynamically generated trends."""
    keyboard: List[List[InlineKeyboardButton]] = []
    
    for i in range(0, len(trend_names), 2):
        row = []
        for topic in trend_names[i:i+2]:
            # Telegram callback_data is limited to 64 bytes. Truncate if necessary.
            safe_topic = topic[:45] 
            row.append(InlineKeyboardButton(text=topic, callback_data=f"scout_{safe_topic}"))
        keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton("🔄 Generate New Trends", callback_data="refresh_trending")])
    return InlineKeyboardMarkup(keyboard)

async def trigger_trend_generation(message: Any) -> None:
    """Executes the trend engine and updates the UI."""
    if not _config_mgr or not _trend_engine:
        await message.reply_text("System not fully initialized.")
        return

    status_msg = await message.reply_text("🔥 *Scanning the web for live tech trends...*", parse_mode="Markdown")

    try:
        # Initialize config manager
        await _config_mgr.initialize()
        # 1. Get targets dynamically from ConfigManager
        num_trends, category, subcat, topics, urls = _config_mgr.get_trends()
        
        # 2. Run the TrendEngine
        trends: List[Trend] = await _trend_engine.fetch_and_generate_trends(
            num_trends=num_trends,
            category=category,
            subcategory=subcat,
            topics=topics,
            urls=urls
        )

        if trends:
            trend_names = [t.name for t in trends]
            keyboard = build_dynamic_keyboard(trend_names)
            
            # Format a nice summary to show the user what was searched
            topics_str = ", ".join(topics) if topics else "general news"
            text = f"🔥 *Live Trends Discovered*\n\n**Category:** {category} > {subcat}\n**Focus:** {topics_str}\n\n👇 Select a trend below:"
            
            await status_msg.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await status_msg.edit_text("⚠️ *Scan Complete*, but no significant trends were extracted.", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Trend generation failed: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ *Error generating trends*: {str(e)}", parse_mode="Markdown")

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    user_name = update.effective_user.first_name
    welcome_text = (
        f"👋 *Hello, {user_name}!* \n\n"
        "I am your AI Trend Analyzer.\n\n"
        "Use `/trending` to scan the web and extract the latest emerging tech trends."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
        
    text = (
        "🤖 *Bot Commands:*\n"
        "`/start` - Main menu\n"
        "`/trending` - Scan web for live trends"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await trigger_trend_generation(update.message)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not update.effective_message:
        return

    if update.effective_user.id != ADMIN_ID:
        await query.answer("⛔ Access Denied", show_alert=True)
        return

    data = query.data
    if not data:
        return

    if data.startswith("scout_"):
        topic = data.replace("scout_", "")
        # Placeholder alert since scouting is disabled
        await query.answer(f"Pipeline paused at Trend Engine.\n\nSelected: {topic}", show_alert=True)
    elif data == "refresh_trending":
        await query.answer() # Acknowledge the click
        await trigger_trend_generation(update.effective_message)

# --- Application Lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _ptb_app, _config_mgr, _trend_engine
    
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        yield
        return

    # 1. Initialize API Clients
    if not all([SUPABASE_URL, SUPABASE_KEY, TAVILY_API_KEY, GEMINI_API_KEY]):
        logger.warning("Missing one or more API keys (Supabase, Tavily, Gemini). Trend Engine may fail.")

    db_client: AsyncClient = await create_async_client(
        SUPABASE_URL or "", SUPABASE_KEY or ""
    )
    tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
    llm_client = genai.Client(api_key=GEMINI_API_KEY)

    # 2. Initialize Config Manager
    logger.info("Initializing Config Manager...")
    _config_mgr = ConfigManager()
    await _config_mgr.initialize()

    # 3. Initialize Trend Engine
    logger.info("Initializing Trend Engine...")
    _trend_engine = TrendEngine(
        tavily_client=tavily_client,
        llm_client=llm_client,
        db_client=db_client
    )

    # 4. Build Telegram Application
    _ptb_app = cast(Application, Application.builder().token(TOKEN).build())
    admin_only = filters.User(user_id=ADMIN_ID)

    _ptb_app.add_handler(CommandHandler("start", start_command, filters=admin_only))
    _ptb_app.add_handler(CommandHandler("help", help_command, filters=admin_only))
    _ptb_app.add_handler(CommandHandler("trending", trending_command, filters=admin_only))
    _ptb_app.add_handler(CallbackQueryHandler(button_handler)) 

    await _ptb_app.initialize()
    await _ptb_app.start()
    logger.info("Telegram Bot Started")

    if WEBHOOK_URL and SECRET_TOKEN:
        webhook_path = f"{WEBHOOK_URL}/webhook"
        logger.info(f"Setting webhook to: {webhook_path}")
        await _ptb_app.bot.set_webhook(url=webhook_path, secret_token=SECRET_TOKEN)
    elif WEBHOOK_URL and not SECRET_TOKEN:
        logger.warning("Webhook URL provided but NO SECRET TOKEN. This is insecure!")
    
    app.state.ptb_app = _ptb_app
    
    yield 

    logger.info("Stopping Application...")
    if _ptb_app:
        await _ptb_app.stop()
        await _ptb_app.shutdown()

# --- FastAPI App ---

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(
    request: Request, 
    x_telegram_bot_api_secret_token: Optional[str] = Header(None, alias="X-Telegram-Bot-Api-Secret-Token")
) -> Response:
    if x_telegram_bot_api_secret_token != SECRET_TOKEN:
        logger.warning("Unauthorized webhook attempt!")
        return Response(status_code=403, content="Forbidden")

    try:
        data = await request.json()
        raw_app: Any = getattr(request.app.state, "ptb_app", None)
        ptb_app = cast(Optional[Application], raw_app)

        if not ptb_app:
            return Response(status_code=500, content="Bot not initialized")
            
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(status_code=500)

@app.get("/")
async def health_check() -> dict:
    return {"status": "active", "mode": "secure_webhook"}