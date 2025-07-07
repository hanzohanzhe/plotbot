import os
import uuid
import logging
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# --- Basic Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Load configuration from environment variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN environment variable must be set")

PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL") # Used to set the webhook

# --- In-memory job storage ---
JOBS = {}

# --- Pydantic model defines the API's data structure ---
class TaskUpdateRequest(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None # Kept for compatibility, can be null

# --- FastAPI application instance ---
app = FastAPI()

# --- Telegram Bot Setup ---
# Create the Application instance using Application.builder()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --- Helper Function ---
def send_telegram_message(chat_id: int, text: str):
    """A helper function to send a message to a specified Telegram user."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(url, json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}, timeout=30)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"Error sending message to chat_id {chat_id}: {e.response.text}")
    except Exception as e:
        logger.error(f"An unknown error occurred while sending message: {e}")

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: CallbackContext):
    """Handles the /start command"""
    welcome_text = (
        "Hello! Welcome to the AI Drawing Bot.\n\n"
        "Use `/vtuber <description>` to submit a drawing task.\n"
        "For example: `/vtuber a silver-haired girl in a cyberpunk jacket`\n\n"
        "Your task will be added to the queue, please wait patiently for it to be processed."
    )
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: CallbackContext):
    """Handles the /help command"""
    help_text = (
        "Available commands:\n"
        "/start - Show welcome message\n"
        "/help - Show this help message\n"
        "/vtuber <description> - Create a VTuber model based on your text description"
    )
    await update.message.reply_text(help_text)

async def vtuber_command(update: Update, context: CallbackContext):
    """Handles the /vtuber command to create a new task"""
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Please enter your description. For example: `/vtuber a girl wearing a cat-ear hat`")
        return

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "prompt": prompt,
        "chat_id": update.effective_chat.id, # Store chat_id
        "status": "PENDING"
    }
    send_telegram_message(
        update.effective_chat.id,
        f"✅ Task submitted successfully, now waiting in the queue for a compute node...\n\nTask ID: `{job_id}`"
    )
    logger.info(f"Task submitted: {job_id} for chat_id {update.effective_chat.id}")

# --- Register command handlers with the Telegram application ---
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("vtuber", vtuber_command))

# --- FastAPI Webhook Endpoint ---
@app.post(f"/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """This endpoint receives updates from Telegram"""
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)

# --- API Endpoint for Workers ---
@app.get("/api/get-task")
async def get_task():
    """Called by the local Worker to get a pending task"""
    for job_id, task_details in JOBS.items():
        if task_details["status"] == "PENDING":
            task_details["status"] = "RUNNING"
            logger.info(f"Task assigned to Worker: {job_id}")
            # Include chat_id when returning the task
            return {
                "job_id": job_id,
                "prompt": task_details["prompt"],
                "chat_id": task_details["chat_id"] # <-- New: return chat_id
            }
    return {"job_id": None, "prompt": None, "chat_id": None} # <-- New: also return chat_id when no task

@app.post("/api/update-task")
async def update_task(update: TaskUpdateRequest):
    """Called by the local Worker to update a task's status"""
    job = JOBS.get(update.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    
    job["status"] = update.status
    logger.info(f"Task status updated: {update.job_id} -> {update.status}")

    if update.status == "COMPLETED":
        # If the worker successfully sent the file, we can send a confirmation message.
        send_telegram_message(job["chat_id"], f"🎉 Your task `{update.job_id}` is complete! The file has been sent to you directly by the bot.")
    elif update.status == "FAILED":
        send_telegram_message(job["chat_id"], f"Sorry, your task `{update.job_id}` has failed.")
    
    return {"message": "Task status updated"}

@app.get("/")
def health_check():
    """Root path for health checks"""
    return {"status": "ok", "service": "Telebot Dispatch Center"}

# --- Lifecycle events to run on application startup and shutdown ---
@app.on_event("startup")
async def startup_event():
    """Runs on application startup"""
    await telegram_app.initialize()

    if not PUBLIC_SERVER_URL:
        logger.warning("Warning: PUBLIC_SERVER_URL environment variable is not set. Cannot set webhook automatically.")
        return
    
    webhook_url = f"{PUBLIC_SERVER_URL}/{BOT_TOKEN}"
    logger.info(f"Setting Webhook to: {webhook_url}")
    await telegram_app.bot.set_webhook(url=webhook_url)

@app.on_event("shutdown")
async def shutdown_event():
    """Runs on application shutdown"""
    logger.info("Removing Webhook and shutting down application...")
    await telegram_app.shutdown()
