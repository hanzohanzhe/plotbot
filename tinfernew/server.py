import os
import uuid
import logging
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# --- Basic Settings ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Load configuration from environment variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Error: BOT_TOKEN environment variable must be set.")

PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL") # Used for setting webhook

# --- In-memory task storage ---
JOBS = {}

# --- Pydantic model definition for API data structure ---
class TaskUpdateRequest(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None

# --- FastAPI application instance ---
app = FastAPI()

# --- Telegram Bot Setup ---
# Create application instance using Application.builder()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --- Multilingual Messages ---
# Define messages for different languages
MESSAGES = {
    "en": {
        "welcome": (
            "Hello! Welcome to the AI Drawing Bot.\n\n"
            "Use `/vtuber <description>` to submit a drawing task.\n"
            "For example: `/vtuber a silver-haired girl in blue jeans`\n\n"
            "Tasks will be queued, please wait patiently for processing."
        ),
        "help": (
            "Available commands:\n"
            "/start - Show welcome message\n"
            "/help - Show this help information\n"
            "/vtuber <description> - Create a VTuber model based on your text description"
        ),
        "prompt_missing": "Please enter your description. For example: `/vtuber a girl with cat ear headphones`",
        "task_submitted": "✅ Task successfully submitted, queuing for processing...\n\nTask ID: `{job_id}`",
        "task_completed": "🎉 Your task `{job_id}` is complete!\n\nClick the link below to download your model:\n{result_url}",
        "task_failed": "Sorry, your task `{job_id}` failed to execute."
    },
    "zh": {
        "welcome": (
            "你好! 欢迎使用 AI 绘图机器人。\n\n"
            "使用 `/vtuber <描述>` 来提交一个画图任务。\n"
            "例如: `/vtuber 一个穿着蓝色牛仔裤的银发女孩`\n\n"
            "任务提交后将进入队列，请耐心等待处理。"
        ),
        "help": (
            "可用命令:\n"
            "/start - 显示欢迎信息\n"
            "/help - 显示此帮助信息\n"
            "/vtuber <描述> - 根据您的文字描述创建一个VTuber模型"
        ),
        "prompt_missing": "请输入您的描述。例如: `/vtuber 一个有着金发双马尾的女孩`",
        "task_submitted": "✅ 任务已成功提交，正在排队等待计算节点处理...\n\n任务ID: `{job_id}`",
        "task_completed": "🎉 您的任务 `{job_id}` 已完成！\n\n请点击以下链接下载您的模型：\n{result_url}",
        "task_failed": "很抱歉，您的任务 `{job_id}` 执行失败了。"
    }
}

def get_message(lang_code: str | None, key: str) -> str:
    """
    Retrieves the appropriate message based on language code and message key.
    Defaults to English if the language is not found or key is missing.
    """
    # Simple mapping for language codes (e.g., 'zh-hans' -> 'zh')
    lang = 'en' # Default language
    if lang_code and lang_code.startswith('zh'):
        lang = 'zh'
    elif lang_code and lang_code.startswith('en'):
        lang = 'en'
    
    # Return message for the determined language, fallback to English if key is missing
    return MESSAGES.get(lang, MESSAGES['en']).get(key, MESSAGES['en'][key])

# --- Helper function ---
def send_telegram_message(chat_id: int, text: str):
    """A helper function to send messages to a specified Telegram user."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(url, json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}, timeout=30)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"Error sending message to chat_id {chat_id}: {e.response.text}")
    except Exception as e:
        logger.error(f"Unknown error occurred while sending message: {e}")

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: CallbackContext):
    """Handles the /start command."""
    lang_code = update.effective_user.language_code if update.effective_user else None
    welcome_text = get_message(lang_code, "welcome")
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: CallbackContext):
    """Handles the /help command."""
    lang_code = update.effective_user.language_code if update.effective_user else None
    help_text = get_message(lang_code, "help")
    await update.message.reply_text(help_text)

async def vtuber_command(update: Update, context: CallbackContext):
    """Handles the /vtuber command, creates a new task."""
    lang_code = update.effective_user.language_code if update.effective_user else None
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text(get_message(lang_code, "prompt_missing"))
        return

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "prompt": prompt,
        "chat_id": update.effective_chat.id,
        "status": "PENDING",
        "language": lang_code # Store the user's language code
    }
    send_telegram_message(
        update.effective_chat.id,
        get_message(lang_code, "task_submitted").format(job_id=job_id)
    )
    logger.info(f"Task submitted: {job_id} for chat_id {update.effective_chat.id} with language {lang_code}")

# --- Register command handlers to Telegram application ---
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("vtuber", vtuber_command))

# --- FastAPI Webhook Endpoint ---
@app.post(f"/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """This endpoint receives updates from Telegram."""
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)

# --- API Endpoints for Worker ---
@app.get("/api/get-task")
async def get_task():
    """Called by local Worker to get a pending task."""
    for job_id, task_details in JOBS.items():
        if task_details["status"] == "PENDING":
            task_details["status"] = "RUNNING"
            logger.info(f"Task assigned to Worker: {job_id}")
            return {"job_id": job_id, "prompt": task_details["prompt"]}
    return {"job_id": None, "prompt": None}

@app.post("/api/update-task")
async def update_task(update: TaskUpdateRequest):
    """Called by local Worker to update task status."""
    job = JOBS.get(update.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    
    job["status"] = update.status
    logger.info(f"Task status updated: {update.job_id} -> {update.status}")

    # Retrieve user's language from the stored job details
    user_lang_code = job.get("language", 'en') # Default to English if language not found
    
    if update.status == "COMPLETED" and update.result_url:
        send_telegram_message(job["chat_id"], get_message(user_lang_code, "task_completed").format(job_id=update.job_id, result_url=update.result_url))
    elif update.status == "FAILED":
        send_telegram_message(job["chat_id"], get_message(user_lang_code, "task_failed").format(job_id=update.job_id))
    
    return {"message": "Task status updated"}

@app.get("/")
def health_check():
    """Root path for health check."""
    return {"status": "ok", "service": "Telebot Dispatch Center"}

# --- Lifecycle events to run on application startup and shutdown ---
@app.on_event("startup")
async def startup_event():
    """Runs on application startup."""
    # [Crucial fix] Initialize the application before setting the webhook
    await telegram_app.initialize()

    if not PUBLIC_SERVER_URL:
        logger.warning("Warning: PUBLIC_SERVER_URL environment variable is not set, cannot set Webhook automatically.")
        return
    
    webhook_url = f"{PUBLIC_SERVER_URL}/{BOT_TOKEN}"
    logger.info(f"Setting Webhook to: {webhook_url}")
    await telegram_app.bot.set_webhook(url=webhook_url)

@app.on_event("shutdown")
async def shutdown_event():
    """Runs on application shutdown."""
    logger.info("Removing Webhook and shutting down application...")
    # [Crucial fix] Need to call shutdown on close as well
    await telegram_app.shutdown()
