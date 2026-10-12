FROM python:3.13-slim

WORKDIR /app

# 复制服务端源码与手机端页面
COPY server.py mobile.html ./

# 创建数据目录（Render 磁盘将挂载到这里）
RUN mkdir -p /data

# 暴露端口（Render 会通过 PORT 环境变量传入）
EXPOSE 8000

# 启动服务端（--data 指向持久化磁盘，无控制台时自动写日志到 /data/server.log）
CMD ["python", "server.py", "--host", "0.0.0.0", "--data", "/data"]
