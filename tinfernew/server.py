回退至无支付功能版本的指南

本指南将指导您如何将服务器上的应用，安全地恢复到最初的、不包含任何支付功能的稳定版本。
第一步：在 GitHub 上更新 server.py 文件

这是最关键的一步。我们需要将您仓库中的 server.py 文件内容，替换为最初的、最简单的版本。

    请在浏览器中打开您 GitHub 仓库里的 plotbot/tinfernew/server.py 文件，并点击“编辑”按钮。

    删除里面的所有旧代码。

    将以下这份原始版本的代码完整地粘贴进去：

    import os
    import uuid
    import logging
    import httpx
    from fastapi import FastAPI, Request, Response, HTTPException
    from pydantic import BaseModel
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackContext

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
            "For example: `/vtuber a silver-haired girl in a white shirt`\n\n"
            "Your task will be added to the queue, please wait patiently for it to be processed."
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
        """Handles the /vtuber command to create a new task"""
        prompt = " ".join(context.args)
        if not prompt:
            await update.message.reply_text("Please enter your description. For example: `/vtuber a girl wearing a cat-ear hat`")
            return

        job_id = str(uuid.uuid4())
        JOBS[job_id] = {
            "prompt": prompt,
            "chat_id": update.effective_chat.id,
            "status": "PENDING"
        }
        send_telegram_message(
            update.effective_chat.id,
            f"✅ Task submitted successfully, now waiting in the queue for a compute node...\n\nTask ID: `{job_id}`"
        )
        logger.info(f"Task submitted: {job_id} for chat_id {update.effective_chat.id}")

    async def dmiu_command(update: Update, context: CallbackContext):
        """Handles the /dmiu command to contact the owner."""
        my_telegram_username = "hanzohang"
        my_telegram_url = f"https://t.me/{my_telegram_username}"

        text = "Hello! Click the button below to start a direct chat with me (the bot administrator)."

        keyboard = [
            [InlineKeyboardButton("💬 Start Chat", url=my_telegram_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(text, reply_markup=reply_markup)

    # --- Register command handlers with the Telegram application ---
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
            send_telegram_message(job["chat_id"], f"🎉 Your task `{update_request.job_id}` is complete! The file has been sent to you directly by the bot.")
        elif update_request.status == "FAILED":
            send_telegram_message(job["chat_id"], f"Sorry, your task `{update_request.job_id}` has failed.")

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

    提交 (Commit) 您在 GitHub 上的更改。

第二步：简化服务器上的 .env 文件

原始版本的代码不需要任何支付相关的配置。我们需要清理一下 .env 文件。

    SSH 连接到您的服务器，并进入项目目录：

    cd ~/plotbot/tinfernew/

    编辑 .env 文件：

    nano .env

    删除里面所有的内容，只保留以下两行：

    # Telegram Bot Configuration
    BOT_TOKEN=... (请填入您的真实 Token)

    # Public URL Configuration
    PUBLIC_SERVER_URL=https://paymentbot.tinfer.ai

    保存并退出 (Ctrl+X -> Y -> Enter)。

第三步：更新并重启服务

现在，我们让服务器上的代码与您刚刚更新的 GitHub 仓库同步，并用这个最简单的版本重启服务。

    从 GitHub 拉取最新代码:

    git pull

    重新构建并启动服务:

    sudo docker-compose up -d --build

完成以上步骤后，您的机器人应该已经恢复到了最初的、可以正常提交任务的状态。请您在 Telegram 中测试 /vtuber 命令，它现在应该会直接提示您任务已提交，不再有任何支付流程。

对于之前给您带来的所有麻烦，我再次表示深深的歉意。希望这次的回退操作能让您的服务先稳定下来。
