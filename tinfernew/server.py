import os
import uuid
import logging
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# --- 基础设置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 从环境变量加载配置 ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("错误: 必须设置 BOT_TOKEN 环境变量")

PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL") # 用于设置 webhook

# --- 内存中的任务存储 ---
JOBS = {}

# --- Pydantic 模型定义 API 的数据结构 ---
class TaskUpdateRequest(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None # 尽管不再用于下载，但保留兼容性，可为空

# --- FastAPI 应用实例 ---
app = FastAPI()

# --- Telegram Bot 设置 ---
# 使用 Application.builder() 创建应用实例
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --- START: 新增多语言消息字典 ---
MESSAGES = {
    "en": {
        "welcome": (
            "Hello! Welcome to the AI Drawing Bot.\n\n"
            "Use `/vtuber <description>` to submit a drawing task.\n"
            "For example: `/vtuber a silver-haired girl in a cyberpunk jacket`\n\n"
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
        "task_completed": "🎉 Your task `{job_id}` is complete! The file has been sent to you directly via the bot.",
        "task_failed": "Sorry, your task `{job_id}` failed to execute."
    },
    "zh": {
        "welcome": (
            "你好! 欢迎使用 AI 绘图机器人。\n\n"
            "使用 `/vtuber <描述>` 来提交一个画图任务。\n"
            "例如: `/vtuber 一个穿着赛博朋克夹克的银发女孩`\n\n"
            "任务提交后将进入队列，请耐心等待处理。"
        ),
        "help": (
            "可用命令:\n"
            "/start - 显示欢迎信息\n"
            "/help - 显示此帮助信息\n"
            "/vtuber <描述> - 根据您的文字描述创建一个VTuber模型"
        ),
        "prompt_missing": "请输入您的描述。例如: `/vtuber 一个戴着猫耳帽子的女孩`",
        "task_submitted": "✅ 任务已成功提交，正在排队等待计算节点处理...\n\n任务ID: `{job_id}`",
        "task_completed": "🎉 您的任务 `{job_id}` 已完成！文件已通过机器人直接发送给您。",
        "task_failed": "很抱歉，您的任务 `{job_id}` 执行失败了。"
    }
}

def get_message(lang_code: str | None, key: str) -> str:
    """
    根据语言代码和消息键检索相应的消息。
    如果语言未找到或键缺失，则默认为英文。
    """
    # 规范化语言代码：仅使用主要部分（例如，'en-US' -> 'en'）
    # 如果 lang_code 为 None 或为空，默认为 'en'
    effective_lang = 'en' 
    if lang_code:
        lang_prefix = lang_code.split('-')[0].lower()
        if lang_prefix in MESSAGES:
            effective_lang = lang_prefix
    
    logger.info(f"Resolved language for key '{key}': '{effective_lang}' (Original: '{lang_code}')")
    
    # 返回确定语言的消息，如果键缺失则回退到英文
    return MESSAGES.get(effective_lang, MESSAGES['en']).get(key, MESSAGES['en'][key])
# --- END: 新增多语言消息字典和辅助函数 ---


# --- 辅助函数 ---
def send_telegram_message(chat_id: int, text: str):
    """一个辅助函数，用于向指定的Telegram用户发送消息。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(url, json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}, timeout=30)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"发送消息到 chat_id {chat_id} 时出错: {e.response.text}")
    except Exception as e:
        logger.error(f"发送消息时发生未知错误: {e}")

# --- Telegram 命令处理器 ---
async def start_command(update: Update, context: CallbackContext):
    """处理 /start 命令"""
    # --- MODIFIED: 获取用户语言并使用多语言消息 ---
    lang_code = update.effective_user.language_code if update.effective_user else None
    logger.info(f"Start command received. User ID: {update.effective_user.id}, Language Code: {lang_code}")
    welcome_text = get_message(lang_code, "welcome")
    await update.message.reply_text(welcome_text)
    # --- END MODIFIED ---

async def help_command(update: Update, context: CallbackContext):
    """处理 /help 命令"""
    # --- MODIFIED: 获取用户语言并使用多语言消息 ---
    lang_code = update.effective_user.language_code if update.effective_user else None
    logger.info(f"Help command received. User ID: {update.effective_user.id}, Language Code: {lang_code}")
    help_text = get_message(lang_code, "help")
    await update.message.reply_text(help_text)
    # --- END MODIFIED ---

async def vtuber_command(update: Update, context: CallbackContext):
    """处理 /vtuber 命令，创建新任务"""
    # --- MODIFIED: 获取用户语言并使用多语言消息，存储语言 ---
    lang_code = update.effective_user.language_code if update.effective_user else None
    logger.info(f"Vtuber command received. User ID: {update.effective_user.id}, Language Code: {lang_code}")

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text(get_message(lang_code, "prompt_missing"))
        return

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "prompt": prompt,
        "chat_id": update.effective_chat.id, # 存储 chat_id
        "status": "PENDING",
        "language": lang_code # 新增：存储用户的语言代码
    }
    send_telegram_message(
        update.effective_chat.id,
        get_message(lang_code, "task_submitted").format(job_id=job_id)
    )
    logger.info(f"任务已提交: {job_id} for chat_id {update.effective_chat.id} with language {lang_code}. Current JOBS: {JOBS}")
    # --- END MODIFIED ---

# --- 将命令处理器注册到 Telegram 应用 ---
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("vtuber", vtuber_command))

# --- FastAPI Webhook 端点 ---
@app.post(f"/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """这个端点接收来自Telegram的更新"""
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)

# --- 为 Worker 提供的 API 端点 ---
@app.get("/api/get-task")
async def get_task():
    """由本地Worker调用，获取一个待处理的任务"""
    for job_id, task_details in JOBS.items():
        if task_details["status"] == "PENDING":
            task_details["status"] = "RUNNING"
            logger.info(f"任务已分配给 Worker: {job_id}")
            # 返回任务时，包含 chat_id
            return {
                "job_id": job_id,
                "prompt": task_details["prompt"],
                "chat_id": task_details["chat_id"] # <-- 确保返回 chat_id
            }
    return {"job_id": None, "prompt": None, "chat_id": None} # <-- 没有任务时也返回 chat_id

@app.post("/api/update-task")
async def update_task(update: TaskUpdateRequest):
    """由本地Worker调用，更新任务状态"""
    job = JOBS.get(update.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未找到任务")
    
    job["status"] = update.status
    logger.info(f"任务状态更新: {update.job_id} -> {update.status}")

    # --- MODIFIED: 根据存储的用户语言发送任务更新消息 ---
    # 从存储的任务详情中检索用户语言
    user_lang_code = job.get("language", 'en') # 如果未找到语言，默认为英文
    logger.info(f"Updating task {update.job_id}. User's stored language: {user_lang_code}")

    if update.status == "COMPLETED":
        send_telegram_message(job["chat_id"], get_message(user_lang_code, "task_completed").format(job_id=update.job_id))
    elif update.status == "FAILED":
        send_telegram_message(job["chat_id"], get_message(user_lang_code, "task_failed").format(job_id=update.job_id))
    # --- END MODIFIED ---
    
    return {"message": "任务状态已更新"}

@app.get("/")
def health_check():
    """根路径，用于健康检查"""
    return {"status": "ok", "service": "Telebot Dispatch Center"}

# --- 在应用启动和关闭时运行的生命周期事件 ---
@app.on_event("startup")
async def startup_event():
    """应用启动时运行"""
    await telegram_app.initialize()

    if not PUBLIC_SERVER_URL:
        logger.warning("警告: PUBLIC_SERVER_URL 环境变量未设置，无法自动设置Webhook。")
        return
    
    webhook_url = f"{PUBLIC_SERVER_URL}/{BOT_TOKEN}"
    logger.info(f"正在设置 Webhook 到: {webhook_url}")
    await telegram_app.bot.set_webhook(url=webhook_url)

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时运行"""
    logger.info("正在移除 Webhook 并关闭应用...")
    await telegram_app.shutdown()
