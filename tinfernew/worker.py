import os
import time
import logging
import requests
import subprocess
import shutil
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading
import ngrok
import sys

# ==============================================================================
# --- 1. 基础设置 (Basic Setup) ---
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("LocalTextoonWorker")

# ==============================================================================
# --- 2. 配置 (Configuration) ---
# 在运行此脚本前，请务必检查并修改以下所有路径为您本地的真实路径！
# ==============================================================================
# 您部署在云端的调度中心的公网IP地址
DISPATCH_CENTER_URL = "http://34.87.45.115"

# 您为 Textoon 创建的 Conda 环境中的 Python 解释器路径
# 例如: /home/your_user/miniconda3/envs/textoon/bin/python
TEXTOON_PYTHON_PATH = "/home/deepseek/.conda/envs/textoon"

# 您本地 Textoon 项目的 main.py 脚本的绝对路径
# 例如: /home/your_user/projects/Textoon/main.py
TEXTOON_SCRIPT_PATH = "/home/deepseek/textoon2/main.py"

# ngrok authtoken, 强烈建议从环境变量加载
# 运行前请先设置: export NGROK_AUTHTOKEN='YOUR_TOKEN'
# 或者直接在这里填入: ngrok.set_auth_token("YOUR_TOKEN")
if os.environ.get("NGROK_AUTHTOKEN"):
    ngrok.set_auth_token(os.environ.get("NGROK_AUTHTOKEN"))
else:
    logger.warning("NGROK_AUTHTOKEN 环境变量未设置。pyngrok 可能无法正常工作。")


# ==============================================================================
# --- 3. 核心功能 (Core Functions) ---
# ==============================================================================

def run_textoon_locally(job_id: str, prompt: str) -> str | None:
    """
    在本地安全地执行 Textoon 命令，并将结果打包成 zip 文件。
    成功则返回打包后的 zip 文件路径，失败则返回 None。
    """
    base_output_dir = os.path.join(os.getcwd(), "output")
    job_output_dir = os.path.join(base_output_dir, job_id)
    zip_output_path = os.path.join(base_output_dir, f"{job_id}.zip")

    # 清理旧文件并创建新目录
    if os.path.exists(job_output_dir):
        shutil.rmtree(job_output_dir)
    os.makedirs(job_output_dir, exist_ok=True)

    logger.info(f"Starting local task {job_id}. Output will be in: {job_output_dir}")

    try:
        # 确保 ComfyUI 正在另一个终端中独立运行！
        # 这是 Textoon 运行的硬性要求。
        logger.info("Prerequisite: Ensuring ComfyUI is running in a separate process...")
        
        command = [
            TEXTOON_PYTHON_PATH,
            TEXTOON_SCRIPT_PATH,
            "--text_prompt", prompt,
            "--output_path", job_output_dir
        ]
        
        # 执行命令，设置较长的超时时间
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=600  # 10分钟超时
        )
        logger.info(f"Textoon script for job {job_id} executed successfully.")
        logger.debug(f"Script output:\n{result.stdout}")

        # 将生成的结果目录打包成一个 zip 文件
        logger.info(f"Zipping output directory '{job_output_dir}' to '{zip_output_path}'")
        shutil.make_archive(os.path.join(base_output_dir, job_id), 'zip', job_output_dir)
        
        return zip_output_path

    except subprocess.TimeoutExpired:
        logger.error(f"Task {job_id} timed out.")
        return None
    except subprocess.CalledProcessError as e:
        logger.error(f"Textoon script failed for job {job_id}:\n--- STDERR ---\n{e.stderr}\n--- STDOUT ---\n{e.stdout}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred during local execution for job {job_id}: {e}")
        return None

def serve_file_with_ngrok(file_path: str) -> str | None:
    """
    为一个文件启动一个临时的 Web 服务器和 ngrok 隧道，并返回公网 URL。
    """
    if not os.path.exists(file_path):
        logger.error(f"File not found for serving: {file_path}")
        return None

    # ngrok 只能服务于一个目录，所以我们服务于文件所在的目录
    directory = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    
    # 在一个单独的线程中启动 HTTP 服务器
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass # 抑制日志输出

    httpd = HTTPServer(("localhost", 8082), QuietHandler)
    
    def serve():
        # 切换到目标目录才能正确提供服务
        os.chdir(directory)
        httpd.serve_forever()

    thread = threading.Thread(target=serve)
    thread.daemon = True
    thread.start()
    logger.info(f"Serving directory '{directory}' on http://localhost:8082")

    try:
        # 启动 ngrok 隧道
        listener = ngrok.connect(8082, "http")
        public_url = listener.public_url
        download_url = f"{public_url}/{filename}"
        logger.info(f"ngrok tunnel created. Download URL: {download_url}")
        return download_url
    except Exception as e:
        logger.error(f"Failed to create ngrok tunnel: {e}")
        return None


# ==============================================================================
# --- 4. 主循环 (Main Loop) ---
# ==============================================================================

def main():
    logger.info("======================================================")
    logger.info("=== Local Textoon Worker for Telegram Bot is starting...")
    logger.info("=== Prerequisite Check:")
    logger.info("=== 1. Is ComfyUI with all custom nodes running?")
    logger.info("=== 2. Are all .safetensors models in the correct folders?")
    logger.info("=== 3. Are all paths in this script configured correctly?")
    logger.info("======================================================")
    
    active_listener = None

    while True:
        try:
            # 在开始新任务前，关闭旧的 ngrok 隧道
            if active_listener:
                logger.info("Closing previous ngrok tunnel...")
                ngrok.disconnect(active_listener.public_url)
                active_listener = None

            # 1. 从云端调度中心获取任务
            response = requests.get(f"{DISPATCH_CENTER_URL}/api/get-task", timeout=15)
            response.raise_for_status()
            task = response.json()

            if task.get("job_id"):
                job_id = task["job_id"]
                prompt = task["prompt"]
                logger.info(f"Fetched new task: {job_id} - Prompt: '{prompt}'")

                # 2. 本地执行画图并打包
                zip_file_path = run_textoon_locally(job_id, prompt)
                
                status_to_update = "FAILED"
                public_url = None

                if zip_file_path:
                    # 3. 为结果文件创建公网链接
                    public_url = serve_file_with_ngrok(zip_file_path)
                    if public_url:
                        status_to_update = "COMPLETED"
                        # 保存 listener 以便下次循环时关闭
                        active_listener = ngrok.get_listeners()[0] 
                
                # 4. 向调度中心更新任务状态
                logger.info(f"Updating task {job_id} status to {status_to_update}")
                requests.post(
                    f"{DISPATCH_CENTER_URL}/api/update-task",
                    json={
                        "job_id": job_id,
                        "status": status_to_update,
                        "result_url": public_url
                    },
                    timeout=30
                )
                
                # 如果任务成功，我们让隧道保持一段时间以便用户下载
                if status_to_update == "COMPLETED":
                    logger.info("Task completed. Keeping ngrok tunnel open for 10 minutes for download.")
                    time.sleep(600)

            else:
                # 如果没有任务，等待
                time.sleep(10)

        except requests.exceptions.RequestException as e:
            logger.error(f"Could not connect to Dispatch Center: {e}. Retrying in 30 seconds.")
            time.sleep(30)
        except Exception as e:
            logger.error(f"An unexpected error occurred in the main loop: {e}", exc_info=True)
            time.sleep(30)

if __name__ == "__main__":
    main()

