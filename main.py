import os
import html
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, cast, AsyncGenerator, Any, Dict

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

# Cache the latest trends in memory 
_latest_trends: Dict[str, Trend] = {} 

# --- UI / Keyboard Generators ---

def build_trends_list_keyboard(trends: List[Trend]) -> InlineKeyboardMarkup:
    """Creates the main menu keyboard listing all generated trends using their UUIDs."""
    keyboard: List[List[InlineKeyboardButton]] = []
    
    # Create rows of 1 button each for better readability of trend names
    for t in trends:
        if not t.id: 
            continue
        # Truncate button text slightly if it's too long, but callback_data gets UUID
        btn_text = t.name[:40] + "..." if len(t.name) > 40 else t.name
        keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"view_{t.id}")])
        
    keyboard.append([InlineKeyboardButton("🔄 Regenerate Trends", callback_data="refresh_trending")])
    return InlineKeyboardMarkup(keyboard)

def build_trend_detail_keyboard(trend_id: str) -> InlineKeyboardMarkup:
    """Creates the action buttons when viewing a specific trend."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕵️ Scout This Topic", callback_data=f"scout_{trend_id}")],
        [InlineKeyboardButton("🔙 Back to Trends List", callback_data="back_to_list")]
    ])

# --- Core Logic Functions ---

def format_trends_list_message() -> str:
    """Formats the top-level list view message."""
    if not _config_mgr:
        return "System not initialized."
    
    # We grab the current target settings from the config manager safely
    category = getattr(_config_mgr, 'active_category', 'Unknown')
    return (
        f"🔥 <b>Live Trends Discovered</b>\n\n"
        f"<b>Category:</b> {html.escape(category).title()}\n\n"
        f"👇 Select a trend below to view its full context:"
    )

async def trigger_trend_generation(message: Any) -> None:
    """Executes the trend engine and updates the UI."""
    global _latest_trends
    if not _config_mgr or not _trend_engine:
        await message.reply_text("System not fully initialized.")
        return

    status_msg = await message.reply_text("🔥 <b>Scanning the web for live tech trends...</b>", parse_mode="HTML")

    try:
        await _config_mgr.initialize()
        num_trends, category, subcat, topics, urls = _config_mgr.get_trends()
        
        trends: List[Trend] = await _trend_engine.fetch_and_generate_trends(
            num_trends=num_trends,
            category=category,
            subcategory=subcat,
            topics=topics,
            urls=urls
        )

        if trends:
            # Update global memory cache mapping UUIDs to Trend objects
            _latest_trends = {str(t.id): t for t in trends if t.id}
            
            keyboard = build_trends_list_keyboard(trends)
            text = format_trends_list_message()
            
            await status_msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await status_msg.edit_text("⚠️ <b>Scan Complete</b>, but no significant trends were extracted.", parse_mode="HTML")

    except Exception as e:
        logger.error(f"Trend generation failed: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ <b>Error generating trends</b>: {html.escape(str(e))}", parse_mode="HTML")


# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    safe_user_name = html.escape(update.effective_user.first_name)
    welcome_text = (
        f"👋 <b>Hello, {safe_user_name}!</b> \n\n"
        "I am your AI Trend Analyzer.\n\n"
        "Use <code>/trending</code> to scan the web and extract the latest emerging tech trends."
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
        
    text = (
        "🤖 <b>Bot Commands:</b>\n"
        "<code>/start</code> - Main menu\n"
        "<code>/trending</code> - Scan web for live trends"
    )
    await update.message.reply_text(text, parse_mode="HTML")

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

    # --- Routing the UI clicks ---
    try:
        if data.startswith("view_"):
            # DETAIL VIEW
            trend_id = data.replace("view_", "")
            trend = _latest_trends.get(trend_id)
            
            if not trend:
                await query.answer("⚠️ Trend expired from memory. Please regenerate.", show_alert=True)
                return
                
            await query.answer() # Ack the click
            
            detail_text = (
                f"📊 <b>Trend Details</b>\n\n"
                f"<b>Name:</b> {html.escape(trend.name)}\n\n"
                f"<b>Context:</b>\n<i>{html.escape(trend.context)}</i>"
            )
            keyboard = build_trend_detail_keyboard(trend_id)
            await query.edit_message_text(text=detail_text, reply_markup=keyboard, parse_mode="HTML")

        elif data == "back_to_list":
            # RETURN TO LIST VIEW
            if not _latest_trends:
                await query.answer("⚠️ Session expired. Please regenerate.", show_alert=True)
                return
                
            await query.answer()
            trends_list = list(_latest_trends.values())
            keyboard = build_trends_list_keyboard(trends_list)
            text = format_trends_list_message()
            await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="HTML")

        elif data.startswith("scout_"):
            # INITIATE SCOUTING
            trend_id = data.replace("scout_", "")
            trend = _latest_trends.get(trend_id)
            
            if not trend:
                await query.answer("⚠️ Trend expired from memory. Please regenerate.", show_alert=True)
                return
                
            await query.answer()
            
            scouting_text = (
                f"🕵️ <b>Scouting Initiated!</b>\n\n"
                f"<b>Target:</b> {html.escape(trend.name)}\n"
                f"<b>Trend ID:</b> <code>{trend_id}</code>\n\n"
                f"<i>(Pipeline paused here: Ready for Phase 3 ScoutEngine...)</i>"
            )
            # Remove buttons so the user can't click "Scout" twice
            await query.edit_message_text(text=scouting_text, parse_mode="HTML")

        elif data == "refresh_trending":
            # REGENERATE
            await query.answer()
            await trigger_trend_generation(update.effective_message)
            
    except Exception as e:
        logger.error(f"Button handler error: {e}", exc_info=True)
        await query.answer("❌ An error occurred processing your request.", show_alert=True)


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