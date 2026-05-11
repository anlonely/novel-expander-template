# Novel Expander

一个面向中文网文场景的小说扩写工具。支持整章扩写、批量任务、进度追踪、失败重试以及 TXT / DOCX / EPUB 导出。

## 功能

- 导入 TXT 小说并自动分章
- 两种扩写模式：`one_pass` / `detailed`
- 批量扩写与 SSE 实时进度
- 中断恢复、失败重试、撤销上一版扩写
- 导出 TXT / DOCX / EPUB

## 运行

### 本机

```bash
pip3 install -r requirements.txt
python3 -m uvicorn app:app --host 0.0.0.0 --port 8899
```

### Docker

```bash
docker compose up -d --build
```

## 配置

参考 `.env.example` 配置 API 与站点登录参数。

