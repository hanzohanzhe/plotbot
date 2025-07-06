import os
import uuid
import logging
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext

# --- 基础设置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 从环境变量加载配置 ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL")

if not BOT_TOKEN:
    raise ValueError("错误: 必须设置 BOT_TOKEN 环境变量")

# --- 内存中的任务存储 ---
JOBS = {}

# --- Pydantic 模型 ---
class TaskUpdateRequest(BaseModel):
    job_id: str
    status: str
    result_url: str | None = None

# --- FastAPI 应用实例 ---
# 【关键修复】我们将 bot 应用的初始化移到 startup 事件中
app = FastAPI()
telegram_app: Application | None = None

# --- 辅助函数和命令处理器 ---
def send_telegram_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        httpx.post(url, json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}, timeout=30).raise_for_status()
    except Exception as e:
        logger.error(f"发送消息时出错: {e}")

async def start_command(update: Update, context: CallbackContext):
    welcome_text = (
        "你好! 欢迎使用 AI 绘图机器人。\n\n"
        "使用 `/vtuber <描述>` 来提交一个画图任务。\n"
        "例如: `/vtuber 一个穿着赛博朋克夹克的银发女孩`\n\n"
        "任务提交后将进入队列，请耐心等待处理。"
    )
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: CallbackContext):
    help_text = (
        "可用命令:\n"
        "/start - 显示欢迎信息\n"
        "/help - 显示此帮助信息\n"
        "/vtuber <描述> - 根据您的文字描述创建一个VTuber模型"
    )
    await update.message.reply_text(help_text)

async def vtuber_command(update: Update, context: CallbackContext):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("请输入您的描述。例如: `/vtuber 一个戴着猫耳帽子的女孩`")
        return

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "prompt": prompt,
        "chat_id": update.effective_chat.id,
        "status": "PENDING"
    }
    send_telegram_message(
        update.effective_chat.id,
        f"✅ 任务已成功提交，正在排队等待计算节点处理...\n\n任务ID: `{job_id}`"
    )
    logger.info(f"任务已提交: {job_id} for chat_id {update.effective_chat.id}")

# --- FastAPI 端点 ---
@app.post(f"/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """这个端点接收来自Telegram的更新"""
    if telegram_app:
        await telegram_app.update_queue.put(
            Update.de_json(data=await request.json(), bot=telegram_app.bot)
        )
    return Response(status_code=200)

@app.get("/api/get-task")
async def get_task():
    for job_id, task_details in JOBS.items():
        if task_details["status"] == "PENDING":
            task_details["status"] = "RUNNING"
            logger.info(f"任务已分配给 Worker: {job_id}")
            return {"job_id": job_id, "prompt": task_details["prompt"]}
    return {"job_id": None, "prompt": None}

@app.post("/api/update-task")
async def update_task(update: TaskUpdateRequest):
    job = JOBS.get(update.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="未找到任务")
    
    job["status"] = update.status
    logger.info(f"任务状态更新: {update.job_id} -> {update.status}")

    if update.status == "COMPLETED" and update.result_url:
        send_telegram_message(job["chat_id"], f"🎉 您的任务 `{update.job_id}` 已完成！\n\n请点击以下链接下载您的模型：\n{update.result_url}")
    elif update.status == "FAILED":
        send_telegram_message(job["chat_id"], f"很抱歉，您的任务 `{update.job_id}` 执行失败了。")
    
    return {"message": "任务状态已更新"}

@app.get("/")
def health_check():
    return {"status": "ok", "service": "Telebot Dispatch Center"}

# --- 【关键修复】重构生命周期事件 ---
@app.on_event("startup")
async def startup_event():
    """应用启动时运行"""
    global telegram_app
    
    # 1. 创建应用实例
    logger.info("正在创建 Telegram Application 实例...")
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    # 2. 注册命令处理器
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("vtuber", vtuber_command))
    logger.info("命令处理器已注册。")

    # 3. 初始化应用
    logger.info("正在初始化 Telegram Application...")
    await telegram_app.initialize()
    logger.info("Telegram Application 初始化完成。")

    # 4. 设置 Webhook
    if not PUBLIC_SERVER_URL:
        logger.warning("警告: PUBLIC_SERVER_URL 环境变量未设置，无法自动设置Webhook。")
    else:
        webhook_url = f"{PUBLIC_SERVER_URL}/{BOT_TOKEN}"
        logger.info(f"正在设置 Webhook 到: {webhook_url}")
        try:
            await telegram_app.bot.set_webhook(url=webhook_url)
            logger.info("Webhook 设置成功！")
        except Exception as e:
            logger.error(f"设置 Webhook 时发生致命错误: {e}", exc_info=True)
            # 即使设置失败，也继续运行，以便我们可以通过 API 调试
    
    # 5. 启动后台任务队列处理
    logger.info("正在启动后台更新处理...")
    await telegram_app.start()
    logger.info("后台更新处理已启动。应用完全准备就绪！")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时运行"""
    if telegram_app:
        logger.info("正在停止并关闭 Telegram Application...")
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Telegram Application 已关闭。")

