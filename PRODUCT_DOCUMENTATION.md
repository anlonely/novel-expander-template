# 小说扩写助手产品文档

本文档用于产品、流程和代码审查。项目根目录：`/Users/bing/novel-expander`。当前本地服务默认运行在 `http://0.0.0.0:8899/`，前端是单页 Vue 应用，后端是 FastAPI + SQLite。

## 1. 产品定位

小说扩写助手用于导入中文小说 TXT，按章节管理原文与扩写版本，并调用兼容 OpenAI Chat Completions 协议的模型对章节中被省略、符号替代、隐喻替代或明显写薄的内容做还原/增密。

核心目标：

- 保留原章节骨架、人物、事件顺序、叙事视角和结尾结果。
- 对没有明确删减或薄写痕迹的章节原样跳过，避免无中生有。
- 对长章节按场景自动分段，传递上下文摘要、首尾锚点和上一段扩写尾部状态，降低丢剧情、串章和提前收束的概率。
- 模型拒绝处理时不把拒绝文本写入正文，直接回落原文并标记跳过。
- 支持人工直接编辑原文和扩写内容，用于修正模型输出。

## 2. 技术结构

主要文件：

- `app.py`：FastAPI 应用、登录、API 路由、任务队列、SSE、导出、后台扩写 worker。
- `ai_service.py`：模型调用、文本规范化、提示词、检测、扩写、长章节分段、完整性校验、摘要生成。
- `config.py`：默认 API、模型、上下文、分段、重试、鉴权等配置。
- `settings_manager.py`：运行时设置与 API Profile 管理，持久化到 `data/settings.json`、`data/api_profiles.json`。
- `prompt_store.py`：可编辑提示词持久化，持久化到 `data/prompts.json`。
- `parser.py`：TXT 编码检测、章节识别、无章节文本自动分段。
- `models.py`：SQLAlchemy 模型，SQLite 数据库位于 `data/novels.db`。
- `static/index.html`、`static/js/app.js`、`static/css/style.css`：前端 UI。

## 3. 数据模型

### Novel

- `id`：小说 ID。
- `title`：小说标题，默认从文件名提取。
- `original_filename`：上传文件名。
- `global_summary`：全局角色/设定摘要，跨章节累积，供后续扩写参考。
- `created_at` / `updated_at`：时间戳。

### Chapter

- `id`：章节 ID。
- `novel_id`：所属小说。
- `title`：章节标题。
- `sort_order`：章节顺序。
- `original_content`：原文。
- `expanded_content`：当前扩写内容，可为空。
- `expanded_content_prev`：上一版扩写内容，用于撤销。
- `summary`：章节摘要，用于后续章节上下文。
- `skipped`：是否判定无需扩写或拒绝后回落原文。
- `status`：`pending`、`expanding`、`completed`、`failed`、`skipped` 等。
- `progress`：章节扩写进度。
- `error_message`：失败原因。

### ExpandTask

- `id`：任务 ID。
- `novel_id`：所属小说。
- `status`：`queued`、`running`、`pausing`、`paused`、`completed`、`failed`、`cancelled`、`interrupted`。
- `model`：本任务使用的模型。
- `mode` / `quality`：历史兼容字段。当前产品固定为默认综合模式，服务端强制 `mode="one_pass"`、`quality="balanced"`。
- `progress`：任务总体进度。
- `total_chapters`、`completed_chapters`、`failed_chapters`、`skipped_chapters`：统计信息。
- `chapter_ids_json`：本次任务限定处理的章节 ID。
- `use_expanded_as_base`：是否基于已有扩写继续扩写。
- `last_completed_index`：暂停/恢复用的章节索引。
- `failed_chapter_ids_json`：失败章节列表。
- `queue_priority`、`queued_at`：队列排序。
- `error_message`：任务错误。

## 4. 完整产品流程

### 4.1 登录与访问

配置项：

- `SITE_AUTH_USERNAME`：默认 `novel`。
- `SITE_AUTH_PASSWORD`：为空时关闭应用层登录。
- `SITE_AUTH_COOKIE`：默认 `novel_expander_session`。
- `SITE_AUTH_SECRET`：默认从 `ADMIN_API_KEY`、`API_KEY` 或随机值生成。
- `SITE_AUTH_SESSION_DAYS`：默认 30 天。

流程：

1. 用户访问 `/`。
2. 如果设置了站点密码且未登录，跳转 `/login`。
3. 登录成功后写入签名 Cookie。
4. 后续 API 请求由中间件鉴权。

### 4.2 API 配置流程

入口：前端“设置 -> API 配置”。

Profile 字段：

- `name`：配置名称。
- `api_base`：兼容 OpenAI 协议的 API 地址。
- `api_key`：写入型字段，接口返回时脱敏为空。
- `admin_api_key`：可选，用于 token 状态检查。
- `default_model`：默认模型。
- `model_fallback_order`：模型失败后的轮换顺序，逗号分隔。

流程：

1. `/api/profiles` 读取所有配置和当前激活配置。
2. 新增/编辑配置时保存到 `data/api_profiles.json`。
3. 切换配置时调用 `settings_manager._apply_active_profile()`，同步到 `config.API_BASE`、`config.API_KEY`、`config.ADMIN_API_KEY`、`config.DEFAULT_MODEL`、`config.MODEL_FALLBACK_ORDER`。
4. 同步后重建模型客户端。

注意：

- API Key 和 Admin Key 不会明文返回给前端。
- 编辑 Profile 时空 Key 表示保留旧值，不会清空旧 Key。

### 4.3 上传小说流程

入口：前端“上传小说”。

后端接口：`POST /api/novels/upload`。

流程：

1. 前端上传 `.txt` 文件。
2. `parser.decode_bytes()` 用 `chardet` 自动识别编码，优先兼容 `utf-8`、`gb18030`、`latin-1`。
3. `parser.clean_text()` 统一换行并压缩多余空白。
4. `parser.find_chapters()` 按以下模式识别章节：
   - `第一章`、`第二十三章`
   - `第1章`
   - `Chapter 1`
   - `卷一`
   - `1.` / `1、`
5. 如果找不到章节，`parser.auto_segment()` 按约 3000 字自动拆成“第 N 节”。
6. 创建 `Novel` 和 `Chapter` 记录，章节按 `sort_order` 保存。

### 4.4 阅读与章节管理流程

主要接口：

- `GET /api/novels`：小说列表。
- `GET /api/novels/{novel_id}`：小说详情和章节列表。
- `GET /api/novels/{novel_id}/chapters/{chapter_id}`：章节正文、扩写正文、段落列表。
- `DELETE /api/novels/{novel_id}`：删除小说及章节。

前端视图：

- 原文。
- 扩写。
- 对比。

章节头部功能：

- 指令重写整章。
- 撤销扩写版本。
- 编辑原文。
- 编辑扩写。

### 4.5 扩写配置流程

当前产品已删除可选档位和“分段精细”配置。前端“扩写配置”只显示：

- 默认综合扩写说明。
- 模型选择。

服务端行为：

- `POST /api/novels/{novel_id}/expand` 创建任务时强制：
  - `mode="one_pass"`
  - `quality="balanced"`
- `expand_worker()` 运行时也强制：
  - `mode="one_pass"`
  - `quality="balanced"`
- 前端发起任务不再传 `mode` 和 `quality`。

默认综合策略：

- 短章节整章扩写。
- 超过 `ONE_PASS_MAX_CHARS` 的章节按场景分段扩写。
- 长章节分段时传递当前段摘要、前后段摘要、首尾锚点、上一段扩写结果尾部状态。

### 4.6 扩写任务创建流程

入口：

- 批量扩写：右侧任务区选择章节后开始。
- 当前章节扩写：章节头部按钮。
- 继续扩写：可基于已有扩写内容再次处理。

接口：

- `GET /api/novels/{novel_id}/expand/estimate`：预估章节数、字数、耗时、token。
- `POST /api/novels/{novel_id}/expand`：创建扩写任务。
- `GET /api/novels/{novel_id}/expand/stream`：SSE 进度流。

创建逻辑：

1. 检查小说是否存在。
2. 检查同一本小说是否已有 `queued`、`pausing`、`paused` 或 `running` 任务。
3. 根据用户选择生成 `chapter_ids_json`。
4. 创建 `ExpandTask(status="queued")`。
5. 调用 `_dispatch_next_task()`，如果没有任务运行，则启动 worker。

### 4.7 任务队列流程

接口：

- `GET /api/tasks/queue`：队列和历史任务。
- `POST /api/tasks/{task_id}/prioritize`：置顶等待中的任务。
- `POST /api/tasks/{task_id}/pause`：暂停运行/排队任务。
- `POST /api/tasks/{task_id}/resume`：恢复暂停任务。
- `POST /api/tasks/{task_id}/cancel`：取消任务。
- `DELETE /api/tasks/history`：清空历史任务。运行中和队列中的任务不会被清空。
- `POST /api/novels/{novel_id}/expand/resume`：恢复中断任务。
- `POST /api/novels/{novel_id}/expand/retry-failed`：只重试失败章节。

排序要求：

- 正在运行的任务始终在任务列表顶部。
- 其次是排队/暂停等活跃任务。
- 历史任务按时间倒序。

状态更新：

- Worker 通过 SSE 广播 `progress`、`chapter_done`、`error`、`task_done`。
- 前端收到 SSE 后刷新章节、任务和日志。

### 4.8 Worker 章节处理流程

对每个章节：

1. 检查取消/暂停信号。
2. 将章节标记为 `expanding`。
3. 选择输入内容：
   - 默认使用 `original_content`。
   - 如果 `use_expanded_as_base=true` 且存在 `expanded_content`，使用扩写内容作为输入。
4. 获取下一章开头约 180 字，作为最终衔接锚点。只给最后一段使用，避免下一章内容被复制到本章末尾。
5. 合并上下文摘要：
   - 小说 `global_summary`
   - 前一章 `summary`
6. 调用 `expand_chapter_one_pass()`。
7. 如果模型拒绝，捕获 `AIRefusalError`，用输入原文作为输出，不写入拒绝文本。
8. 规范化输出文本。
9. 判断是否跳过：
   - 输出与输入相同。
   - 输出为空。
   - 输出很短且包含“无法处理/无需扩写/拒绝”等通知信号。
   - AI 拒绝处理。
10. 跳过时：
    - `status="skipped"`
    - `skipped=True`
    - 清空可能由中间保存写入的通知文本。
11. 成功时：
    - 备份上一版扩写到 `expanded_content_prev`
    - 写入 `expanded_content`
    - `status="completed"`
12. 生成章节摘要：
    - 省请求模式下使用本地摘要 `build_local_chapter_summary()`。
    - 非省请求模式可调用模型摘要 `generate_chapter_summary()`。
13. 刷新小说全局摘要 `_refresh_novel_global_summary()`。

失败处理：

- `ExpansionIntegrityError`：恢复扩写内容为失败前快照，避免半成品导出。
- 致命 API 错误：停止整个任务并标记 failed。
- 非致命章节错误：标记当前章节 failed，继续下一章。

### 4.9 短章节扩写流程

函数：`expand_chapter_one_pass()`。

流程：

1. 如果 `SKIP_IF_NO_CONTENT=true`，先调用 `quick_check_needs_expansion()`。
2. 如果检测无需扩写，直接返回原文。
3. 构建上下文：
   - 前文/全局摘要。
   - 覆盖提示 `_build_expansion_coverage_hint()`。
   - 默认综合策略说明 `_strategy_instruction()`。
   - 下一章开头锚点。
4. 计算可用上下文：
   - `get_max_content_chars(model)`
   - 扣除 prompt 开销。
   - 与 `ONE_PASS_MAX_CHARS` 取最小值。
5. 如果章节长度小于等于上限，整章一次发送。
6. 输出后调用 `_retry_with_integrity_guard()` 做完整性检查和必要重试。

### 4.10 长章节分段扩写流程

触发条件：

- 章节长度超过默认综合模式 one-pass 上限 `ONE_PASS_MAX_CHARS`。

函数：`_expand_long_chapter()`。

分段流程：

1. `split_into_paragraphs()` 拆段。
2. `_detect_scenes()` 根据空段、分隔线、时间/地点切换等场景边界识别场景。
3. `_build_segments_from_scenes()` 以场景为单位构建分段，目标大小为 `DEFAULT_SEGMENT_SIZE`，并受 `SEGMENT_MIN_SIZE`、`SEGMENT_MAX_SIZE` 约束。
4. 为每个分段调用 `build_local_chapter_summary()` 生成分段摘要。
5. 每个分段构建上下文块 `_build_segment_context_block()`，包含：
   - 当前分段序号。
   - 当前分段摘要。
   - 前一分段摘要。
   - 后一分段摘要。
   - 当前分段首尾锚点。
   - 不得复制上下文、不得输出下一章内容、不得提前收束的规则。
6. 第一段额外注入：
   - 前文/全局摘要。
   - 整章覆盖提示。
7. 非第一段额外注入：
   - 上一段扩写结果尾部状态 `_continuity_tail()`。
8. 最后一段才注入下一章开头。
9. 每段输出后：
   - 通过 `_retry_with_integrity_guard()` 校验。
   - 写入 `expanded_by_segment`。
   - 调用 `segment_save_callback()` 中间保存，防止长任务中断后完全丢失进度。
10. 全部分段完成后用空行拼接，并剥离可能泄漏的下一章参考内容。

防丢失设计：

- 分段不使用重叠正文直接拼接，避免重复。
- 上一段只传“扩写结果尾部状态”，不要求模型复述。
- 当前段首尾锚点要求模型覆盖当前分段原文的开头、推进和结尾。
- 前后段摘要让模型知道边界，但不让其输出相邻段正文。
- 完整性校验发现关键锚点缺失时会重试；仍失败则标记章节失败并恢复旧扩写。

### 4.11 完整性校验与拒绝处理

拒绝检测：

- `ai_service._detect_refusal()` 识别上游返回的拒绝、安全策略、无法处理等文本。
- `chat_completion()` 内部遇到拒绝会重试；超过重试次数抛出 `AIRefusalError`。
- Worker 捕获后直接用输入原文作为输出，并标记跳过。
- 拒绝文本不会写入 `expanded_content`。

完整性校验：

- `_source_coverage_issues()` 检查输出是否覆盖源文本关键锚点，防止只输出一小段或丢失大段剧情。
- `_strip_forbidden_context_leak()` 清理输出中可能复制的下一章开头。
- `_retry_with_integrity_guard()` 在输出过短、锚点缺失、串章时重试。
- 如果仍失败，抛出 `ExpansionIntegrityError`。

### 4.12 摘要上下文流程

章节摘要用途：

- 为后续章节扩写提供角色、关系、场景、结尾状态等上下文。

生成方式：

- `build_local_chapter_summary()`：本地轻量摘要，省请求模式默认使用。
- `generate_chapter_summary()`：模型摘要，非省请求模式使用。

全局摘要：

- `_refresh_novel_global_summary()` 汇总章节摘要，按 `sort_order` 排序，避免章节顺序错乱。
- Worker 每章完成/跳过后刷新。
- 手动编辑原文或扩写内容后也会重新生成当前章节摘要并刷新全局摘要。

### 4.13 手动编辑流程

整章编辑：

- 前端按钮：`编辑原文`、`编辑扩写`。
- 后端接口：`POST /api/novels/{novel_id}/chapters/{chapter_id}/save-content`。
- 请求体：
  - `content`：完整章节正文。
  - `is_expanded`：`true` 表示保存扩写内容，`false` 表示保存原文。

保存规则：

- 内容不能为空。
- 保存扩写内容：
  - 如果内容变化，当前 `expanded_content` 备份到 `expanded_content_prev`。
  - 新内容写入 `expanded_content`。
  - 章节标记 `completed`，`skipped=false`。
- 保存原文：
  - 写入 `original_content`。
  - 如果章节原来是 `failed`，改回 `pending`。
- 保存后重新生成章节摘要并刷新小说全局摘要。

段落编辑：

- 前端可编辑单段落。
- 保存时重组整章内容，仍调用同一个 `save-content` 接口。

### 4.14 指令重写与局部扩写流程

接口：

- `POST /api/novels/{novel_id}/chapters/{chapter_id}/rewrite`：整章指令重写。
- `POST /api/novels/{novel_id}/chapters/{chapter_id}/rewrite-paragraph`：段落流式重写。
- `POST /api/novels/{novel_id}/chapters/{chapter_id}/insert-prompt`：插入提示。
- `POST /api/novels/{novel_id}/chapters/{chapter_id}/expand-selection`：选中文本扩写。

说明：

- 这些功能属于人工精修工具，不走批量任务队列。
- 结果会更新章节扩写内容或段落内容。
- 默认重写指令来自 `default_rewrite_instruction` 提示词配置。

### 4.15 导出流程

接口：`GET /api/novels/{novel_id}/export`。

参数：

- `format`：`txt`、`docx`、`epub`。
- `separator_style`：TXT 分隔样式。

规则：

- 导出优先使用 `expanded_content`。
- 如果章节没有扩写内容或被跳过，回落 `original_content`。
- 导出文件写入 `data/exports/`。

## 5. 配置说明

### 5.1 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_BASE` | `https://grok.anlonely.me/v1` | 兼容 OpenAI 协议的接口地址 |
| `API_KEY` / `OPENAI_API_KEY` | 空 | 模型 API Key |
| `ADMIN_API_KEY` | 空 | 管理/Token 状态接口 Key |
| `DEFAULT_MODEL` | `grok-4.20-auto` | 默认模型 |
| `MODEL_FALLBACK_ORDER` | `grok-4.20-auto,grok-4.20-fast,grok-4.20-expert` | 模型轮换顺序 |
| `CONSERVE_REQUESTS` | `true` | 省请求模式 |
| `SKIP_IF_NO_CONTENT` | `true` | 检测无删减时跳过扩写 |
| `EXPANSION_CHECK_MODE` | `romance_or_omission` | 检测模式：`romance_or_omission`、`omission_only`、`always` |
| `SITE_AUTH_USERNAME` | `novel` | 站点登录用户名 |
| `SITE_AUTH_PASSWORD` | 空 | 站点登录密码，空表示不启用登录 |
| `SITE_AUTH_COOKIE` | `novel_expander_session` | 登录 Cookie 名 |
| `SITE_AUTH_SECRET` | 自动生成 | Cookie 签名密钥 |
| `SITE_AUTH_SESSION_DAYS` | `30` | 登录有效期 |

### 5.2 默认模型配置

`config.MODEL_MAX_TOKENS`：

- `grok-4.20-auto`：131072
- `grok-4.20-fast`：131072
- `grok-4.20-expert`：131072
- `grok-4`：131072
- `grok-3`：131072
- `grok-3-mini`：131072

`get_model_candidates(model)`：

- 指定模型优先。
- 其余按 `MODEL_FALLBACK_ORDER` 补齐。
- 如果指定 `grok-4`，会映射为当前默认模型。

### 5.3 内容与分段配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CONTEXT_BEFORE_CHARS` | 2200 | 扩写前文上下文长度。过长可能诱发复述和状态漂移 |
| `CONTEXT_AFTER_CHARS` | 1200 | 后文上下文长度，只保留衔接锚点 |
| `SEGMENT_SIZE` | 6000 | 运行时设置里的分段目标字符数 |
| `DEFAULT_SEGMENT_SIZE` | 6000 | 当前默认综合长章节分段目标字符数 |
| `SEGMENT_MIN_SIZE` | 2000 | 分段最小字符数，避免碎片段 |
| `SEGMENT_MAX_SIZE` | 15000 | 分段最大字符数，单场景过长时硬上限 |
| `ONE_PASS_MAX_CHARS` | 7000 | 短章整章处理上限，超过后走长章节分段 |
| `EXPANSION_RATIO_TARGET` | `5-10` | 扩写目标倍率说明 |
| `MAX_RETRIES` | 3 | 模型拒绝/失败重试次数 |
| `OUTPUT_RESERVED_TOKENS` | 16000 | 输出预留 token |
| `SYSTEM_PROMPT_RESERVED_TOKENS` | 4000 | 系统提示词预留 token |

注意：`settings_manager` 当前仍暴露 `segment_size` 并同步到 `config.SEGMENT_SIZE`，但长章节默认综合流程读取的是 `config.DEFAULT_SEGMENT_SIZE`。如果希望 UI 中“分段目标”实时影响默认综合分段，需要让设置同步 `DEFAULT_SEGMENT_SIZE` 或让 `_expand_long_chapter()` 读取 `SEGMENT_SIZE`。这是建议 Claude 重点检查的点。

### 5.4 速率限制配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `REQUEST_DELAY` | 3.0 | 单 token 反代保守请求间隔 |
| `RATE_LIMIT_BACKOFF_BASE` | 15.0 | 429 退避基础秒数 |
| `RATE_LIMIT_BACKOFF_MAX` | 300.0 | 429 退避上限 |
| `RATE_LIMIT_BACKOFF_FACTOR` | 2.0 | 退避倍增因子 |
| `TOKEN_POOL_CHECK_INTERVAL` | 20.0 | token 池不可用时检查间隔 |
| `TOKEN_POOL_WAIT_MAX` | 900.0 | token 池最长等待 |
| `PROGRESS_DEBOUNCE_SECONDS` | 3.0 | 进度写库最小间隔 |
| `INTER_CHAPTER_DELAY_SECONDS` | 3.0 | 章节之间额外冷却 |

### 5.5 运行时设置

接口：

- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/settings/reset`

持久化位置：`data/settings.json`。

设置分组：

- `rate`：速率限制。
- `content`：内容与分段。

设置更新后会同步到 `config` 模块的全局变量。

### 5.6 提示词配置

接口：

- `GET /api/prompts`
- `PUT /api/prompts`
- `POST /api/prompts/reset`

持久化位置：`data/prompts.json`。

当前暴露分组：

- `shared`：通用沉浸式写法规则。
- `analysis`：快速检测与精细分析。
- `one_pass`：默认综合扩写 System/User。
- `rewrite`：指令重写。
- `summary`：摘要上下文。

已删除/隐藏的旧概念：

- 质量档位。
- 放开写追加。
- 分段精细用户可选模式。
- 前端不再显示相关按钮，`/api/prompts` 也不应返回相关配置项。

## 6. API 摘要

### 基础与鉴权

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/login` | 登录页 |
| `POST` | `/api/login` | 登录 |
| `POST` | `/api/logout` | 登出 |
| `GET` | `/` | 前端页面 |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/model-test` | 测试模型请求 |

### 设置与 Profile

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/settings` | 获取运行时设置 |
| `PUT` | `/api/settings` | 更新运行时设置 |
| `POST` | `/api/settings/reset` | 恢复默认设置 |
| `GET` | `/api/prompts` | 获取提示词配置 |
| `PUT` | `/api/prompts` | 更新提示词配置 |
| `POST` | `/api/prompts/reset` | 重置提示词 |
| `GET` | `/api/profiles` | 获取 API 配置 |
| `POST` | `/api/profiles` | 新增 API 配置 |
| `PUT` | `/api/profiles/{profile_id}` | 修改 API 配置 |
| `DELETE` | `/api/profiles/{profile_id}` | 删除 API 配置 |
| `POST` | `/api/profiles/{profile_id}/switch` | 切换 API 配置 |
| `GET` | `/api/token-status` | Token/账号池状态 |

### 小说与章节

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/novels/upload` | 上传 TXT 小说 |
| `GET` | `/api/novels` | 小说列表 |
| `GET` | `/api/novels/{novel_id}` | 小说详情 |
| `DELETE` | `/api/novels/{novel_id}` | 删除小说 |
| `GET` | `/api/novels/{novel_id}/chapters/{chapter_id}` | 章节详情 |
| `POST` | `/api/novels/{novel_id}/chapters/{chapter_id}/undo` | 撤销扩写 |
| `POST` | `/api/novels/{novel_id}/chapters/{chapter_id}/save-content` | 保存原文/扩写内容 |
| `GET` | `/api/novels/{novel_id}/export` | 导出小说 |

### 扩写任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/novels/{novel_id}/expand` | 创建扩写任务 |
| `GET` | `/api/novels/{novel_id}/expand/stream` | SSE 进度 |
| `POST` | `/api/novels/{novel_id}/expand/cancel` | 取消当前小说任务 |
| `POST` | `/api/novels/{novel_id}/expand/resume` | 恢复中断任务 |
| `POST` | `/api/novels/{novel_id}/expand/retry-failed` | 重试失败章节 |
| `GET` | `/api/novels/{novel_id}/expand/estimate` | 扩写预估 |
| `GET` | `/api/novels/{novel_id}/expand/interrupted` | 查询中断任务 |
| `GET` | `/api/novels/{novel_id}/tasks` | 当前小说任务列表 |
| `GET` | `/api/tasks/queue` | 全局队列 |
| `DELETE` | `/api/tasks/history` | 清空历史任务 |
| `POST` | `/api/tasks/{task_id}/prioritize` | 任务置顶 |
| `POST` | `/api/tasks/{task_id}/pause` | 暂停任务 |
| `POST` | `/api/tasks/{task_id}/resume` | 恢复任务 |
| `POST` | `/api/tasks/{task_id}/cancel` | 取消任务 |

### 人工改写工具

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/novels/{novel_id}/chapters/{chapter_id}/rewrite` | 整章指令重写 |
| `POST` | `/api/novels/{novel_id}/chapters/{chapter_id}/rewrite-paragraph` | 段落流式重写 |
| `POST` | `/api/novels/{novel_id}/chapters/{chapter_id}/insert-prompt` | 插入提示 |
| `POST` | `/api/novels/{novel_id}/chapters/{chapter_id}/expand-selection` | 选中文本扩写 |

## 7. 前端页面说明

主布局：

- 顶部栏：上传、导出、导出设置、提示词、设置、退出。
- 左侧栏：小说列表、章节列表。
- 中央阅读区：原文/扩写/对比视图、章节操作按钮。
- 右侧栏：扩写进度、章节选择、任务列表。

设置弹窗：

- API 配置：多 Profile 管理。
- 扩写配置：仅默认综合扩写和模型选择。
- 其他运行时设置：速率限制、内容与分段。

提示词弹窗：

- 可编辑当前暴露的提示词。
- 可单项恢复默认或全部恢复默认。

章节编辑弹窗：

- `编辑原文`：加载 `original_content`。
- `编辑扩写`：优先加载 `expanded_content`，不存在则回落 `original_content`。
- 保存后刷新章节详情和小说详情。

## 8. 当前设计约束

- 只支持 TXT 上传；导出支持 TXT/DOCX/EPUB。
- 当前产品只保留默认综合扩写，不再提供用户选择档位。
- 长章节不会将下一章开头注入每一段，只在最后一段注入。
- 中间保存仅用于长章节分段，完整性失败时需要恢复旧版本，避免导出半成品。
- 省请求模式默认开启，摘要和检测尽量用本地逻辑减少模型调用。
- 扩写任务按小说互斥：同一本小说不能同时存在活跃扩写任务。

## 9. 建议 Claude 重点检查的问题

1. `settings_manager.segment_size` 当前同步到 `config.SEGMENT_SIZE`，但默认综合长章节分段读取 `config.DEFAULT_SEGMENT_SIZE`。如果产品期望 UI 配置影响分段目标，需要统一。
2. `ExpandTask.mode`、`ExpandTask.quality` 仍保留历史字段。当前业务强制默认综合模式，建议确认是否需要数据库迁移或仅保留兼容。
3. `ai_service.py` 中旧的两阶段精细扩写函数如果完全不再使用，建议确认是否应删除，避免未来误调用。
4. 长章节分段拼接是否会因模型对每段重复开头/结尾导致轻微重复，需要通过实际样本评估。
5. `_source_coverage_issues()` 的锚点策略是否会误伤大幅改写但剧情完整的章节。
6. 拒绝处理是否覆盖所有上游返回格式，特别是带 Markdown、JSON 或多语言拒绝句式的情况。
7. `segment_save_callback()` 中间保存与取消/失败恢复之间是否存在竞态。
8. 手动保存原文后，如果已有扩写内容，当前摘要优先使用扩写内容；这是否符合“编辑原文后重新扩写”的预期。
9. 清空任务历史是否严格不删除 `queued/running/pausing/paused` 状态任务。
10. 导出时 skipped 章节回落原文，需确认不会导出曾经误写入的拒绝文本或中间半成品。

## 10. 本地验证命令

```bash
cd /Users/bing/novel-expander
python3 -m py_compile app.py ai_service.py prompt_store.py config.py
node --check static/js/app.js
curl -sS http://127.0.0.1:8899/api/prompts | python3 -m json.tool
```

检查旧配置是否仍暴露：

```bash
rg -n "分段精细|放开写|稳妥|质量档位|expandMode|expandQuality|quality_instruction|section_user|section_system" \
  app.py ai_service.py config.py prompt_store.py static/index.html static/js/app.js static/css/style.css
```

预期：

- 前端扩写配置只显示“默认综合扩写”和“模型”。
- 章节头部显示“编辑原文”和“编辑扩写”。
- `/api/prompts` 不返回旧档位或分段精细配置。
- 批量扩写任务仍能创建，并在服务端固定使用默认综合模式。
