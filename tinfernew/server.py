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
GLOBEPAY_PARTNER_CODE = os.environ.get("GLOBEPAY_PARTNER_CODE")
GLOBEPAY_CREDENTIAL = os.environ.get("GLOBEPAY_CREDENTIAL")
PRICE_AMOUNT = os.environ.get("PRICE_AMOUNT", "1")
PRICE_CURRENCY = os.environ.get("PRICE_CURRENCY", "GBP")

if not GLOBEPAY_PARTNER_CODE or not GLOBEPAY_CREDENTIAL:
    raise ValueError("Error: GLOBEPAY_PARTNER_CODE and GLOBEPAY_CREDENTIAL must be set")

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
    """Generates an API request signature according to GlobePay documentation."""
    filtered_params = {k: v for k, v in params.items() if v and k != 'sign'}
    sorted_params = sorted(filtered_params.items())
    unsigned_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    string_to_sign = f"{unsigned_string}&key={credential}"
    
    logger.info(f"String to be signed: {string_to_sign}")
    
    return hashlib.md5(string_to_sign.encode('utf-8')).hexdigest().upper()

def generate_nonce_str() -> str:
    """Generates a 32-character uppercase alphanumeric random string."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=32))

async def create_payment_qr(job_id: str) -> str | None:
    """Calls GlobePay API to create a new payment order and returns the QR code content."""
    globepay_api_url = f"https://pay.globepay.co/api/v1.0/gateway/partners/{GLOBEPAY_PARTNER_CODE}/orders/{job_id}"
    notify_url = f"{PUBLIC_SERVER_URL}/api/payment-notify"
    
    params = {
        "time": str(int(time.time() * 1000)),
        "nonce_str": generate_nonce_str(),
        "price": PRICE_AMOUNT,
        "currency": PRICE_CURRENCY,
        # **DEFINITIVE FIX 2**: Use a hardcoded, simple, ASCII-only description
        # that exactly matches the API documentation example to eliminate all variables.
        "description": "test",
        "notify_url": notify_url,
    }
    
    # **DEFINITIVE FIX 1**: Do NOT include partner_code in the signature calculation,
    # as it is only part of the URL path, not the request body.
    params['sign'] = generate_globepay_signature(params, GLOBEPAY_CREDENTIAL)
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.put(globepay_api_url, data=params, timeout=30)
            
            data = response.json()
            
            logger.info(f"GlobePay API Response for job {job_id}: STATUS={response.status_code}, BODY={data}")

            response.raise_for_status()
            
            if data.get("result_code") == "SUCCESS":
                return data.get("code_url")
            else:
                logger.error(f"GlobePay returned a non-SUCCESS result_code for job {job_id}.")
                return None
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP Error calling GlobePay API for job {job_id}: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Unknown Error calling GlobePay API for job {job_id}: {e}", exc_info=True)
        return None

# --- Telegram Helper Functions ---
async def send_telegram_message(chat_id: int, text: str, reply_markup=None):
    """A helper function to send a message to a specified Telegram user."""
    try:
        await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error sending message to chat_id {chat_id}: {e}")

async def send_qr_code_image(chat_id: int, qr_data: str, caption: str):
    """Generates and sends a QR code image."""
    try:
        img = qrcode.make(qr_data)
        bio = BytesIO()
        bio.name = 'payment_qr.png'
        img.save(bio, 'PNG')
        bio.seek(0)
        await telegram_app.bot.send_photo(chat_id=chat_id, photo=bio, caption=caption)
    except Exception as e:
        logger.error(f"Error sending QR code to chat_id {chat_id}: {e}")

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
    """Handles the /vtuber command to create a new task and initiate payment"""
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Please provide a description. For example: `/vtuber a girl wearing a cat-ear hat`")
        return

    job_id = str(uuid.uuid4()).replace('-', '')
    chat_id = update.effective_chat.id
    
    await update.message.reply_text("Creating your payment order, please wait...")

    qr_code_url = await create_payment_qr(job_id)

    if qr_code_url:
        JOBS[job_id] = {
            "prompt": prompt,
            "chat_id": chat_id,
            "status": "AWAITING_PAYMENT"
        }
        
        payment_caption = (
            f"✅ Your order has been created! Please scan the QR code below to complete the payment.\n\n"
            f"💰 **Amount: {PRICE_AMOUNT} {PRICE_CURRENCY}**\n"
            f"📝 **Your Task:** {prompt}\n"
            f"🆔 **Order ID:** `{job_id}`\n\n"
            f"Once payment is successful, your task will automatically be queued for processing."
        )
        await send_qr_code_image(chat_id, qr_code_url, payment_caption)
        logger.info(f"Payment order created for job {job_id} for chat_id {chat_id}")
    else:
        await send_telegram_message(chat_id, "❌ Sorry, we failed to create a payment order. Please try again later or contact an administrator.")

async def dmiu_command(update: Update, context: CallbackContext):
    """Handles the /dmiu command to contact the owner."""
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
    """This endpoint receives payment success notifications from GlobePay."""
    try:
        data = await request.json()
        logger.info(f"Received GlobePay notification: {data}")

        params_to_validate = data.copy()
        received_sign = params_to_validate.pop('sign', None)
        
        if generate_globepay_signature(params_to_validate, GLOBEPAY_CREDENTIAL) != received_sign:
            logger.warning(f"GlobePay notification signature validation failed: {data}")
            raise HTTPException(status_code=400, detail="Invalid signature")

        order_id = data.get('order_id')
        job = JOBS.get(order_id)

        if not job:
            logger.error(f"Received notification for a non-existent job: {order_id}")
            return {"result": "success"}

        if job["status"] == "AWAITING_PAYMENT":
            job["status"] = "PENDING"
            logger.info(f"Payment successful for job {order_id}. Status updated to PENDING.")
            
            await send_telegram_message(
                job["chat_id"],
                f"🎉 Payment successful!\n\nYour task `{job_id}` is now in the queue to be processed."
            )
        else:
            logger.warning(f"Received duplicate notification for job: {order_id}, current status: {job['status']}")

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
