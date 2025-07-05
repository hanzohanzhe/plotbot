# 使用一个轻量级的 Python 官方镜像作为基础
FROM python:3.11-slim-buster

# 设置容器内的工作目录
WORKDIR /app

# 将依赖文件复制到工作目录
COPY requirements.txt .

# 使用 pip 安装所有依赖项
# --no-cache-dir 选项可以减小镜像大小
RUN pip install --no-cache-dir -r requirements.txt

# 将您的应用程序代码复制到工作目录
COPY server.py .

# 声明容器将监听的端口
EXPOSE 8000

# 容器启动时要执行的命令
# 使用 uvicorn 来运行您的 FastAPI 应用
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
