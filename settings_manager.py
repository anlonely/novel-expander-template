"""
settings_manager.py — 运行时可修改的设置管理

功能：
- 将可调参数从 config.py 硬编码解耦，存入 data/settings.json
- 提供 get/set/reset/all 方法供 API 层调用
- 启动时自动合并默认值（新增字段自动补全）
- 变更 API_BASE / API_KEY 时自动重建 OpenAI client
- 多 API 配置（Profile）管理：增删改查、切换
"""

import json
import logging
import os
import uuid
from copy import deepcopy
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(config.DATA_DIR, "settings.json")
PROFILES_PATH = os.path.join(config.DATA_DIR, "api_profiles.json")

# ========== 默认设置（与 config.py 硬编码值保持一致） ==========
DEFAULTS: Dict[str, Any] = {
    # --- 速率限制 ---
    "request_delay": config.REQUEST_DELAY,
    "rate_limit_backoff_base": config.RATE_LIMIT_BACKOFF_BASE,
    "rate_limit_backoff_max": config.RATE_LIMIT_BACKOFF_MAX,
    "rate_limit_backoff_factor": config.RATE_LIMIT_BACKOFF_FACTOR,
    "inter_chapter_delay_seconds": config.INTER_CHAPTER_DELAY_SECONDS,
    "progress_debounce_seconds": config.PROGRESS_DEBOUNCE_SECONDS,

    # --- 内容与分段 ---
    "context_before_chars": config.CONTEXT_BEFORE_CHARS,
    "context_after_chars": config.CONTEXT_AFTER_CHARS,
    "segment_size": config.SEGMENT_SIZE,
    "segment_min_size": config.SEGMENT_MIN_SIZE,
    "segment_max_size": config.SEGMENT_MAX_SIZE,
    "expansion_ratio_target": config.EXPANSION_RATIO_TARGET,
    "max_retries": config.MAX_RETRIES,
    "conserve_requests": config.CONSERVE_REQUESTS,
    "model_fallback_order": ",".join(config.MODEL_FALLBACK_ORDER),
    "output_reserved_tokens": config.OUTPUT_RESERVED_TOKENS,
    "system_prompt_reserved_tokens": config.SYSTEM_PROMPT_RESERVED_TOKENS,
}

# 当前生效的设置（内存缓存）
_current: Dict[str, Any] = {}


# ========== 设置元数据（用于前端分组和校验） ==========
SETTINGS_META: Dict[str, Dict[str, Any]] = {
    "request_delay":                {"group": "rate",     "label": "请求间隔 (秒)",     "type": "number", "min": 0, "max": 120, "step": 1},
    "rate_limit_backoff_base":      {"group": "rate",     "label": "429退避基础 (秒)",  "type": "number", "min": 1, "max": 600, "step": 1},
    "rate_limit_backoff_max":       {"group": "rate",     "label": "429退避上限 (秒)",  "type": "number", "min": 30, "max": 3600, "step": 10},
    "rate_limit_backoff_factor":    {"group": "rate",     "label": "退避倍增因子",      "type": "number", "min": 1, "max": 10, "step": 0.5},
    "inter_chapter_delay_seconds":  {"group": "rate",     "label": "章节间冷却 (秒)",   "type": "number", "min": 0, "max": 300, "step": 1},
    "progress_debounce_seconds":    {"group": "rate",     "label": "进度写库间隔 (秒)", "type": "number", "min": 1, "max": 30, "step": 1},

    "context_before_chars":         {"group": "content",  "label": "前文上下文 (字符)",  "type": "number", "min": 500, "max": 20000, "step": 500},
    "context_after_chars":          {"group": "content",  "label": "后文上下文 (字符)",  "type": "number", "min": 500, "max": 10000, "step": 500},
    "segment_size":                 {"group": "content",  "label": "分段目标 (字符)",    "type": "number", "min": 2000, "max": 20000, "step": 1000},
    "segment_min_size":             {"group": "content",  "label": "分段最小 (字符)",    "type": "number", "min": 500, "max": 10000, "step": 500},
    "segment_max_size":             {"group": "content",  "label": "分段最大 (字符)",    "type": "number", "min": 5000, "max": 50000, "step": 1000},
    "expansion_ratio_target":       {"group": "content",  "label": "扩写目标倍率",      "type": "string"},
    "max_retries":                  {"group": "content",  "label": "最大重试次数",       "type": "number", "min": 1, "max": 10, "step": 1},
    "conserve_requests":            {"group": "content",  "label": "省请求模式",         "type": "boolean"},
    "model_fallback_order":         {"group": "content",  "label": "模型轮换顺序",       "type": "string"},
    "output_reserved_tokens":       {"group": "content",  "label": "输出预留Token",      "type": "number", "min": 4000, "max": 32000, "step": 1000},
    "system_prompt_reserved_tokens": {"group": "content", "label": "系统提示预留Token",  "type": "number", "min": 1000, "max": 16000, "step": 500},
}

# 分组显示名（不包含 api 组，api 由 profiles 管理）
GROUP_LABELS = {
    "rate":    "速率限制",
    "content": "内容与分段",
}


# ========== API Profiles 管理 ==========

_profiles: List[Dict[str, Any]] = []
_active_profile_id: str = ""


def _default_profile() -> Dict[str, Any]:
    """创建默认 API 配置 profile"""
    return {
        "id": str(uuid.uuid4())[:8],
        "name": "配置1",
        "api_base": config.API_BASE,
        # Secrets should not be persisted to disk by default.
        "api_key": "",
        "admin_api_key": "",
        "default_model": config.DEFAULT_MODEL,
    }


def _load_profiles() -> tuple:
    """从 api_profiles.json 加载配置列表"""
    if not os.path.exists(PROFILES_PATH):
        return [], ""
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        profiles = data.get("profiles", [])
        active_id = data.get("active_profile_id", "")
        return profiles, active_id
    except Exception as e:
        logger.warning(f"Failed to load api_profiles.json: {e}")
        return [], ""


def _save_profiles():
    """保存 profiles 到磁盘"""
    os.makedirs(os.path.dirname(PROFILES_PATH), exist_ok=True)
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "profiles": _profiles,
            "active_profile_id": _active_profile_id,
        }, f, indent=2, ensure_ascii=False)


def _apply_active_profile():
    """将当前激活的 profile 应用到 config 模块"""
    profile = get_active_profile()
    if not profile:
        return
    # Secrets should come from environment by default. Profiles can override them,
    # but if the profile has empty keys we fall back to env-derived defaults.
    config.API_BASE = profile.get("api_base") or config.API_BASE
    config.API_KEY = profile.get("api_key") or config.ENV_API_KEY
    config.ADMIN_API_KEY = profile.get("admin_api_key") or config.ENV_ADMIN_API_KEY
    config.DEFAULT_MODEL = profile.get("default_model", "grok-4.20-auto")
    fallback_order = profile.get("model_fallback_order")
    if fallback_order:
        config.MODEL_FALLBACK_ORDER = [
            m.strip() for m in str(fallback_order).split(",") if m.strip()
        ]
    _reinit_ai_client()
    logger.info(f"Applied API profile: {profile['name']} (id={profile['id']})")


def init_profiles():
    """启动时初始化 profiles"""
    global _profiles, _active_profile_id
    _profiles, _active_profile_id = _load_profiles()

    if not _profiles:
        # 首次运行：用 config.py 中的值创建默认 profile
        default = _default_profile()
        _profiles = [default]
        _active_profile_id = default["id"]
        _save_profiles()
        logger.info("Created default API profile from config.py")
    else:
        changed = False
        for p in _profiles:
            if "default_model" not in p:
                p["default_model"] = config.DEFAULT_MODEL
                changed = True
            if "model_fallback_order" not in p:
                p["model_fallback_order"] = ",".join(config.MODEL_FALLBACK_ORDER)
                changed = True
        # 确保 active_id 有效
        ids = [p["id"] for p in _profiles]
        if _active_profile_id not in ids:
            _active_profile_id = ids[0]
            changed = True
        if changed:
            _save_profiles()

    _apply_active_profile()


def get_profiles() -> List[Dict[str, Any]]:
    """获取所有 profiles（返回副本，脱敏后用于 API 输出）"""
    out = []
    for p in _profiles:
        cp = deepcopy(p)
        api_key = str(cp.get("api_key") or "")
        admin_key = str(cp.get("admin_api_key") or "")
        cp["has_api_key"] = bool(api_key)
        cp["has_admin_api_key"] = bool(admin_key)
        # Never return secrets to clients.
        cp["api_key"] = ""
        cp["admin_api_key"] = ""
        out.append(cp)
    return out


def get_active_profile_id() -> str:
    """获取当前激活 profile 的 id"""
    return _active_profile_id


def get_active_profile() -> Optional[Dict[str, Any]]:
    """获取当前激活的 profile"""
    for p in _profiles:
        if p["id"] == _active_profile_id:
            return deepcopy(p)
    return None


def add_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """新增一个 API 配置"""
    profile = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", f"配置{len(_profiles) + 1}"),
        "api_base": data.get("api_base", ""),
        "api_key": data.get("api_key", ""),
        "admin_api_key": data.get("admin_api_key", ""),
        "default_model": data.get("default_model", "grok-4.20-auto"),
        "model_fallback_order": data.get("model_fallback_order", ",".join(config.MODEL_FALLBACK_ORDER)),
    }
    _profiles.append(profile)
    _save_profiles()
    logger.info(f"Added API profile: {profile['name']} (id={profile['id']})")
    return deepcopy(profile)


def update_profile(profile_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """更新指定 profile"""
    for p in _profiles:
        if p["id"] == profile_id:
            for key in ("name", "api_base", "default_model", "model_fallback_order"):
                if key in data:
                    p[key] = data[key]
            # Secrets are write-only in the UI: an empty value means "keep current".
            # This lets users edit URL/model without accidentally wiping saved keys.
            if data.get("api_key"):
                p["api_key"] = data["api_key"]
            if data.get("admin_api_key"):
                p["admin_api_key"] = data["admin_api_key"]
            _save_profiles()
            # 如果更新的是当前激活的 profile，重新应用
            if profile_id == _active_profile_id:
                _apply_active_profile()
            logger.info(f"Updated API profile: {p['name']} (id={profile_id})")
            return deepcopy(p)
    return None


def delete_profile(profile_id: str) -> bool:
    """删除指定 profile（至少保留一个）"""
    global _active_profile_id
    if len(_profiles) <= 1:
        return False
    idx = None
    for i, p in enumerate(_profiles):
        if p["id"] == profile_id:
            idx = i
            break
    if idx is None:
        return False

    _profiles.pop(idx)
    # 如果删的是激活的，切换到第一个
    if profile_id == _active_profile_id:
        _active_profile_id = _profiles[0]["id"]
        _apply_active_profile()
    _save_profiles()
    logger.info(f"Deleted API profile: {profile_id}")
    return True


def switch_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    """切换激活的 profile"""
    global _active_profile_id
    for p in _profiles:
        if p["id"] == profile_id:
            _active_profile_id = profile_id
            _save_profiles()
            _apply_active_profile()
            return deepcopy(p)
    return None


# ========== 通用设置管理 ==========

def _load_from_disk() -> Dict[str, Any]:
    """从 settings.json 读取设置"""
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load settings.json: {e}")
        return {}


def _save_to_disk(data: Dict[str, Any]):
    """将设置写入 settings.json"""
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _sync_to_config():
    """将当前设置同步回 config 模块的全局变量，使所有引用 config.XXX 的代码立即生效"""
    mapping = {
        "request_delay": "REQUEST_DELAY",
        "rate_limit_backoff_base": "RATE_LIMIT_BACKOFF_BASE",
        "rate_limit_backoff_max": "RATE_LIMIT_BACKOFF_MAX",
        "rate_limit_backoff_factor": "RATE_LIMIT_BACKOFF_FACTOR",
        "inter_chapter_delay_seconds": "INTER_CHAPTER_DELAY_SECONDS",
        "progress_debounce_seconds": "PROGRESS_DEBOUNCE_SECONDS",
        "context_before_chars": "CONTEXT_BEFORE_CHARS",
        "context_after_chars": "CONTEXT_AFTER_CHARS",
        "segment_size": "SEGMENT_SIZE",
        "segment_min_size": "SEGMENT_MIN_SIZE",
        "segment_max_size": "SEGMENT_MAX_SIZE",
        "expansion_ratio_target": "EXPANSION_RATIO_TARGET",
        "max_retries": "MAX_RETRIES",
        "conserve_requests": "CONSERVE_REQUESTS",
        "output_reserved_tokens": "OUTPUT_RESERVED_TOKENS",
        "system_prompt_reserved_tokens": "SYSTEM_PROMPT_RESERVED_TOKENS",
    }
    for key, config_attr in mapping.items():
        if key in _current:
            setattr(config, config_attr, _current[key])
    if "model_fallback_order" in _current:
        config.MODEL_FALLBACK_ORDER = [
            m.strip() for m in str(_current["model_fallback_order"]).split(",") if m.strip()
        ]


def init_settings():
    """启动时调用：从磁盘加载并合并默认值，同时初始化 profiles"""
    global _current
    saved = _load_from_disk()
    _current = deepcopy(DEFAULTS)
    # 合并已保存的设置（只覆盖已知 key）
    for key in DEFAULTS:
        if key in saved:
            _current[key] = saved[key]
    _sync_to_config()

    # 初始化 API profiles
    init_profiles()

    logger.info("Settings loaded from %s", SETTINGS_PATH)


def get_all() -> Dict[str, Any]:
    """返回所有当前设置（脱敏后用于 API 输出）"""
    data = deepcopy(_current)
    api_key = str(data.get("api_key") or "")
    admin_key = str(data.get("admin_api_key") or "")
    data["has_api_key"] = bool(api_key)
    data["has_admin_api_key"] = bool(admin_key)
    # Never return secrets to clients.
    if "api_key" in data:
        data["api_key"] = ""
    if "admin_api_key" in data:
        data["admin_api_key"] = ""
    return data


def get(key: str, default: Any = None) -> Any:
    """获取单个设置值"""
    return _current.get(key, default)


def update(changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    批量更新设置并持久化。
    返回实际变更的 key-value。
    """
    changed = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue  # 忽略未知 key
        if key in {"api_key", "admin_api_key"}:
            # Secrets should be set via environment (.env), not via API/UI.
            continue
        if _current.get(key) != value:
            _current[key] = value
            changed[key] = value

    if changed:
        _save_to_disk(_current)
        _sync_to_config()
        logger.info("Settings updated: %s", list(changed.keys()))

    return changed


def reset_to_defaults() -> Dict[str, Any]:
    """重置所有设置为默认值"""
    global _current
    _current = deepcopy(DEFAULTS)
    _save_to_disk(_current)
    _sync_to_config()
    logger.info("Settings reset to defaults")
    return deepcopy(_current)


def get_meta() -> Dict[str, Any]:
    """返回设置元数据（分组、类型、校验规则），供前端渲染"""
    return {
        "groups": GROUP_LABELS,
        "fields": SETTINGS_META,
    }


def _reinit_ai_client():
    """重建 ai_service 的 OpenAI client"""
    try:
        import ai_service
        ai_service.client = ai_service.AsyncOpenAI(
            api_key=config.API_KEY,
            base_url=config.API_BASE,
            default_headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        logger.info("AI client reinitialized with new API config")
    except Exception as e:
        logger.error(f"Failed to reinitialize AI client: {e}")
