#!/bin/bash
cd "$(dirname "$0")"
echo "正在安装依赖..."
pip3 install -r requirements.txt
echo "启动小说扩写服务..."
echo "访问 http://localhost:8899"
python3 -m uvicorn app:app --host 0.0.0.0 --port 8899 --reload
