# Novel Expander

[![GitHub stars](https://img.shields.io/github/stars/anlonely/novel-expander-template.svg)](https://github.com/anlonely/novel-expander-template)
[![GitHub forks](https://img.shields.io/github/forks/anlonely/novel-expander-template.svg)](https://github.com/anlonely/novel-expander-template)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)

一个面向中文网文场景的小说扩写工具。支持整章扩写、按章节批量任务、进度追踪、失败重试、内容导出（TXT / DOCX / EPUB）以及基础站点登录保护。

## ✨ 功能特性

- 小说导入与章节管理（TXT）
- 两种扩写模式：`one_pass` / `detailed`
- 批量扩写任务、实时进度（SSE）与中断恢复
- 失败章节重试与最近版本撤销
- 导出格式：TXT / DOCX / EPUB
- Docker 与本机两种部署方式

## 🧱 技术栈

- FastAPI
- Uvicorn
- SQLAlchemy + SQLite
- Docker Compose

## 🚀 快速开始

### 1) 本机运行

```bash
cd /Users/bing/novel-expander
pip3 install -r requirements.txt
python3 -m uvicorn app:app --host 0.0.0.0 --port 8899
```

访问：`http://127.0.0.1:8899`

### 2) Docker 运行

```bash
cd /Users/bing/novel-expander
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
docker compose logs -f app
```

## ⚙️ 环境配置

主要环境变量（见 `.env`）：

- `API_BASE`：上游 OpenAI 兼容接口地址
- `API_KEY`：上游接口密钥
- `DEFAULT_MODEL`：默认扩写模型
- `MODEL_FALLBACK_ORDER`：模型回退顺序
- `SITE_AUTH_USERNAME` / `SITE_AUTH_PASSWORD`：站点登录账号

## 📦 目录结构

```text
.
├── app.py
├── ai_service.py
├── settings_manager.py
├── models.py
├── config.py
├── static/
├── templates/
├── data/
├── docker-compose.yml
└── requirements.txt
```

## 📌 仓库说明

- 本仓库用于承载 Novel Expander 的公开说明与基础代码结构。
- 你可以基于当前结构继续扩展：提示词配置、模型策略、任务调度与前端交互。

