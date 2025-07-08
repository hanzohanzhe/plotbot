import os
import uuid
import logging
import httpx
import time
import hashlib
import random
import string
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext
from urllib.parse import urlencode
import qrcode
from io import BytesIO

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

PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL")
if not PUBLIC_SERVER_URL:
    raise ValueError("Error: PUBLIC_SERVER_URL environment variable must be set")

# --- GlobePay Configuration ---
GLOBEPAY_CREDENTIAL = os.environ.get("GLOBEPAY_CREDENTIAL")
# **NEW CONFIGURATION for Static QR Code Workflow**
# The URL where you have hosted the static QR code image.
STATIC_QR_CODE_URL = os.environ.get("STATIC_QR_CODE_URL") 
# The price for display purposes (e.g., "0.99")
PRICE_DISPLAY = os.environ.get("PRICE_DISPLAY", "0.99") 
# The price in the smallest currency unit (e.g., 99 for 0.99 RMB) for validation
PRICE_IN_CENTS = os.environ.get("PRICE_IN_CENTS", "99") 
PRICE_CURRENCY = os.environ.get("PRICE_CURRENCY", "CNY")

if not GLOBEPAY_CREDENTIAL or not STATIC_QR_CODE_URL:
    raise ValueError("Error: GLOBEPAY_CREDENTIAL and STATIC_QR_CODE_URL must be set")

# --- In-memory job storage ---
JOBS = {}

# --- Pydantic model defines the API's data structure ---
class TaskUpdateRequest(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None

# --- FastAPI application instance ---
app = FastAPI()

# --- Telegram Bot Setup ---
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --- GlobePay Helper Functions ---
def generate_globepay_signature(params: dict, credential: str) -> str:
    """Generates a signature for validating incoming notifications."""
    filtered_params = {k: v for k, v in params.items() if v and k != 'sign'}
    sorted_params = sorted(filtered_params.items())
    unsigned_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    string_to_sign = f"{unsigned_string}&key={credential}"
    logger.info(f"Validating signature with string: {string_to_sign}")
    return hashlib.md5(string_to_sign.encode('utf-8')).hexdigest().upper()

# --- Telegram Helper Functions ---
async def send_telegram_message(chat_id: int, text: str, reply_markup=None):
    """A helper function to send a message to a specified Telegram user."""
    try:
        await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error sending message to chat_id {chat_id}: {e}")

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: CallbackContext):
    """Handles the /start command"""
    welcome_text = (
        "Hello! Welcome to the AI Drawing Bot.\n\n"
        "Use `/vtuber <description>` to submit a drawing task.\n"
        "For example: `/vtuber a silver-haired girl in a white shirt`\n\n"
        "After submitting, you will be prompted for payment to start the task."
    )
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: CallbackContext):
    """Handles the /help command"""
    help_text = (
        "Available commands:\n"
        "/start - Show welcome message\n"
        "/help - Show this help message\n"
        "/vtuber <description> - Create a VTuber model based on your text description\n"
        "/dmiu - Contact the bot administrator"
    )
    await update.message.reply_text(help_text)

async def vtuber_command(update: Update, context: CallbackContext):
    """Handles the /vtuber command, now sends a static QR code."""
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Please provide a description. For example: `/vtuber a girl wearing a cat-ear hat`")
        return

    job_id = str(uuid.uuid4()).replace('-', '')[:16] # A shorter, more user-friendly ID
    chat_id = update.effective_chat.id
    
    # Store the job, waiting for payment confirmation
    JOBS[job_id] = {
        "prompt": prompt,
        "chat_id": chat_id,
        "status": "AWAITING_PAYMENT"
    }
    logger.info(f"Job {job_id} created for chat_id {chat_id}, awaiting payment.")

    # Send the static QR code and payment instructions
    payment_caption = (
        f"✅ Your task has been created!\n\n"
        f"To proceed, please use Alipay to scan the QR code below.\n\n"
        f"**IMPORTANT INSTRUCTIONS:**\n"
        f"1. You **MUST** pay the exact amount of **{PRICE_DISPLAY} {PRICE_CURRENCY}**.\n"
        f"2. You **MUST** enter the following Task ID into the payment **remarks/notes** field:\n\n"
        f"`{job_id}`\n\n"
        f"(You can tap to copy the ID)\n\n"
        f"Failure to follow these instructions will result in payment failure and your task will not be processed."
    )
    
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=STATIC_QR_CODE_URL,
            caption=payment_caption,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send static QR code photo: {e}")
        await send_telegram_message(chat_id, "Error: Could not display the payment QR code. Please contact an administrator.")

# This is the function for the /dmiu command
async def dmiu_command(update: Update, context: CallbackContext):
    """Handles the /dmiu command to contact the owner."""
    # IMPORTANT: Replace "hanzohang" with your actual Telegram username
    my_telegram_username = "hanzohang"
    my_telegram_url = f"https://t.me/{my_telegram_username}"
    text = "Hello! Click the button below to start a direct chat with me (the bot administrator)."
    keyboard = [[InlineKeyboardButton("💬 Start Chat", url=my_telegram_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)

# --- Register command handlers ---
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("vtuber", vtuber_command))
# Make sure the /dmiu command handler is registered
telegram_app.add_handler(CommandHandler("dmiu", dmiu_command))

# --- FastAPI Webhook Endpoint ---
@app.post(f"/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """This endpoint receives updates from Telegram"""
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)

# --- API Endpoint for GlobePay Payment Notifications ---
@app.post("/api/payment-notify")
async def payment_notify(request: Request):
    """
    This endpoint receives ALL payment success notifications from GlobePay
    and processes them based on the amount and description.
    """
    try:
        data = await request.json()
        logger.info(f"Received GlobePay notification: {data}")

        # --- 1. Validate Signature ---
        params_to_validate = data.copy()
        received_sign = params_to_validate.pop('sign', None)
        if generate_globepay_signature(params_to_validate, GLOBEPAY_CREDENTIAL) != received_sign:
            logger.warning(f"GlobePay notification signature validation failed: {data}")
            raise HTTPException(status_code=400, detail="Invalid signature")

        # --- 2. Validate Payment Amount ---
        paid_amount = str(data.get("total_fee", "0"))
        if paid_amount != PRICE_IN_CENTS:
            logger.warning(f"Received payment with incorrect amount. Expected {PRICE_IN_CENTS}, got {paid_amount}. Ignoring.")
            return {"result": "success"} # Tell GlobePay we're done

        # --- 3. Extract Job ID from Remarks ---
        # We assume the user correctly entered the job_id in the description field.
        job_id = data.get("description")
        if not job_id:
            logger.warning(f"Received valid payment but description (remark) is empty. Cannot process. Payload: {data}")
            return {"result": "success"}

        # --- 4. Find and Update Job ---
        job = JOBS.get(job_id)
        if not job:
            logger.error(f"Received payment for a non-existent or already processed job: {job_id}")
            return {"result": "success"}

        if job["status"] == "AWAITING_PAYMENT":
            job["status"] = "PENDING"
            logger.info(f"Payment successful for job {job_id}. Status updated to PENDING.")
            await send_telegram_message(
                job["chat_id"],
                f"🎉 Payment of {PRICE_DISPLAY} {PRICE_CURRENCY} for task `{job_id}` confirmed!\n\nYour task is now in the queue to be processed."
            )
        else:
            logger.warning(f"Received duplicate payment notification for job: {job_id}, current status: {job['status']}")

        return {"result": "success"}
    except Exception as e:
        logger.error(f"Error processing GlobePay notification: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

# --- API Endpoint for Workers ---
@app.get("/api/get-task")
async def get_task():
    """Called by the local Worker to get a pending task"""
    for job_id, task_details in JOBS.items():
        if task_details["status"] == "PENDING":
            task_details["status"] = "RUNNING"
            logger.info(f"Task assigned to Worker: {job_id}")
            return {
                "job_id": job_id,
                "prompt": task_details["prompt"],
                "chat_id": task_details["chat_id"]
            }
    return {"job_id": None, "prompt": None, "chat_id": None}

@app.post("/api/update-task")
async def update_task(update_request: TaskUpdateRequest):
    """Called by the local Worker to update a task's status"""
    job = JOBS.get(update_request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    
    job["status"] = update_request.status
    logger.info(f"Task status updated: {update_request.job_id} -> {update_request.status}")

    if update_request.status == "COMPLETED":
        await send_telegram_message(job["chat_id"], f"🎉 Your task `{update_request.job_id}` is complete! The file has been sent to you directly by the bot.")
    elif update_request.status == "FAILED":
        await send_telegram_message(job["chat_id"], f"Sorry, your task `{update_request.job_id}` has failed.")
    
    return {"message": "Task status updated"}

@app.get("/")
def health_check():
    """Root path for health checks"""
    return {"status": "ok", "service": "Telebot Dispatch Center with Payment"}

# --- Lifecycle events ---
@app.on_event("startup")
async def startup_event():
    """Runs on application startup"""
    await telegram_app.initialize()
    webhook_url = f"{PUBLIC_SERVER_URL}/{BOT_TOKEN}"
    logger.info(f"Setting Webhook to: {webhook_url}")
    await telegram_app.bot.set_webhook(url=webhook_url)

@app.on_event("shutdown")
async def shutdown_event():
    """Runs on application shutdown"""
    logger.info("Removing Webhook and shutting down application...")
    await telegram_app.shutdown()
