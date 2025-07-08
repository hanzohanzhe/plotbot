# 选择一个 Python 基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件到工作目录
COPY requirements.txt .

# 安装 requirements.txt 中定义的所有库
# 这一步会安装 Pillow 以及其他所有必要的库
RUN pip install --no-cache-dir -r requirements.txt

# 将你的应用代码复制到工作目录
COPY . .

# 暴露你的应用运行的端口
EXPOSE 8000

# 启动应用的命令
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
