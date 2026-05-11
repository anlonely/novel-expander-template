import os
import secrets

# API配置（SuperGrok 订阅，走中转站）
API_BASE = os.getenv("API_BASE", "https://grok.anlonely.me/v1")
# IMPORTANT: Do not hardcode secrets in repo. Use environment variables.
# If your upstream proxy does not require a key, leaving API_KEY empty is ok.
ENV_API_KEY = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY") or ""
ENV_ADMIN_API_KEY = os.getenv("ADMIN_API_KEY") or ""
API_KEY = ENV_API_KEY
ADMIN_API_KEY = ENV_ADMIN_API_KEY
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "grok-4.20-auto")
MODEL_FALLBACK_ORDER = [
    m.strip()
    for m in os.getenv(
        "MODEL_FALLBACK_ORDER",
        "grok-4.20-auto,grok-4.20-fast,grok-4.20-expert",
    ).split(",")
    if m.strip()
]

# 路径配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "novels.db")

# AI配置
CONTEXT_BEFORE_CHARS = 2200  # 扩写时前文上下文；过长会诱发复述和状态漂移
CONTEXT_AFTER_CHARS = 1200   # 扩写时后文上下文；只保留衔接锚点
SEGMENT_SIZE = 6000   # 长章节分段目标大小（字符数）
SEGMENT_MIN_SIZE = 2000  # 分段最小大小（避免碎片段，会与相邻段合并）
SEGMENT_MAX_SIZE = 15000  # 分段最大大小（单个场景超长时的硬上限）
MAX_RETRIES = 3  # 单 token 模式下减少空转重试
EXPANSION_RATIO_TARGET = "5-10"  # 扩写目标倍率

# Token 估算
TOKEN_PER_CHAR_ZH = 1.5  # 中文字符约 1.5 token
TOKEN_PER_CHAR_EN = 0.25  # 英文字符约 0.25 token
# 各模型上下文窗口（token）— SuperGrok 可用模型
MODEL_MAX_TOKENS = {
    "grok-4.20-auto": 131072,
    "grok-4.20-fast": 131072,
    "grok-4.20-expert": 131072,
    "grok-4": 131072,
    "grok-3": 131072,       # 推荐：创作质量高，上下文大，限流友好
    "grok-3-mini": 131072,  # 快速：适合非创作任务（分析/摘要），创作质量偏弱
}
DEFAULT_MAX_TOKENS = 131072
OUTPUT_RESERVED_TOKENS = 16000  # 为输出预留的 token 数（分段模式下每段输出约 3000-8000 tokens 足够）
SYSTEM_PROMPT_RESERVED_TOKENS = 4000  # 为系统提示词预留的 token 数（放开写模式系统提示更长）

# 质量感知的 one-pass 最大章节字数（超过则强制分段，防止输出过长导致质量下降）
ONE_PASS_MAX_CHARS = {
    "balanced": 20000,   # 稳妥模式：2万字以内一次处理
    "nuanced": 15000,    # 细腻模式：1.5万字
    "unleashed": 7000,   # 放开写模式：更早分段，避免单请求遗忘和短输出
}

# 质量感知的分段目标大小（字符数）
SEGMENT_SIZE_BY_QUALITY = {
    "balanced": 8000,
    "nuanced": 6000,
    "unleashed": 3500,   # 放开写模式每段更小，让 AI 集中精力写好每段
}
CONSERVE_REQUESTS = os.getenv("CONSERVE_REQUESTS", "true").lower() == "true"  # 单 token 省请求模式
SKIP_IF_NO_CONTENT = os.getenv("SKIP_IF_NO_CONTENT", "true").lower() == "true"  # 未检测到省略时是否跳过扩写
EXPANSION_CHECK_MODE = os.getenv("EXPANSION_CHECK_MODE", "romance_or_omission")  # romance_or_omission / omission_only / always

# 动态计算：单次请求可用于内容的最大字符数
def get_max_content_chars(model: str = None) -> int:
    """根据模型动态计算可用于内容的最大字符数"""
    model = model or DEFAULT_MODEL
    max_tokens = MODEL_MAX_TOKENS.get(model, DEFAULT_MAX_TOKENS)
    available_tokens = max_tokens - OUTPUT_RESERVED_TOKENS - SYSTEM_PROMPT_RESERVED_TOKENS
    # 中文为主，用 TOKEN_PER_CHAR_ZH 估算
    return int(available_tokens / TOKEN_PER_CHAR_ZH)


def get_model_candidates(model: str = None) -> list[str]:
    """返回模型尝试顺序：指定模型优先，其余按 fallback 顺序补齐。"""
    preferred = model or DEFAULT_MODEL
    if preferred == "grok-4":
        preferred = DEFAULT_MODEL
    order = MODEL_FALLBACK_ORDER or [DEFAULT_MODEL]
    candidates = [preferred] if preferred else []
    candidates.extend(m for m in order if m and m not in candidates)
    return candidates

# 自适应限流
REQUEST_DELAY = 3.0  # 单 token 反代的保守请求间隔
RATE_LIMIT_BACKOFF_BASE = 15.0   # 429 退避基础秒数
RATE_LIMIT_BACKOFF_MAX = 300.0  # 429 退避上限秒数
RATE_LIMIT_BACKOFF_FACTOR = 2.0  # 退避倍增因子
TOKEN_POOL_CHECK_INTERVAL = 20.0  # token 池不可用时的检查间隔（秒）
TOKEN_POOL_WAIT_MAX = 900.0  # token 池等待最长时间（秒）

# 进度更新节流
PROGRESS_DEBOUNCE_SECONDS = 3.0  # 进度写库最小间隔

# 章节级发送节流
INTER_CHAPTER_DELAY_SECONDS = 3.0  # 章节之间的额外冷却时间

# 服务器配置
HOST = "0.0.0.0"
PORT = 8899

# 站点访问密码。为空时关闭应用层登录，便于本地开发。
SITE_AUTH_USERNAME = os.getenv("SITE_AUTH_USERNAME", "novel")
SITE_AUTH_PASSWORD = os.getenv("SITE_AUTH_PASSWORD", "")
SITE_AUTH_COOKIE = os.getenv("SITE_AUTH_COOKIE", "novel_expander_session")
SITE_AUTH_SECRET = os.getenv("SITE_AUTH_SECRET") or ENV_ADMIN_API_KEY or ENV_API_KEY or secrets.token_urlsafe(32)
SITE_AUTH_SESSION_DAYS = int(os.getenv("SITE_AUTH_SESSION_DAYS", "30"))
