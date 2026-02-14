import os
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio
from contextlib import asynccontextmanager


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ptb_application = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 System Online. Ready to scout news!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Usage: /start_week [topic]")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb_application
    if TOKEN:
        
        ptb_application = Application.builder().token(TOKEN).build()
        
        ptb_application.add_handler(CommandHandler("start", start))
        ptb_application.add_handler(CommandHandler("help", help_command))
        
        await ptb_application.initialize()
        await ptb_application.start()
        print("✅ Bot successfully initialized")
    yield
    
    if ptb_application:
        await ptb_application.stop()
        await ptb_application.shutdown()


app = FastAPI(lifespan=lifespan)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Content Engine is running"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        
        update = Update.de_json(data, ptb_application.bot)
        
        await ptb_application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        print(f"Error processing update: {e}")
        return {"status": "error", "message": str(e)}