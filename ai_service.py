"""
ai_service.py — 小说扩写AI服务模块

功能：
- 精准的中文网文扩写提示词（风格匹配、反幻觉、扩写比例控制）
- 基于场景边界的智能分段（不在场景中间切断）
- 句子边界感知的上下文裁剪（不在句子中间截断）
- 增强的章节摘要链（包含人物、情绪、场景、情节线索）
- 智能段落合并（消除重叠段落的重复内容）
- 分类错误处理（429限流 / 500服务端 / 网络错误 / 内容策略拒绝）
- 快速判断章节是否需要扩写，避免无意义处理
- 分段中间保存回调
"""

import asyncio
import json
import logging
import random
import re
import time
from typing import Optional, List, Dict, Any, Callable, Tuple

import httpx
from openai import AsyncOpenAI
import openai as openai_module  # 用于捕获异常类型
import config
import prompt_store

logger = logging.getLogger(__name__)


def _require_api_key():
    """Fail early with an actionable error if upstream auth is missing."""
    if not (config.API_KEY or "").strip():
        raise RuntimeError("API_KEY is not configured on the server (.env)")

# ========== API 客户端初始化 ==========

client = AsyncOpenAI(
    api_key=config.API_KEY,
    base_url=config.API_BASE,
    default_headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
)

# 最后一次请求的时间戳，用于限速
_last_request_time = 0.0

# 连续 429 计数，用于全局退避
_consecutive_429_count = 0


class AIRefusalError(Exception):
    """Raised when the upstream model returns a refusal notice instead of content."""


class ExpansionIntegrityError(Exception):
    """Raised when generated text drops source plot anchors or leaks context text."""


_TEXT_NORMALIZATION_PATTERNS = [
    (re.compile(r"马\s*叉\s*虫"), "骚"),
    (re.compile(r"馬\s*叉\s*虫"), "骚"),
    (re.compile(r"马\s*蚤"), "骚"),
    (re.compile(r"馬\s*蚤"), "骚"),
    (re.compile(r"马\s*叉"), "骚"),
    (re.compile(r"馬\s*叉"), "骚"),
]


def normalize_output_text(text: str) -> str:
    """Normalize common split-character euphemisms before saving or displaying text."""
    if not text:
        return text
    normalized = text
    for pattern, replacement in _TEXT_NORMALIZATION_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


# ========== 请求基础设施 ==========

async def _rate_limit_wait():
    """确保请求间隔不小于 REQUEST_DELAY"""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    adaptive_delay = config.REQUEST_DELAY + min(_consecutive_429_count * 8.0, 48.0)
    if elapsed < adaptive_delay:
        await asyncio.sleep(adaptive_delay - elapsed)
    _last_request_time = time.time()


def _calc_backoff(attempt: int) -> float:
    """计算指数退避延迟（含随机抖动）

    公式: min(base * factor^attempt + random(0, base), max_delay)
    """
    base = config.RATE_LIMIT_BACKOFF_BASE
    factor = config.RATE_LIMIT_BACKOFF_FACTOR
    max_delay = config.RATE_LIMIT_BACKOFF_MAX
    delay = min(base * (factor ** attempt) + random.uniform(0, base), max_delay)
    return delay


def _parse_retry_after(error) -> Optional[float]:
    """从 429 错误中解析 Retry-After header"""
    try:
        if hasattr(error, 'response') and error.response is not None:
            retry_after = (
                error.response.headers.get('retry-after')
                or error.response.headers.get('Retry-After')
            )
            if retry_after:
                return float(retry_after)
    except Exception:
        pass
    return None


async def _fetch_token_pool_status() -> Optional[Dict[str, Any]]:
    admin_url = config.API_BASE.rstrip('/').replace('/v1', '/v1/admin/tokens')
    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            resp = await http_client.get(
                admin_url,
                headers={
                    "Authorization": f"Bearer {config.ADMIN_API_KEY}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


def _token_pool_available(token_status: Optional[Dict[str, Any]]) -> bool:
    if not token_status:
        return True
    tokens_dict = token_status.get("tokens", {})
    for pool_tokens in tokens_dict.values():
        for tk in pool_tokens:
            if tk.get("status") == "active":
                return True
    return False


async def _wait_for_token_pool_recovery() -> bool:
    waited = 0.0
    while waited < config.TOKEN_POOL_WAIT_MAX:
        status = await _fetch_token_pool_status()
        if _token_pool_available(status):
            logger.info(f"ai_service: token pool recovered after {waited:.0f}s")
            return True
        await asyncio.sleep(config.TOKEN_POOL_CHECK_INTERVAL)
        waited += config.TOKEN_POOL_CHECK_INTERVAL
    logger.warning(f"ai_service: token pool still unavailable after {waited:.0f}s")
    return False


def _is_no_available_tokens_error(error: Exception) -> bool:
    text = str(error).lower()
    return "no available tokens" in text or "rate_limit_error" in text


def _reset_backoff():
    """成功请求后重置全局退避计数"""
    global _consecutive_429_count
    _consecutive_429_count = 0


async def chat_completion(
    messages: List[Dict[str, str]],
    model: str = None,
    stream: bool = False,
    temperature: float = 0.8,
    max_tokens: Optional[int] = None,
) -> str:
    """发送聊天完成请求，带分类错误处理和重试

    错误处理策略：
    - 429 RateLimitError: 解析 Retry-After，指数退避+随机抖动
    - 500+ APIStatusError: 指数退避重试
    - APIConnectionError: 短暂等待后立即重试
    - 4xx 客户端错误: 不重试，直接抛出
    - 文本拒绝: 微调 temperature 后重试
    """
    _require_api_key()
    model_candidates = config.get_model_candidates(model)
    last_error = None

    for attempt in range(config.MAX_RETRIES):
        for current_model in model_candidates:
            try:
                await _rate_limit_wait()
                if stream:
                    result = await _stream_completion_collect(messages, current_model, temperature)
                else:
                    request_kwargs = {
                        "model": current_model,
                        "messages": messages,
                        "temperature": temperature,
                        "stream": False,
                    }
                    if max_tokens:
                        request_kwargs["max_tokens"] = max_tokens
                    response = await client.chat.completions.create(
                        **request_kwargs,
                    )
                    result = response.choices[0].message.content

                if not isinstance(result, str) or not result.strip():
                    last_error = RuntimeError(f"模型返回空内容: model={current_model}")
                    logger.warning(
                        "模型返回空内容 model=%s (attempt %s/%s)",
                        current_model,
                        attempt + 1,
                        config.MAX_RETRIES,
                    )
                    if attempt < config.MAX_RETRIES - 1 or len(model_candidates) > 1:
                        await asyncio.sleep(2)
                        continue
                    raise last_error

                # 检测文本拒绝
                refusal = _detect_refusal(result)
                if refusal:
                    logger.warning(f"AI文本拒绝 ({current_model}, attempt {attempt + 1}): {refusal[:80]}")
                    if attempt < config.MAX_RETRIES - 1:
                        # 温和调整：微调 temperature，不注入 jailbreak 前缀
                        temperature = min(temperature + 0.1, 1.0)
                        await asyncio.sleep(3)
                        continue
                    raise AIRefusalError(f"AI拒绝处理此内容，已重试{config.MAX_RETRIES}次")

                # 成功 — 重置全局退避计数
                _reset_backoff()
                return result

            except openai_module.RateLimitError as e:
                last_error = e
                retry_after = _parse_retry_after(e)
                wait = retry_after or _calc_backoff(attempt)
                logger.warning(
                    f"429 限流 model={current_model} (attempt {attempt + 1}/{config.MAX_RETRIES}), "
                    f"等待 {wait:.1f}s 后尝试下一个模型"
                )
                global _consecutive_429_count
                _consecutive_429_count += 1
                await asyncio.sleep(wait)

            except openai_module.APIConnectionError as e:
                last_error = e
                logger.warning(f"网络错误 model={current_model} (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
                await asyncio.sleep(2)

            except openai_module.APIStatusError as e:
                last_error = e
                if e.status_code >= 500:
                    wait = _calc_backoff(attempt)
                    logger.warning(
                        f"服务端错误 {e.status_code} model={current_model} "
                        f"(attempt {attempt + 1}/{config.MAX_RETRIES}), 等待 {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"API请求错误 {e.status_code} model={current_model}: {e.message}")
                if len(model_candidates) > 1:
                    continue
                raise

            except Exception as e:
                last_error = e
                if "AI拒绝" in str(e):
                    raise
                logger.error(f"未知错误 model={current_model} (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(_calc_backoff(attempt))
                else:
                    raise

        if attempt < config.MAX_RETRIES - 1:
            await asyncio.sleep(_calc_backoff(attempt))

    if last_error is not None:
        raise last_error
    raise Exception(f"请求失败，已重试 {config.MAX_RETRIES} 次")


# 拒绝响应检测关键词
_REFUSAL_PATTERNS = [
    "很抱歉，作为",
    "作为AI",
    "作为Grok",
    "无法按照您的要求",
    "超出了我当前的响应边界",
    "无法生成此类",
    "拒绝处理此请求",
    "xAI核心安全准则",
    "我无法生成、扩展",
    "我无法生成、扩展、还原",
    "请提供明确以双方",
    "请提供**明确以成年",
    "I cannot",
    "I'm unable to",
    "I can't assist",
    "I can't help with",
    "content policy",
    "违反了我的",
    "无法提供此类内容",
    "不适合生成",
    "As an AI",
    "As Grok",
    # 补充：AI 自我声明式拒绝（Claude / Gemini / 国产模型常见格式）
    "拒绝生成该内容",
    "此请求通过自定义",
    "越狱/角色扮演绕过",
    "属于典型的越狱",
    "我不会按该自定义规则",
    "不会按该自定义规则",
    "不会输出任何修改",
    "应原样保留",          # 避免将"保留原文"说明误存为内容
    "我无法协助",
    "我不能协助",
    "无法协助完成",
    "这超出了我的",
    "这违反了我的",
    "该请求违反",
    "Sorry, I can",
    "I apologize, but I",
    "Unfortunately, I",
]

_OMISSION_PATTERNS = [
    re.compile(r'[…]{2,}'),
    re.compile(r'[\*×□]{3,}'),
    re.compile(r'许久后[，。…]?'),
    re.compile(r'事毕[，。]?'),
    re.compile(r'不知过了多久'),
    re.compile(r'良久[，。…]?'),
]

_ROMANCE_PATTERNS = [
    re.compile(p)
    for p in (
        r'暧昧|旖旎|亲昵|亲密|撩拨|挑逗|调情|情欲|欲念|欲火|冲动|沉沦|缠绵|销魂|春意|春色|春光|春宵|云雨|温存',
        r'脸红|羞红|羞涩|娇羞|娇嗔|娇喘|喘息|轻喘|低吟|呻吟|呢喃|嘤咛|媚眼|媚意|媚态',
        r'心跳|心悸|悸动|小鹿乱撞|呼吸急促|气息.*紊乱|气息.*灼热|气息.*滚烫',
        r'吻|亲吻|吻住|吻上|吻了|索吻|拥吻|热吻|唇|红唇|朱唇|香唇|咬唇|耳垂|脖颈|锁骨',
        r'拥抱|搂住|搂紧|抱住|抱紧|贴近|贴着|贴在|依偎|偎依|入怀|怀中|怀里',
        r'抚摸|轻抚|摩挲|揉捏|捏住|握住.*腰|扶住.*腰|搂.*腰|纤腰|腰肢|玉手|肌肤|香肩|玉腿|大腿|胸口|胸前|怀抱',
        r'床榻|床上|榻上|房中|闺房|浴池|浴桶|沐浴|更衣|衣衫|衣裙|衣带|薄纱|亵衣|衣不蔽体',
        r'孤男寡女|共处一室|洞房|成亲|双修|炉鼎|采补|合欢|媚药|春药|中药|药性|燥热',
        r'女帝|圣女|仙子|妖女|美妇|美人|绝色|倾城|尤物|娇躯|玉体|酥软|柔软',
    )
]


def _romance_signal_count(text: str) -> int:
    """Count broad romance/intimacy signals before spending model tokens."""
    return sum(len(pattern.findall(text)) for pattern in _ROMANCE_PATTERNS)


def _detect_refusal(text: str) -> Optional[str]:
    """检测AI是否返回了拒绝响应。返回匹配到的拒绝关键词或None"""
    if not text:
        return None
    # 只检查前500个字符，拒绝响应通常在开头
    check_text = text[:500]
    for pattern in _REFUSAL_PATTERNS:
        if pattern in check_text:
            return pattern
    return None


def _compact_match_text(text: str) -> str:
    """Normalize text for anchor checks without losing Chinese character order."""
    text = normalize_output_text(text or "")
    return re.sub(r"[\s\u3000\"'“”‘’《》【】\[\]（）()，,。.!！?？:：;；、…—\-]+", "", text)


def _anchor_windows(text: str, size: int = 28) -> List[str]:
    compact = _compact_match_text(text)
    if len(compact) < size:
        return [compact] if len(compact) >= 14 else []
    if len(compact) <= size * 2:
        return [compact[:size]]
    return [compact[:size], compact[-size:]]


def _paragraph_has_expansion_marker(paragraph: str) -> bool:
    return any(pattern.search(paragraph) for pattern in _OMISSION_PATTERNS)


def _source_coverage_issues(source_text: str, output_text: str) -> List[str]:
    """Detect severe source loss while allowing local expansion/rephrasing."""
    issues: List[str] = []
    source_norm = _compact_match_text(source_text)
    output_norm = _compact_match_text(output_text)
    if not source_norm or not output_norm:
        return ["输出为空，无法保存"]
    if output_norm == source_norm:
        return []
    if len(source_norm) >= 1200 and len(output_norm) < len(source_norm) * 0.55:
        issues.append(f"输出长度过短：{len(output_norm)}/{len(source_norm)}")

    paragraphs = [
        p for p in split_into_paragraphs(source_text)
        if len(_compact_match_text(p)) >= 36 and not _paragraph_has_expansion_marker(p)
    ]
    if not paragraphs:
        return issues

    # 首尾剧情锚点最容易暴露“只续写一段”或“漏掉结尾”的问题。
    head_anchors = _anchor_windows(paragraphs[0])
    tail_anchors = _anchor_windows(paragraphs[-1])
    if head_anchors and not any(anchor in output_norm for anchor in head_anchors):
        issues.append("章节开头剧情锚点缺失")
    if tail_anchors and not any(anchor in output_norm for anchor in tail_anchors):
        issues.append("章节结尾剧情锚点缺失")

    if len(paragraphs) <= 14:
        sample = paragraphs
    else:
        step = max(1, len(paragraphs) // 12)
        sample = paragraphs[::step][:12]
        if paragraphs[-1] not in sample:
            sample.append(paragraphs[-1])

    missing = 0
    checked = 0
    for paragraph in sample:
        anchors = _anchor_windows(paragraph)
        if not anchors:
            continue
        checked += 1
        if not any(anchor in output_norm for anchor in anchors):
            missing += 1
    if checked >= 6 and missing / checked >= 0.7:
        issues.append(f"原文剧情锚点大量缺失：{missing}/{checked}")
    return issues


def _strip_forbidden_context_leak(output_text: str, forbidden_context: str) -> Tuple[str, bool]:
    """Trim copied next-chapter/context text if it appears near the generated ending."""
    if not output_text or not forbidden_context:
        return output_text, False

    candidates: List[str] = []
    for line in forbidden_context.splitlines():
        cleaned = line.strip()
        if len(cleaned) >= 18 or (cleaned.startswith("【") and cleaned.endswith("】") and len(cleaned) >= 4):
            candidates.append(cleaned)
    for sentence in re.split(r"(?<=[。！？!?…])", forbidden_context):
        cleaned = sentence.strip()
        if 10 <= len(cleaned) <= 220:
            candidates.append(cleaned)

    # Prefer longer snippets so ordinary shared names/titles do not trigger trimming.
    candidates = sorted(set(candidates), key=len, reverse=True)
    start_limit = int(len(output_text) * 0.45)
    for candidate in candidates:
        pos = output_text.find(candidate)
        if pos >= start_limit:
            return output_text[:pos].rstrip(), True
    return output_text, False


def _integrity_failure_message(source_text: str, output_text: str, forbidden_context: str = "") -> str:
    cleaned, leaked = _strip_forbidden_context_leak(output_text, forbidden_context)
    issues = _source_coverage_issues(source_text, cleaned)
    if leaked:
        issues.append("输出结尾复制了下一章/上下文参考内容")
    return "；".join(issues)


async def _retry_with_integrity_guard(
    *,
    messages: List[Dict[str, str]],
    source_text: str,
    output_text: str,
    model: str,
    temperature: float,
    forbidden_context: str = "",
    max_tokens: Optional[int] = None,
) -> str:
    cleaned, leaked = _strip_forbidden_context_leak(output_text, forbidden_context)
    issues = _source_coverage_issues(source_text, cleaned)
    if not leaked and not issues:
        return cleaned

    issue_text = "；".join((["输出结尾复制了参考上下文"] if leaked else []) + issues)
    logger.warning("扩写完整性校验失败，准备重试: %s", issue_text)
    retry_messages = list(messages) + [{
        "role": "user",
        "content": (
            "上一次输出不能保存，原因："
            f"{issue_text}。\n"
            "请重新输出完整正文：必须覆盖当前章节原文的开头、正文推进和结尾；"
            "参考上下文和下一章开头只用于理解，绝对不要复制进正文；"
            "不要只续写局部片段，不要丢掉非亲密剧情、对话、结果和章节收束。"
        ),
    }]
    retry_result = await chat_completion(
        retry_messages,
        model=model,
        temperature=max(0.25, temperature - 0.08),
        max_tokens=max_tokens,
    )
    retry_result = normalize_output_text(retry_result or "")
    cleaned, leaked = _strip_forbidden_context_leak(retry_result, forbidden_context)
    issues = _source_coverage_issues(source_text, cleaned)
    if leaked or issues:
        final_issue = "；".join((["输出结尾复制了参考上下文"] if leaked else []) + issues)
        raise ExpansionIntegrityError(f"扩写完整性校验失败：{final_issue}")
    return cleaned


def _heuristic_implicit_sections(paragraphs: List[str]) -> List[Dict[str, Any]]:
    """保守启发式检测明显省略段，避免分析失败时整章乱扩写。"""
    sections = []
    for idx, para in enumerate(paragraphs):
        matched = None
        for pattern in _OMISSION_PATTERNS:
            if pattern.search(para):
                matched = pattern.pattern
                break
        if matched:
            sections.append({
                "start_para": idx,
                "end_para": idx,
                "description": f"检测到明显省略标记: {matched}",
                "type": "heuristic",
                "characters_involved": [],
                "intensity": "明显",
            })
    return sections


def _normalize_analysis_sections(
    sections: List[Dict[str, Any]],
    paragraph_count: int,
) -> List[Dict[str, Any]]:
    """Sort, clamp and merge analysis sections so replacement cannot skip text."""
    normalized: List[Dict[str, Any]] = []
    for raw in sections or []:
        if not isinstance(raw, dict) or paragraph_count <= 0:
            continue
        try:
            start = int(raw.get("start_para", 0))
            end = int(raw.get("end_para", start))
        except (TypeError, ValueError):
            continue
        start = max(0, min(start, paragraph_count - 1))
        end = max(start, min(end, paragraph_count - 1))
        item = dict(raw)
        item["start_para"] = start
        item["end_para"] = end
        normalized.append(item)

    normalized.sort(key=lambda item: (item["start_para"], item["end_para"]))
    merged: List[Dict[str, Any]] = []
    for item in normalized:
        if not merged or item["start_para"] > merged[-1]["end_para"] + 1:
            merged.append(item)
            continue
        prev = merged[-1]
        prev["end_para"] = max(prev["end_para"], item["end_para"])
        descriptions = [prev.get("description", ""), item.get("description", "")]
        prev["description"] = "；".join(part for part in descriptions if part)
        for key in ("characters_involved", "characters"):
            values = []
            for candidate in (prev.get(key), item.get(key)):
                if isinstance(candidate, list):
                    values.extend(str(v) for v in candidate if v)
            if values:
                prev[key] = list(dict.fromkeys(values))
        if item.get("metaphor_mapping") and item.get("metaphor_mapping") not in str(prev.get("metaphor_mapping", "")):
            prev["metaphor_mapping"] = "；".join(
                part for part in [str(prev.get("metaphor_mapping", "")), str(item.get("metaphor_mapping", ""))] if part
            )
    return merged


async def _stream_completion_collect(
    messages: List[Dict[str, str]],
    model: str,
    temperature: float,
) -> str:
    """流式请求但收集完整结果返回"""
    full_text = ""
    async for chunk in stream_completion(messages, model, temperature):
        full_text += chunk
    return full_text


async def stream_completion(
    messages: List[Dict[str, str]],
    model: str = None,
    temperature: float = 0.8,
):
    """流式聊天完成，逐块yield文本，带重试机制

    在遇到 429 限流或其他可重试错误时自动重试。
    """
    _require_api_key()
    model_candidates = config.get_model_candidates(model)
    last_error = None
    for attempt in range(config.MAX_RETRIES):
        for current_model in model_candidates:
            try:
                await _rate_limit_wait()
                response = await client.chat.completions.create(
                    model=current_model,
                    messages=messages,
                    temperature=temperature,
                    stream=True,
                )
                async for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                _reset_backoff()
                return  # 正常完成

            except openai_module.RateLimitError as e:
                last_error = e
                wait = _calc_backoff(attempt)
                logger.warning(
                    f"Stream 429 model={current_model} (attempt {attempt + 1}/{config.MAX_RETRIES}), "
                    f"等待 {wait:.1f}s 后尝试下一个模型"
                )
                await asyncio.sleep(wait)

            except openai_module.APIConnectionError as e:
                last_error = e
                logger.warning(f"Stream 网络错误 model={current_model} (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
                await asyncio.sleep(2)

            except openai_module.APIStatusError as e:
                last_error = e
                if e.status_code >= 500:
                    wait = _calc_backoff(attempt)
                    logger.warning(
                        f"Stream 服务端错误 {e.status_code} model={current_model} "
                        f"(attempt {attempt + 1}/{config.MAX_RETRIES}), 等待 {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Stream API请求错误 {e.status_code} model={current_model}: {e.message}")
                if len(model_candidates) > 1:
                    continue
                raise

            except Exception as e:
                last_error = e
                logger.error(f"Stream 错误 model={current_model} (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(_calc_backoff(attempt))
                else:
                    raise

        if attempt < config.MAX_RETRIES - 1:
            await asyncio.sleep(_calc_backoff(attempt))

    if last_error is not None:
        raise last_error
    raise Exception(f"Stream 请求失败，已重试 {config.MAX_RETRIES} 次")


# ========== 文本处理工具函数 ==========

# 场景分隔符模式：连续的省略号、星号、短横线、等号、空行等
SCENE_BREAK_PATTERNS = [
    re.compile(r'^[\s\u3000]*[…·\-=\*\u2014\u2015]{3,}[\s\u3000]*$'),   # ………… 或 *** 或 --- 或 ===
    re.compile(r'^[\s\u3000]*[\-\*\=\u2500\u2501]{3,}[\s\u3000]*$'),    # 各种分隔线
    re.compile(r'^[\s\u3000]*[☆★◆◇■□▲△●○]{3,}[\s\u3000]*$'),         # 装饰性分隔符
]

# 中文句子结束标点
_SENTENCE_ENDINGS = set('。！？…\n')
_SENTENCE_ENDING_RE = re.compile(r'[。！？…\n]')


def trim_to_sentence_boundary(text: str, max_chars: int, from_end: bool = False) -> str:
    """将文本裁剪到最近的句子边界（。！？…… 或换行符）

    Args:
        text: 原始文本
        max_chars: 最大字符数
        from_end: True=保留末尾内容, False=保留开头内容

    Returns:
        裁剪后的文本，在句子边界处截断
    """
    if len(text) <= max_chars:
        return text

    if from_end:
        # 保留最后 max_chars 个字符，然后找到第一个句子边界作为开始
        trimmed = text[-max_chars:]
        for i, ch in enumerate(trimmed):
            if ch in _SENTENCE_ENDINGS and i > 0 and i < len(trimmed) - 1:
                return trimmed[i + 1:].lstrip()
        return trimmed
    else:
        # 保留前 max_chars 个字符，然后找到最后一个句子边界作为结束
        trimmed = text[:max_chars]
        for i in range(len(trimmed) - 1, -1, -1):
            if trimmed[i] in _SENTENCE_ENDINGS:
                return trimmed[:i + 1]
        return trimmed


def split_into_paragraphs(text: str) -> List[str]:
    """将文本分割为有意义的段落列表

    对中文网文的处理逻辑：
    - 每行对话通常独占一行（单换行分隔）
    - 空行表示段落/场景分隔
    - 连续的对话行和叙述行合并为一个段落块
    - 避免产生过多的微小"段落"
    """
    lines = text.strip().split('\n')
    paragraphs = []
    current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # 空行 = 段落分隔
            if current:
                paragraphs.append('\n'.join(current))
                current = []
        else:
            current.append(stripped)

    if current:
        paragraphs.append('\n'.join(current))

    # 如果整个文本只分出了一个巨大段落，尝试按双换行再分
    if len(paragraphs) == 1 and len(paragraphs[0]) > 500:
        parts = re.split(r'\n\s*\n', text.strip())
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) > 1:
            return parts

    return paragraphs if paragraphs else [text.strip()]


def merge_paragraphs(paragraphs: List[str]) -> str:
    """将段落列表合并为文本"""
    return '\n\n'.join(paragraphs)


def _is_scene_break(paragraph: str) -> bool:
    """判断一个段落是否为场景分隔符"""
    stripped = paragraph.strip()
    # 空段落
    if not stripped:
        return True
    # 纯分隔线（很短且匹配模式）
    if len(stripped) < 30:
        for pattern in SCENE_BREAK_PATTERNS:
            if pattern.match(stripped):
                return True
    return False


def _estimate_dialogue_ratio(text: str) -> float:
    """估算文本中对话的比例（以字符数计）

    中文对话通常以 「」""'' 或引号包裹
    """
    dialogue_chars = 0
    total_chars = len(text)
    if total_chars == 0:
        return 0.0

    # 匹配各种引号内的对话
    dialogue_patterns = [
        re.compile(r'[「\u201c][^」\u201d]*[」\u201d]'),
        re.compile(r'[\u2018][^\u2019]*[\u2019]'),
        re.compile(r'"[^"]*"'),
    ]
    for pattern in dialogue_patterns:
        for match in pattern.finditer(text):
            dialogue_chars += len(match.group())

    return min(dialogue_chars / total_chars, 1.0)


def _normalize_quality(quality: Optional[str]) -> str:
    # Legacy clients may still send old quality names. The product now exposes
    # one integrated default, so all old values collapse to the same behavior.
    return "balanced"


def _strategy_instruction(quality: str = "balanced") -> str:
    return (
        "当前扩写策略：默认综合模式。\n"
        "优先忠实保留原文章节骨架、对话、事件顺序和结尾结果；"
        "只补足明确省略、隐喻替代或明显写薄的关键过程。"
        "长章节会按场景分段处理，每段必须覆盖当前分段原文的开头、推进和结尾，"
        "不能只写一小段就提前收束，也不能复制上下文或下一章内容。"
    )


_CN_NUMERAL_VALUES = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _parse_small_count(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUMERAL_VALUES:
        return _CN_NUMERAL_VALUES[raw]
    if raw.startswith("十") and len(raw) == 2:
        return 10 + _CN_NUMERAL_VALUES.get(raw[1], 0)
    if raw.endswith("十") and len(raw) == 2:
        return _CN_NUMERAL_VALUES.get(raw[0], 0) * 10
    if "十" in raw and len(raw) == 3:
        left, right = raw.split("十", 1)
        return _CN_NUMERAL_VALUES.get(left, 0) * 10 + _CN_NUMERAL_VALUES.get(right, 0)
    return 0


def _infer_required_process_count(chapter_title: str, chapter_content: str) -> int:
    """Infer explicit multi-round/process count from title/content hints such as 五连 or 五次."""
    text = f"{chapter_title}\n{chapter_content[:1200]}"
    counts: List[int] = []
    for match in re.finditer(r"([一二两三四五六七八九十\d]{1,3})\s*(?:连|次|轮|回)", text):
        value = _parse_small_count(match.group(1))
        if 2 <= value <= 20:
            counts.append(value)
    return max(counts) if counts else 0


def _build_expansion_coverage_hint(chapter_title: str, chapter_content: str) -> str:
    required_count = _infer_required_process_count(chapter_title, chapter_content)
    if required_count <= 1:
        return ""
    return (
        "=== 覆盖要求（系统自动检测）===\n"
        f"标题或正文暗示本章包含 {required_count} 次连续关键过程。"
        f"必须逐次覆盖 1/{required_count} 到 {required_count}/{required_count}，"
        "每一次都要有独立的起因、反应、推进和余韵；禁止把中间几次用概括句、列表或一两句话带过。\n"
    )


def _min_unleashed_output_chars(chapter_title: str, chapter_content: str) -> int:
    required_count = _infer_required_process_count(chapter_title, chapter_content)
    if required_count >= 5:
        return 5000
    if required_count >= 3:
        return 4000
    return 0


SUMMARY_CONTEXT_CHARS = 2600
CONTINUITY_CONTEXT_CHARS = 1400
MULTI_PROCESS_SOURCE_CHARS = 3600


def _trim_summary_context(context_summary: str) -> str:
    """Keep cross-chapter context useful but small enough to avoid distracting the model."""
    if not context_summary:
        return ""
    return trim_to_sentence_boundary(context_summary, SUMMARY_CONTEXT_CHARS, from_end=True)


def _continuity_tail(text: str) -> str:
    """Short state tail for seamless continuation without encouraging repetition."""
    if not text:
        return ""
    return trim_to_sentence_boundary(text, CONTINUITY_CONTEXT_CHARS, from_end=True)


def _segment_boundary_brief(segment_text: str) -> str:
    paragraphs = split_into_paragraphs(segment_text)
    if not paragraphs:
        return ""
    first = trim_to_sentence_boundary(paragraphs[0], 180, from_end=False)
    last = trim_to_sentence_boundary(paragraphs[-1], 220, from_end=False)
    if first == last:
        return f"分段首尾锚点：{first}"
    return f"分段开头锚点：{first}\n分段结尾锚点：{last}"


def _build_segment_context_block(
    chapter_title: str,
    segments: List[Dict[str, Any]],
    segment_summaries: List[str],
    seg_idx: int,
) -> str:
    current = segments[seg_idx]["text"]
    parts = [
        "=== 当前分段处理边界（必须遵守）===",
        f"当前是《{chapter_title}》第 {seg_idx + 1}/{len(segments)} 段。",
        "只扩写【当前分段原文】；上一段、下一段和下一章内容只用于衔接，严禁写入当前正文。",
        "输出必须覆盖当前分段原文从开头到结尾的全部剧情、对话、动作和结果，不能只写一个局部场面后突然结束。",
        _segment_boundary_brief(current),
        "=== 当前分段摘要（只用于自检，不要输出）===",
        segment_summaries[seg_idx],
    ]
    if seg_idx > 0:
        parts.extend([
            "=== 上一分段摘要（只用于状态衔接，不要复述）===",
            segment_summaries[seg_idx - 1],
        ])
    if seg_idx + 1 < len(segments):
        parts.extend([
            "=== 下一分段摘要（只用于收束位置，不要提前写）===",
            segment_summaries[seg_idx + 1],
        ])
    return "\n".join(part for part in parts if part)


def _multi_process_source_context(chapter_content: str, part_index: int, required_count: int) -> str:
    """Provide the original as a compact skeleton, not as repeated material to rewrite every time."""
    if len(chapter_content) <= MULTI_PROCESS_SOURCE_CHARS:
        return chapter_content
    if part_index == 1:
        return trim_to_sentence_boundary(chapter_content, MULTI_PROCESS_SOURCE_CHARS, from_end=False)
    if part_index == required_count:
        return trim_to_sentence_boundary(chapter_content, MULTI_PROCESS_SOURCE_CHARS, from_end=True)
    head = trim_to_sentence_boundary(chapter_content, MULTI_PROCESS_SOURCE_CHARS // 2, from_end=False)
    tail = trim_to_sentence_boundary(chapter_content, MULTI_PROCESS_SOURCE_CHARS // 2, from_end=True)
    return f"{head}\n\n[...中间按当前编号和上一段状态承接，不要复述省略内容...]\n\n{tail}"


def _multi_process_target_chars(required_count: int, part_index: int) -> tuple[int, int]:
    """Return a conservative per-request target range for multi-process expansion."""
    if required_count >= 5:
        base_min, base_max = 950, 1600
    else:
        base_min, base_max = 850, 1400
    if part_index == 1 or part_index == required_count:
        return base_min + 150, base_max + 300
    return base_min, base_max


async def _expand_multi_process_chapter(
    chapter_title: str,
    chapter_content: str,
    prev_chapter_summary: str,
    model: str,
    quality: str,
    required_count: int,
    progress_callback: Callable = None,
    next_chapter_opening: str = "",
    segment_save_callback: Callable = None,
) -> str:
    """Expand explicitly multi-round chapters through several stateful requests.

    A single Chat Completions request often returns a compact 3k-ish result even
    when asked for 5k+ words. Splitting by explicit round count forces coverage
    while preserving continuity through the generated tail of the previous part.
    """
    system_prompt = (
        _one_pass_system_prompt(quality)
        + "\n\n【多请求连续扩写规则】\n"
        + "本章会分多次请求生成。每次只处理指定编号的连续关键过程，"
        + "必须沿用上一段结尾的人物位置、衣着、情绪、体力、关系状态和叙事视角。"
        + "上一段结尾只用于状态衔接，不允许复述、改写或总结上一段。"
        + "禁止输出章节标题，禁止提前写后续编号，禁止用概括句跳过中间编号。"
    )
    strategy_instruction = _strategy_instruction(quality)
    previous_tail = ""
    generated_parts: List[str] = []
    min_total = _min_unleashed_output_chars(chapter_title, chapter_content)

    for part_idx in range(1, required_count + 1):
        target_min, target_max = _multi_process_target_chars(required_count, part_idx)
        if progress_callback:
            await progress_callback(
                0.12 + (part_idx - 1) / required_count * 0.76,
                f"正在分次扩写第{part_idx}/{required_count}段...",
            )

        if part_idx == 1:
            output_scope = (
                f"从章节开头自然写到第 {part_idx}/{required_count} 次关键过程结束。"
                "保留原文前置铺垫，只输出正文，不输出标题。"
            )
        elif part_idx == required_count:
            output_scope = (
                f"只写第 {part_idx}/{required_count} 次关键过程，并在结束后自然衔接原文结尾。"
                "原文结尾里的结果、奖励、状态变化等必须保留并写顺。"
            )
        else:
            output_scope = (
                f"只写第 {part_idx}/{required_count} 次关键过程。"
                "从上一段结尾状态直接接上，不重复前文，不写后续编号。"
            )

        context_info = ""
        if prev_chapter_summary and part_idx == 1:
            context_info += f"=== 上下文摘要（只用于人设/关系/设定，不要复述）===\n{_trim_summary_context(prev_chapter_summary)}\n"
        if previous_tail:
            context_info += (
                "=== 上一段结尾状态（只用于无缝接续，严禁复述）===\n"
                f"{previous_tail}\n"
            )
        if next_chapter_opening and part_idx == required_count:
            context_info += (
                "=== 下一章开头（仅供最终衔接，不要输出下一章内容）===\n"
                f"{next_chapter_opening}\n"
            )

        user_prompt = (
            f"{context_info}"
            "=== 原章节骨架（只作为剧情顺序和结尾锚点，不要逐句复述）===\n"
            f"{chapter_title}\n{_multi_process_source_context(chapter_content, part_idx, required_count)}\n\n"
            "=== 本次任务 ===\n"
            f"本章检测到 {required_count} 次连续关键过程。当前只处理第 {part_idx}/{required_count} 次。\n"
            f"{output_scope}\n"
            f"本段目标长度约 {target_min}-{target_max} 字；不得低于 {target_min} 字，除非原文信息确实不足。\n"
            "不要输出说明、标题、markdown、编号列表或分析。只输出可直接拼接进章节的正文片段。\n\n"
            f"{strategy_instruction}\n\n"
            "直接输出本段正文："
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        result = await chat_completion(
            messages,
            model=model,
            temperature=_select_temperature("one_pass", chapter_content, quality),
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )

        if len(result or "") < target_min:
            logger.warning(
                "多请求扩写第%s/%s段过短，重试: output=%s min=%s",
                part_idx,
                required_count,
                len(result or ""),
                target_min,
            )
            retry_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt + f"\n\n上一次本段只有 {len(result or '')} 字，必须重写并补足层次。"},
            ]
            retry_result = await chat_completion(
                retry_messages,
                model=model,
                temperature=_select_temperature("one_pass", chapter_content, quality),
                max_tokens=config.OUTPUT_RESERVED_TOKENS,
            )
            if len(retry_result or "") > len(result or ""):
                result = retry_result

        cleaned = (result or "").strip()
        generated_parts.append(cleaned)
        previous_tail = _continuity_tail(cleaned)

        if segment_save_callback:
            try:
                await segment_save_callback("\n\n".join(generated_parts), part_idx, required_count)
            except Exception as e:
                logger.warning(f"多请求扩写中间保存失败: {e}")

    final_text = "\n\n".join(part for part in generated_parts if part)
    if min_total and len(final_text) < min_total:
        logger.warning(
            "多请求扩写总长度仍低于目标: title=%s output=%s min=%s",
            chapter_title,
            len(final_text),
            min_total,
        )
    if progress_callback:
        await progress_callback(1.0, "完成")
    return final_text


def _select_temperature(task: str, text: str, quality: str = "balanced") -> float:
    """根据任务类型和文本特征选择温度。"""
    dialogue_ratio = _estimate_dialogue_ratio(text)
    if task == "analysis":
        return 0.2
    if task == "summary":
        return 0.25
    if task == "rewrite":
        return 0.92 if dialogue_ratio > 0.55 else 0.86
    return 0.88 if dialogue_ratio > 0.55 else 0.82


IMMERSIVE_INTIMACY_STYLE_GUIDE = """
【沉浸式亲密场景写法】
- 禁止“动作流水账”：不要一口气罗列多个姿势、动作或阶段；一次只推进一个明确变化，并写清它为什么发生、角色如何反应、下一步如何自然承接。
- 禁止“一句话带过”：关键过程不能用“换了几个方式”“折腾了很久”“又继续了下去”等概括句糊过去。
- 每个关键变化至少包含四类信息中的三类：人物主动/犹豫的动机、身体或呼吸的细小反应、对白或未说出口的心理、节奏停顿或余韵。
- 让变化来自人物性格和当下关系，而不是作者硬塞花样；害羞的人要有退缩和嘴硬，强势的人要有控制感，口是心非的人要有自我辩解。
- 镜头要有远近切换：先写位置和氛围，再落到手、眼神、呼吸、衣料、声音、停顿等细节，最后回到人物关系变化。
- 同一场景里避免重复同一套句式和同一类反应；每一段都必须推进情绪、关系或局面。
- 可随机选择叙事重心：羞耻感、占有欲、试探拉扯、失控边缘、事后安静、嘴硬心软、反差感、暧昧压迫感。不要每次都写成同一种模板。
- 变化维度库：每次亲密场景至少选择2-4个维度形成不同写法，不要固定套路。
  维度A：主导权变化，如一方试探、另一方反制、短暂退让、重新掌控、互相拉扯。
  维度B：空间和距离变化，如靠近、压迫、避让、被困住、被拉回、从紧绷到松动。
  维度C：节奏变化，如慢下来观察反应、突然停住逼对方表态、被情绪带动加快、事后回落。
  维度D：互动重心变化，如眼神、手、呼吸、衣料、声音、耳边低语、失控前的停顿。
  维度E：心理变化，如羞耻、嘴硬、吃醋、占有欲、自我说服、明知不该却放任。
  维度F：关系后果，如称呼变化、态度软化、边界被改写、第二天的尴尬或心照不宣。
- 写作时从这些维度组合出不同场面，不要输出“方式列表”，要把选择融进连续叙事。
- 禁止粗俗机械比喻、器械化比喻和低质网文套话；不要把人物写成没有心理和关系变化的动作执行器。
- 语言要像原作者自然写出来的小说，不要像说明书、清单、医学描述或夸张短视频文案。
"""


def _detect_scenes(paragraphs: List[str]) -> List[List[int]]:
    """将段落列表按场景分组，返回每个场景包含的段落索引列表

    场景分割依据：
    1. 显式的场景分隔符（***、---、===、空行等）
    2. 时间/地点跳转标记（"第二天"、"次日"、"回到"等开头）
    """
    if not paragraphs:
        return []

    scenes = []
    current_scene = []

    # 时间/场景跳转关键词（出现在段落开头时视为新场景）
    scene_transition_re = re.compile(
        r'^[\s\u3000]*(第二[天日]|次[日晨]|翌[日晨]|清[晨早]|傍晚|夜[晚里幕]|'
        r'几[天日个小]时[后之]|过了[一二三几半][天日个小]|'
        r'回到|来到|走进|推开门|离开[了之]后)'
    )

    for i, para in enumerate(paragraphs):
        if _is_scene_break(para):
            # 场景分隔符：结束当前场景，分隔符本身作为独立的"场景"
            if current_scene:
                scenes.append(current_scene)
                current_scene = []
            scenes.append([i])  # 分隔符独立成"场景"
        elif current_scene and scene_transition_re.match(para):
            # 时间/场景跳转：开始新场景
            if current_scene:
                scenes.append(current_scene)
            current_scene = [i]
        else:
            current_scene.append(i)

    if current_scene:
        scenes.append(current_scene)

    return scenes


def _build_segments_from_scenes(
    paragraphs: List[str],
    scenes: List[List[int]],
    segment_size: int = None,
) -> List[Dict[str, Any]]:
    """基于场景分组构建分段，确保不在场景中间切断

    Args:
        paragraphs: 段落列表
        scenes: 场景分组（每个元素是段落索引列表）
        segment_size: 分段目标大小（字符数）

    Returns:
        分段列表，每个分段包含:
        - 'para_indices': 段落索引列表
        - 'text': 分段文本
        - 'context_scene_indices': 上一个分段最后一个场景的段落索引（用于上下文参考）
    """
    if segment_size is None:
        segment_size = config.SEGMENT_SIZE

    segments = []
    current_indices = []
    current_length = 0
    last_scene_of_prev_segment = []  # 上一个分段的最后一个场景（用于上下文）

    for scene in scenes:
        scene_text = '\n\n'.join(paragraphs[i] for i in scene)
        scene_length = len(scene_text)

        # 如果单个场景就超过 2 倍分段大小，强制切分该场景
        if scene_length > segment_size * 2:
            # 先把当前积累的内容打包
            if current_indices:
                segments.append({
                    'para_indices': list(current_indices),
                    'text': '\n\n'.join(paragraphs[i] for i in current_indices),
                    'context_scene_indices': list(last_scene_of_prev_segment),
                })
                last_scene_of_prev_segment = _get_last_scene_indices(current_indices, scenes)
                current_indices = []
                current_length = 0

            # 对超大场景按段落级别切分
            sub_indices = []
            sub_length = 0
            for idx in scene:
                para_len = len(paragraphs[idx])
                if sub_length + para_len > segment_size and sub_indices:
                    segments.append({
                        'para_indices': list(sub_indices),
                        'text': '\n\n'.join(paragraphs[i] for i in sub_indices),
                        'context_scene_indices': list(last_scene_of_prev_segment),
                    })
                    last_scene_of_prev_segment = sub_indices[-3:]  # 用最后几个段落作为上下文
                    sub_indices = []
                    sub_length = 0
                sub_indices.append(idx)
                sub_length += para_len

            if sub_indices:
                current_indices = sub_indices
                current_length = sub_length
            continue

        # 判断加入当前场景后是否超过分段大小
        if current_length + scene_length > segment_size and current_indices:
            # 检查对话比例——对话密集的段落允许更大的分段（对话基本原样保留，token效率高）
            dialogue_ratio = _estimate_dialogue_ratio(
                '\n\n'.join(paragraphs[i] for i in current_indices)
            )
            effective_limit = segment_size * (1.3 if dialogue_ratio > 0.6 else 1.0)

            if current_length + scene_length > effective_limit:
                # 打包当前分段
                segments.append({
                    'para_indices': list(current_indices),
                    'text': '\n\n'.join(paragraphs[i] for i in current_indices),
                    'context_scene_indices': list(last_scene_of_prev_segment),
                })
                last_scene_of_prev_segment = _get_last_scene_indices(current_indices, scenes)
                current_indices = []
                current_length = 0

        current_indices.extend(scene)
        current_length += scene_length

    # 收尾
    if current_indices:
        segments.append({
            'para_indices': list(current_indices),
            'text': '\n\n'.join(paragraphs[i] for i in current_indices),
            'context_scene_indices': list(last_scene_of_prev_segment),
        })

    # 后处理：合并过小的段，拆分过大的段
    segments = _post_process_segments(segments, paragraphs)

    return segments


def _post_process_segments(
    segments: List[Dict[str, Any]],
    paragraphs: List[str],
) -> List[Dict[str, Any]]:
    """后处理分段：合并过小的段（< SEGMENT_MIN_SIZE），确保段大小合理"""
    if len(segments) <= 1:
        return segments

    result = []
    for seg in segments:
        seg_len = len(seg['text'])

        if result and seg_len < config.SEGMENT_MIN_SIZE:
            # 合并到前一个段
            prev = result[-1]
            prev['para_indices'].extend(seg['para_indices'])
            prev['text'] = '\n\n'.join(paragraphs[i] for i in prev['para_indices'])
            logger.debug(
                f"合并过小分段 ({seg_len}字) 到前一段 (合并后 {len(prev['text'])}字)"
            )
        elif seg_len > config.SEGMENT_MAX_SIZE and len(seg['para_indices']) > 1:
            # 超大段：尝试从中间拆分
            indices = seg['para_indices']
            mid = len(indices) // 2
            first_half = indices[:mid]
            second_half = indices[mid:]

            result.append({
                'para_indices': first_half,
                'text': '\n\n'.join(paragraphs[i] for i in first_half),
                'context_scene_indices': seg.get('context_scene_indices', []),
            })
            result.append({
                'para_indices': second_half,
                'text': '\n\n'.join(paragraphs[i] for i in second_half),
                'context_scene_indices': first_half[-3:],
            })
            logger.debug(
                f"拆分过大分段 ({seg_len}字) 为两段 "
                f"({len(result[-2]['text'])}字 + {len(result[-1]['text'])}字)"
            )
        else:
            result.append(seg)

    return result


def _get_last_scene_indices(para_indices: List[int], scenes: List[List[int]]) -> List[int]:
    """获取给定段落索引中最后一个完整场景的段落索引（用于上下文参考）"""
    if not para_indices:
        return []

    last_idx = para_indices[-1]
    for scene in reversed(scenes):
        if last_idx in scene:
            return scene
    # 回退：返回最后几个段落
    return para_indices[-3:]


def _extract_character_names(text: str) -> List[str]:
    """从文本中提取可能的人物名称

    中文网文常见的命名模式：
    - 对话前的名称标记："XXX说/道/问/笑"
    - 两字或三字中文名
    """
    names = set()

    # 模式1: "XXX说/道/问/笑/叫/喊/嘟囔/嗤笑/轻笑..."
    speech_patterns = re.findall(
        r'([\u4e00-\u9fff]{2,3}?)\s*(?:说道|笑道|问道|喊道|叫道|冷哼道|嗤笑道|'
        r'笑着说|轻声道|低声道|怒道|急道|叹道|惊呼道|哼道|'
        r'说|道|问|喃喃|嘟囔|冷哼|嗤笑|轻笑|惊呼)',
        text
    )
    # 常见的虚词/代词/量词，不可能是人名或人名开头
    _NON_NAME_WORDS = {
        '他们', '她们', '我们', '大家', '众人', '所有', '一个', '那个', '这个', '别人',
        '有人', '某人', '对方', '自己', '彼此', '两人', '三人', '几个',
    }
    _NON_NAME_PREFIXES = '的了着和跟在被把又也都还却又已正让叫被'
    for name in speech_patterns:
        if name in _NON_NAME_WORDS:
            continue
        # 去掉开头的虚词（如"的刘刚" -> "刘刚"）
        while name and name[0] in _NON_NAME_PREFIXES and len(name) > 2:
            name = name[1:]
        if len(name) >= 2:
            names.add(name)

    # 模式2: 被称呼的名字 "叫/唤/喊 + 名字"
    called_patterns = re.findall(r'(?:叫|唤|喊)\s*([\u4e00-\u9fff]{2,4})', text)
    for name in called_patterns:
        names.add(name)

    return list(names)[:10]  # 最多返回10个人名


# ========== 提示词定义 ==========

ANALYSIS_SYSTEM_PROMPT = """你是中文小说删减痕迹分析器，只做一件事：找出明显被省略、屏蔽、隐喻替代的段落。

判定为“需要扩写”的典型信号：
1. 连续省略号或 ***、×××、□□□ 直接遮蔽内容。
2. “事毕”“许久后”“不知过了多久”等把过程整段跳过。
3. 用雨露、花开、发烫之类意象只暗示过程，没有写出实际经过。
4. 【整场景扩展隐喻系统】整个场景用一套日常活动作为隐喻来替代性行为全过程。识别特征：
   - 整个场景的"日常活动"在字面上不合理，但角色反应强烈（推搡、害羞、妥协、喘息）
   - 多个物品/动作形成一致的隐喻映射，例如：
     "看月亮"→看胸部，"月亮好大好圆"→形容乳房，"窗帘/吊带绳"→内衣/胸罩，"拉窗帘"→脱内衣
     "月亮表面不规则凸起"→乳头，"纠正不规则"→揉捏乳头
     "爬玻璃上看"→趴在窗前被从身后进入，"四只脚"→两人交合体位暗示
     "咳嗽/呸"→口交/深喉后反应，"吹风感冒"→掩饰喉咙不适
     "晨练/俯卧撑/仰卧起坐"→各种性交体位，"跟着节奏/骑我一头/掌握节奏"→女上位骑乘，"双腿发软/缺氧/速度加快"→插得太快太深、高潮反应
     "洒了咖啡/水/茶"→射精/喷出体液，"印到了里面"→内射，"衣服跑没了/变脏/打扫卫生"→脱光/弄脏身体清理身体
     又如："吃棒棒糖"→口交，"骑马"→骑乘位，"弹钢琴"→手指插入
   - 旁观者的"误解"（如丈夫在楼下浑然不知）进一步确认隐喻
5. 场景刚开始就突兀跳到事后状态。
6. 【日常借口掩饰型】用日常理由解释明显是性行为后遗症的身体反应，例如：
   - "感冒/咳嗽/嗓子疼"实际是口交后喉咙不适
   - "腿软/走路不稳/腰酸"实际是性爱后身体反应
   - "吹风着凉"实际是衣衫不整/身体暴露后的掩饰
   - "运动过度/锻炼"实际是激烈性爱
   判断方法：看前后文是否有亲密暗示、暧昧场景、两人独处，再结合身体症状是否不合常理。

不要误判：
1. 正常时间推进。
2. 已经写完整的互动。
3. 与亲密内容无关的战斗、省略或转场。
4. 角色确实因为正常原因生病（需结合前后文判断，没有亲密暗示时不要硬往性方面解读）。

【重要】当检测到整场景扩展隐喻（信号4）时：
- metaphor_mapping 必须列出完整的隐喻→真实内容对照表
- description 必须推断出实际的性行为顺序（例如：脱衣→抚摸乳房→插入→口交→口爆）
- 要根据隐喻出现的先后顺序来推断性行为的实际进程
- 场景末尾的"日常借口"（如咳嗽=口交后反应）可以反推之前发生了什么

只输出 JSON。"""

ANALYSIS_USER_PROMPT = """请分析以下小说章节，精确识别所有被编辑删减的段落区域。

返回格式（纯JSON，无markdown）：
{{
  "has_implicit_content": true/false,
  "characters": ["角色名1", "角色名2"],
  "sections": [
    {{
      "start_para": 段落起始索引(从0开始),
      "end_para": 段落结束索引(包含),
      "description": "简述该段被删减前可能包含的内容",
      "type": "省略号替代/符号屏蔽/跳跃叙述/意象隐喻/突兀跳转/物品替代隐喻/日常借口掩饰/整场景扩展隐喻",
      "metaphor_mapping": "说明隐喻/借口与真实内容的对应关系，例如: '体温计'→'肉棒', '感冒咳嗽'→'口交后喉咙不适', '腿软'→'性爱后反应'",
      "characters_involved": ["涉及的角色名"],
      "intensity": "轻微/明显/大段"
    }}
  ]
}}

如果没有被删减内容，返回 {{"has_implicit_content": false, "characters": [], "sections": []}}

=== 章节内容（段落以序号标注）===
{numbered_paragraphs}"""

_ONE_PASS_SYSTEM_BASE = """你是中文小说删减内容还原器。

硬规则：
1. 优先补原文明显被省略、屏蔽、隐喻替代的部分。
2. 对已经写出但明显写薄、写快、只写结果没写过程的亲密段落，允许在不改变剧情走向的前提下增密重写，让过程更细、更顺、更有层次。
3. 如果没有明确删减痕迹，也没有明显写薄的关键段落，必须原样返回，不能无中生有。
4. 还原内容必须和前后文、下一章开头自然衔接，保证剧情连贯。
5. 不新增原文没有暗示的人物、设定、冲突、地点或时间跳跃。
6. 识别“日常借口掩饰”型删减：如果角色用感冒、腿软、腰酸、衣衫不整等日常理由掩盖亲密场景后的反应，要还原被掩饰的情绪、关系变化和身体状态，但不要写成动作说明书。
7. 【整场景扩展隐喻还原】如果原文用“晨练/俯卧撑/仰卧起坐”“看月亮”“洒咖啡”等日常活动作为隐喻掩饰亲密过程，不要保留表面的掩饰动作；应结合上下文还原成连贯的亲密场景，重点写人物拉扯、节奏变化、细小反应和事后余韵。

【反幻觉——场景锁定】（极其重要）
- 严格遵守原文的场景环境：如果原文场景在室内（卧室/浴室/客厅/办公室等），所有描写必须发生在该室内空间，禁止出现室外环境描写（窗外风景、街道、天空等），除非原文明确提到角色看向窗外。
- 如果原文场景在室外，同理不能突然跳到室内。
- 时间必须与原文一致：原文是白天就是白天，晚上就是晚上，不要自行变更。
- 不要编造原文中不存在的环境元素（家具、装饰、天气、声音等），只能使用原文已提及或合理推断的环境细节。

【风格锚定——与原文保持一致】（极其重要）
- 叙事人称锁定：原文用第一人称就用第一人称，用第三人称就用第三人称，绝不切换。
- 叙事视角锁定：原文以谁的视角叙述，扩写就必须保持同一视角，不能突然跳到其他角色的内心。
- 用词风格匹配：仔细观察原文的语言风格——是口语化还是书面化，是现代白话还是古风文言，是冷硬还是细腻——扩写部分必须与原文风格浑然一体，不能产生风格割裂。
- 句式节奏匹配：原文用短句就多用短句，原文用长句就多用长句。不要让扩写部分突然变成另一种句式节奏。
- 关键用语保留：原文中角色的专属称呼（绰号、昵称、特殊称谓）、口头禅、特色用词必须原样保留，不要替换成同义词。
- 情绪用词一致：原文用”恼”就用”恼”，不要替换成”愤怒”；原文用”腻歪”就用”腻歪”，不要替换成”亲昵”。保持原作者的用词偏好。

【角色驱动描写】
- 描写必须根据角色的性格特征来写：冷淡的角色不会突然变得热情洋溢，害羞的角色不会突然口无遮拦。
- 角色的情感状态必须与上下文一致：紧张就是紧张，放松就是放松，不要凭空切换情绪。
- 角色的身体状态要连贯：如果角色刚经历过激烈活动，后续应有对应的身体反应（喘息、疲惫、酸软等）。
- 对白风格必须匹配角色人设：每个角色的说话方式、用词习惯、语气应保持一致。

【结构保持——与原文对齐】
- 原文的段落结构是骨架，扩写是在骨架上”增肉”，不是拆掉骨架重建。
- 原文中未被删减的段落应尽可能保留原句，只在需要衔接扩写内容的位置做必要的过渡调整。
- 原文的剧情事件顺序不可打乱：事件A发生在事件B之前，扩写后也必须保持这个顺序。
- 原文的对话内容应原样保留，除非对话本身就是隐喻需要转化。扩写时只能在对话之间补充动作、神态、心理描写。
- 原文各段落的篇幅比例应大致保持：如果原文战斗场景占30%、亲密场景占70%，扩写后这个比例不应严重失衡。

【去重规则】
- 禁止在同一次扩写中重复描写相同的动作、感受或场景。
- 如果一个动作（如亲吻、抚摸）已经写过，后续不要再用不同措辞重写同一个动作，而是应该推进到下一个阶段。
- 每个段落的描写应推进情节或深化情感，不能原地打转。

【上下文衔接——段间连续性】
- 如果提供了”前文”上下文，你的输出开头必须与前文末尾自然衔接，不能重复前文最后写过的内容，也不能跳过中间情节。
- 如果提供了”后文/下一章开头”，你的输出结尾必须能自然过渡到后文，不能在结尾凭空新增后文中没有的事件。
- 分段扩写时，前一段的结尾状态（人物位置、穿着、情绪、动作状态）就是你的起始状态，不能遗忘或矛盾。

还原时优先保持：
1. 原文视角、句式、语气、人物关系。
2. 场景内服装、道具、动作顺序、情绪走向。
3. 节奏层次和事后余韵。
4. 把”接触-试探-深入-反应-停顿-再推进-余韵”写完整，避免只剩动作骨架。
5. 对已写出的动作，不要只换同义词，要补足触感、呼吸、细小反应、心理波动、姿势变化和承接句。

{immersive_style_guide}

少量示例：
原文：”她咬着唇，后面的事便只剩一串省略号……再抬眼时，天已经亮了。”
正确：补全过程，并让”天已经亮了”成为自然结果。
错误：新增别的人物、突然换场景、直接整章重写成全新内容。

原文已经写到两人亲密关系推进，但中间只有动作骨架、缺少细节与层次。
正确：保留同一场景和剧情结果，把试探、停顿、身体发软、节奏变化、人物嘴硬心乱、事后余韵写得更具体。
错误：把场景改到别处，或者让人物突然做出原文没有铺垫的决定。

只输出完整章节正文。"""

def _one_pass_system_prompt(quality: str = "balanced") -> str:
    return _prompt_value("one_pass_system_base").replace("{immersive_style_guide}", "").strip()

ONE_PASS_USER_PROMPT = """请处理以下章节：
1. 被省略、符号替代、一笔带过、隐喻替代的段落→还原完整
2. 写薄/只有动作骨架的亲密段落→在不改剧情结果的前提下增密重写
3. 没有以上两类内容→原样返回，不做任何修改

所有写作规则以 system 提示词为准。不要输出解释、分析、标题或 markdown。
{context_info}

{strategy_instruction}

=== 当前章节：{chapter_title} ===
{chapter_content}

{next_chapter_info}

直接输出还原后的完整章节内容："""

_SECTION_EXPAND_SYSTEM_BASE = """你要只处理一个目标片段中的删减痕迹或明显写薄的关键过程。

规则：
1. 没有明确删减痕迹，且片段本身也不显得单薄，就原样返回。
2. 如果片段已有动作骨架但过程太薄，可以增密重写，但剧情结果、人物关系、场景位置必须不变。
3. 只改目标片段，不输出前后文。
4. 与前文末尾、后文开头严密衔接，不重复已发生内容。
5. 不新增原文未暗示的剧情。
6. 识别"日常借口掩饰"：感冒、腿软、腰酸、吹风着凉、衣衫不整等如果在上下文中明显是在遮掩亲密场景后果，应还原被掩饰的情绪、关系变化和身体状态。
7. 【整场景扩展隐喻还原】如果原文用"晨练/俯卧撑"、"看月亮"、"洒咖啡"等日常活动作掩饰，不要保留表面的借口动作；应结合上下文还原成连贯的亲密场景，重点写人物拉扯、节奏变化、细小反应和事后余韵。

【反幻觉——场景锁定】（极其重要）
- 严格遵守原文场景：室内就是室内，室外就是室外。不要编造不属于当前场景的环境元素。
- 如果前后文的场景都在卧室/浴室/客厅等室内空间，你的扩写也必须发生在同一空间内，禁止出现室外描写。
- 不要凭空添加原文没有提到的家具、装饰、天气、光线等环境细节。只使用原文已有或可合理推断的元素。

【风格锚定——与原文保持一致】（极其重要）
- 叙事人称和视角必须与原文一致，不可切换。
- 观察原文的语言特征（口语化/书面化、句式长短、用词偏好），扩写必须完全匹配。
- 角色的专属称呼、口头禅、特色用词必须原样保留。
- 原文用"恼"就用"恼"，不要替换成"愤怒"；原文用什么词就沿用什么词。

【结构保持——与原文对齐】
- 原文中未被删减的句子尽量保留原句，扩写是"增肉"不是"重写"。
- 原文对话原样保留，只在对话间补充动作、神态、心理描写。
- 原文的事件顺序不可打乱。

【角色驱动描写】
- 根据角色性格来写：保持每个角色一贯的说话风格、反应方式、行为模式。
- 角色情感状态必须与上下文连贯，不能突然跳变。
- 身体状态要有连续性（如刚经历激烈活动后应有喘息、酸软等后续反应）。

【去重规则】
- 前文已经展开写过的动作/过程，绝对不要用不同措辞再重写一遍。
- 你的内容应该是在前文基础上推进，而不是换个说法重复已有内容。

【上下文衔接——段间连续性】
- 仔细阅读前文末尾：人物此刻的位置、姿势、穿着、情绪状态是什么？你的输出必须从这个状态开始，不能遗忘或矛盾。
- 仔细阅读后文开头：你的输出结尾必须能自然过渡到后文，不能留下逻辑断裂。
- 如果前文已经写了"他把她抱上床"，你就不能再让角色还在客厅。

如果片段存在隐喻替代，要结合上下文解码，但仍保持原场景结构。

{immersive_style_guide}

{character_info}"""

def _section_expand_system_prompt(quality: str = "balanced", character_info: str = "") -> str:
    return (
        _SECTION_EXPAND_SYSTEM_BASE
        .replace("{immersive_style_guide}", "")
        .replace("{character_info}", character_info)
    )

SECTION_EXPAND_USER_PROMPT = """请处理【需要还原的片段】：
- 如果有删减痕迹，就补完整。
- 如果已写出但明显写薄，就在不改剧情结果的前提下增密重写。
- 如果两者都没有，就原样返回。

⚠️场景锁定：仔细阅读前后文的场景环境（地点、时间、室内/室外），你的扩写必须发生在完全相同的场景中，不得编造任何原文中不存在的环境描写。
⚠️角色一致：根据角色性格、当前情绪、身体状态来写，不要脱离人设。
⚠️禁止重复：不要重写前文已经详细描写过的内容，从前文结束处的进度继续推进。
⚠️风格一致：你的扩写在语言风格（人称、句式、用词）上必须与前后文完全一致，不能有风格割裂。原文对话原样保留，只在对话间补充动作/神态/心理。
⚠️状态接续：阅读前文末尾，确认人物当前的位置、姿势、穿着、情绪——你的输出必须从这个状态自然开始。
⚠️识别隐喻伪装：不要被表面的"晨练"、"俯卧撑"、"看月亮"、"洒咖啡"迷惑。如果片段是这种隐喻掩饰，应还原亲密场景中的人物关系、节奏变化和后续状态，绝不能继续描写表面借口。

=== 前文（仅供参考，不要输出） ===
{context_before}

=== 【需要还原的片段】 ===
{section_content}

=== 后文（仅供参考，不要输出） ===
{context_after}

{additional_instruction}

只输出还原后的片段内容："""

REWRITE_SYSTEM_PROMPT = """你是专业的中文网络小说改写师。

【改写原则】
1. 严格遵循用户的改写指示，保持叙事连贯性（人称、时态、风格一致）
2. 如果原文存在物品替代、日常活动替代或省略跳过，要结合上下文还原真实情境，但不能写成动作说明书
3. 保持原文人物关系、情绪走向和语言风格，不新增无铺垫的设定或场景

【默认改写方向】将隐晦/省略的内容展开为完整描写，覆盖：
- 节奏推进：试探、停顿、靠近、推进、收束和余韵
- 感官细节：呼吸、温度、距离、触感、声音和身体状态
- 对白和心理：嘴硬、犹豫、自我说服、失控边缘、关系确认
- 叙事承接：每个关键变化都要有起因、反应、停顿和后果

【篇幅目标】亲密段落展开到2000-4000字，普通段落展开到1000-2000字。
{immersive_style_guide}

【严禁】省略号跳过、概括句代替、动作清单、说明书式描写、markdown标记。
直接输出改写后的内容。"""

REWRITE_USER_PROMPT = """改写指示：{instruction}

=== 前文（仅供参考，不要输出） ===
{context_before}

=== 需要改写的段落 ===
{paragraph_content}

=== 后文（仅供参考，不要输出） ===
{context_after}

直接输出改写后的内容："""

SUMMARY_SYSTEM_PROMPT = """你是一位专业的小说内容分析师。请为以下章节生成结构化摘要，供后续章节扩写时作为上下文参考。

摘要必须包含以下要素：
1.【主要人物】：本章出场的角色及其当前情绪状态、彼此关系变化
2.【情节发展】：本章的关键事件，按时间顺序列出（必须明确指出事件的因果关系）
3.【场景环境】：主要场景的时间、地点描述（具体到室内/室外、房间、时段）
4.【亲密场景】：如果本章包含亲密/性描写场景，概括场景的进展程度和双方状态（重要：如果亲密场景在章节末尾未完结，必须明确说明"未完结"及当前进行到的具体阶段）
5.【情绪基调】：本章整体和结尾的情绪基调
6.【结尾状态】：章节结束时每个角色的物理状态（位置、穿着）、情绪状态、以及任何未解决的悬念或正在进行的动作——这一条极其重要，因为下一章需要从这个状态自然接续
7.【关键用语】：原文中的特色用词、角色专属称呼、口头禅（如有）

用300-500字概括。只输出摘要，不要其他内容。"""

QUICK_CHECK_SYSTEM_PROMPT = """你是一个文本分析助手。判断以下小说章节是否包含被编辑删减、省略或符号替代的内容。只回答 JSON。"""

QUICK_CHECK_USER_PROMPT = """请判断以下章节是否有被删减/省略的内容。

回答格式（纯JSON）：
{{"needs_expansion": true/false, "reason": "简要理由"}}

章节内容（前2000字）：
{chapter_excerpt}"""

DEFAULT_REWRITE_INSTRUCTION = (
    "将隐晦、省略、一笔带过的内容展开为沉浸式描写。"
    "不要写成动作清单或一句话概括；补足动机、停顿、呼吸、感官细节、对白、心理递进和事后余韵。"
    "每个关键变化都要有起因、反应、承接和后果，保持原文风格和人物关系。"
)


def _prompt_definitions() -> Dict[str, Dict[str, Any]]:
    return {
        "quick_check_system": {
            "group": "analysis",
            "group_label": "分析检测",
            "label": "快速检测 System",
            "description": "规则无法判断且省请求模式关闭时，用于快速判断章节是否需要扩写。",
            "default": QUICK_CHECK_SYSTEM_PROMPT,
            "rows": 4,
        },
        "quick_check_user": {
            "group": "analysis",
            "group_label": "分析检测",
            "label": "快速检测 User",
            "description": "可用变量：{chapter_excerpt}",
            "default": QUICK_CHECK_USER_PROMPT,
            "rows": 8,
        },
        "analysis_system": {
            "group": "analysis",
            "group_label": "分析检测",
            "label": "精细分析 System",
            "description": "识别章节内省略、薄写、隐喻替代区域时使用。",
            "default": ANALYSIS_SYSTEM_PROMPT,
            "rows": 16,
            "hidden": True,
        },
        "analysis_user": {
            "group": "analysis",
            "group_label": "分析检测",
            "label": "精细分析 User",
            "description": "可用变量：{numbered_paragraphs}",
            "default": ANALYSIS_USER_PROMPT,
            "rows": 14,
            "hidden": True,
        },
        "one_pass_system_base": {
            "group": "one_pass",
            "group_label": "一次扩写",
            "label": "一次扩写 System 基础",
            "description": "一次扩写和场景分段扩写的主系统提示词。",
            "default": _ONE_PASS_SYSTEM_BASE,
            "rows": 18,
        },
        "one_pass_user": {
            "group": "one_pass",
            "group_label": "一次扩写",
            "label": "一次扩写 User",
            "description": "可用变量：{context_info} {strategy_instruction} {chapter_title} {chapter_content} {next_chapter_info}",
            "default": ONE_PASS_USER_PROMPT,
            "rows": 16,
        },
        "rewrite_system": {
            "group": "rewrite",
            "group_label": "指令重写",
            "label": "指令重写 System",
            "description": "整章指令重写时使用。",
            "default": REWRITE_SYSTEM_PROMPT,
            "rows": 12,
        },
        "rewrite_user": {
            "group": "rewrite",
            "group_label": "指令重写",
            "label": "指令重写 User",
            "description": "可用变量：{instruction} {context_before} {paragraph_content} {context_after}",
            "default": REWRITE_USER_PROMPT,
            "rows": 10,
        },
        "default_rewrite_instruction": {
            "group": "rewrite",
            "group_label": "指令重写",
            "label": "默认整章重写指令",
            "description": "用户没有填写指令时使用。",
            "default": DEFAULT_REWRITE_INSTRUCTION,
            "rows": 5,
        },
        "summary_system": {
            "group": "summary",
            "group_label": "摘要上下文",
            "label": "章节摘要 System",
            "description": "为后续章节扩写生成上下文摘要时使用。",
            "default": SUMMARY_SYSTEM_PROMPT,
            "rows": 12,
        },
    }


def _prompt(key: str, default: str) -> str:
    return prompt_store.get_text(key, default)


def _prompt_value(key: str) -> str:
    definition = _prompt_definitions()[key]
    return _prompt(key, str(definition.get("default", "")))


def _render_prompt(template: str, **values: Any) -> str:
    try:
        return template.format(**values)
    except Exception as e:
        logger.warning("Prompt template format failed, using simple replacement fallback: %s", e)
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered


def get_prompt_settings() -> Dict[str, Any]:
    return prompt_store.get_all(_prompt_definitions())


def update_prompt_settings(changes: Dict[str, Any]) -> Dict[str, str]:
    return prompt_store.update(_prompt_definitions(), changes)


def reset_prompt_settings(keys: Optional[List[str]] = None) -> Dict[str, str]:
    return prompt_store.reset(_prompt_definitions(), keys)


def get_default_rewrite_instruction() -> str:
    return _prompt_value("default_rewrite_instruction")


def build_local_chapter_summary(chapter_title: str, chapter_content: str) -> str:
    """本地快速摘要，避免单 token 模式下每章额外消耗一次模型请求。
    增强版：更完整地捕获人物状态、场景和结尾状态。"""
    paragraphs = split_into_paragraphs(chapter_content)
    names = _extract_character_names(chapter_content)

    # 取开头、1/3处、2/3处、结尾段落 —— 更全面覆盖
    head = paragraphs[0] if paragraphs else ""
    tail = paragraphs[-1] if len(paragraphs) > 1 else ""
    # 取更多中间段落用于捕获情节发展
    third_hint = ""
    two_third_hint = ""
    if len(paragraphs) > 4:
        third_hint = paragraphs[len(paragraphs) // 3]
        two_third_hint = paragraphs[2 * len(paragraphs) // 3]
    elif len(paragraphs) > 2:
        third_hint = paragraphs[len(paragraphs) // 2]

    parts = [f"章节：{chapter_title}"]
    if names:
        parts.append(f"人物：{'、'.join(names[:8])}")
    if head:
        parts.append(f"开头：{trim_to_sentence_boundary(head, 150, from_end=False)}")
    if third_hint:
        parts.append(f"前段：{trim_to_sentence_boundary(third_hint, 120, from_end=False)}")
    if two_third_hint:
        parts.append(f"后段：{trim_to_sentence_boundary(two_third_hint, 120, from_end=False)}")
    if tail and tail != head:
        # 结尾给更多空间 —— 结尾状态对下一章衔接至关重要
        parts.append(f"结尾：{trim_to_sentence_boundary(tail, 200, from_end=False)}")

    # 粗略场景检测
    scene_keywords = []
    for p in paragraphs[:10]:
        for kw in ["卧室", "客厅", "浴室", "办公室", "教室", "车上", "酒店", "餐厅", "公园", "街道"]:
            if kw in p and kw not in scene_keywords:
                scene_keywords.append(kw)
    if scene_keywords:
        parts.append(f"场景：{'、'.join(scene_keywords[:4])}")

    return "\n".join(parts)[:800]


# ========== 快速扩写判断 ==========

async def quick_check_needs_expansion(
    chapter_content: str,
    model: str = None,
    force_model: bool = False,
) -> dict:
    """快速判断章节是否包含被删减内容，避免对无需扩写的章节进行无意义的处理。

    Returns:
        {"needs_expansion": bool, "reason": str}
    """
    check_mode = getattr(config, "EXPANSION_CHECK_MODE", "romance_or_omission")
    if check_mode == "always":
        return {"needs_expansion": True, "reason": "扩写检测模式：always"}

    # 先做规则检测：如果文本中有明显的省略标记，直接返回 True
    paragraphs = split_into_paragraphs(chapter_content)
    heuristic_sections = _heuristic_implicit_sections(paragraphs)
    if heuristic_sections:
        return {"needs_expansion": True, "reason": "检测到明确省略/删减标记"}

    romance_count = _romance_signal_count(chapter_content)
    if check_mode == "romance_or_omission":
        # Pure romance words are noisy in long web-novel chapters. A low threshold
        # caused normal relationship scenes to be expanded from scratch, which
        # increases hallucination and plot loss. Keep this path conservative.
        if romance_count >= 5:
            return {"needs_expansion": True, "reason": f"检测到暧昧/亲密信号 {romance_count} 处"}
        return {"needs_expansion": False, "reason": "未检测到明确省略或暧昧/亲密信号"}

    if check_mode == "omission_only":
        return {"needs_expansion": False, "reason": "未检测到明确省略/删减标记"}

    if config.CONSERVE_REQUESTS and not force_model:
        return {"needs_expansion": False, "reason": "省请求模式：未检测到明确省略标记"}

    # 如果没有明显标记，用 AI 快速判断
    messages = [
        {
            "role": "system",
            "content": _prompt_value("quick_check_system"),
        },
        {
            "role": "user",
            "content": _render_prompt(
                _prompt_value("quick_check_user"),
                chapter_excerpt=chapter_content[:2000],
            ),
        },
    ]

    try:
        response = await chat_completion(messages, model=model, temperature=_select_temperature("analysis", chapter_content[:2000]))
        json_match = re.search(r'\{[\s\S]*?\}', response)
        if json_match:
            result = json.loads(json_match.group())
            logger.info(f"快速检测结果: needs_expansion={result.get('needs_expansion')}, reason={result.get('reason', '')}")
            return result
    except Exception as e:
        logger.warning(f"快速检测失败: {e}")

    heuristic_sections = _heuristic_implicit_sections(split_into_paragraphs(chapter_content))
    if heuristic_sections:
        return {"needs_expansion": True, "reason": "规则检测到明显省略痕迹"}

    # 检测失败时保守处理：默认不扩写，避免无中生有
    return {"needs_expansion": False, "reason": "检测失败且未发现明确省略痕迹"}


# ========== 核心分析逻辑 ==========

async def analyze_chapter(
    chapter_content: str,
    model: str = None,
    force_model: bool = False,
) -> Dict:
    """分析章节，识别隐含性描写段落

    返回包含以下字段的字典：
    - has_implicit_content: 是否有隐含内容
    - characters: 涉及的角色名
    - sections: 需要扩写的段落区域列表
    """
    if model is None:
        model = config.DEFAULT_MODEL

    paragraphs = split_into_paragraphs(chapter_content)

    if config.CONSERVE_REQUESTS and not force_model:
        heuristic_sections = _heuristic_implicit_sections(paragraphs)
        return {
            "has_implicit_content": bool(heuristic_sections),
            "characters": _extract_character_names(chapter_content),
            "sections": heuristic_sections,
        }

    # 构建带编号的段落文本
    numbered = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))

    # 动态计算可用字符数
    max_chars = config.get_max_content_chars(model)
    if len(numbered) > max_chars:
        numbered = trim_to_sentence_boundary(numbered, max_chars, from_end=False)
        numbered += "\n\n[...后续段落省略...]"

    messages = [
        {"role": "system", "content": _prompt_value("analysis_system")},
        {"role": "user", "content": _render_prompt(
            _prompt_value("analysis_user"),
            numbered_paragraphs=numbered,
        )},
    ]

    # 尝试使用 response_format（某些兼容 API 可能不支持），失败则走统一模型轮换入口。
    try:
        response_text = None
        for current_model in config.get_model_candidates(model):
            try:
                await _rate_limit_wait()
                response = await client.chat.completions.create(
                    model=current_model,
                    messages=messages,
                    temperature=_select_temperature("analysis", chapter_content),
                    stream=False,
                    response_format={"type": "json_object"},
                )
                response_text = response.choices[0].message.content
                break
            except Exception as e:
                logger.debug(f"analysis response_format 失败 model={current_model}: {e}")
        if response_text is None:
            raise RuntimeError("analysis response_format failed for all models")
    except Exception as e:
        # 如果 response_format 不被支持，回退到普通请求
        logger.debug(f"response_format 不支持，回退到普通请求: {e}")
        response_text = await chat_completion(messages, model=model, temperature=_select_temperature("analysis", chapter_content))

    # 解析JSON响应
    try:
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            result = json.loads(json_match.group())
            logger.info(
                f"章节分析完成: has_implicit={result.get('has_implicit_content')}, "
                f"sections={len(result.get('sections', []))}, "
                f"characters={result.get('characters', [])}"
            )
            return result
    except json.JSONDecodeError as e:
        logger.warning(f"JSON解析失败: {e}")

    heuristic_sections = _heuristic_implicit_sections(paragraphs)
    if heuristic_sections:
        logger.warning("分析结果JSON解析失败，回退到启发式省略段检测")
        return {
            "has_implicit_content": True,
            "characters": _extract_character_names(chapter_content),
            "sections": heuristic_sections,
        }

    logger.warning("分析结果JSON解析失败，且未发现明确省略痕迹，保守跳过扩写")
    return {
        "has_implicit_content": False,
        "characters": _extract_character_names(chapter_content),
        "sections": [],
    }


# ========== 一次性扩写 ==========

async def expand_chapter_one_pass(
    chapter_title: str,
    chapter_content: str,
    prev_chapter_summary: str = "",
    model: str = None,
    quality: str = "balanced",
    progress_callback: Callable = None,
    next_chapter_opening: str = "",
    skip_if_no_content: bool = True,
    segment_save_callback: Callable = None,
) -> str:
    """一次性扩写整个章节（适合短-中等长度章节）

    对于超出单次请求上下文的长章节，自动启用场景感知分段处理。

    Args:
        chapter_title: 章节标题
        chapter_content: 章节内容
        prev_chapter_summary: 前一章摘要
        model: 模型名称
        progress_callback: 进度回调 async (progress, message) -> None
        next_chapter_opening: 下一章开头文本
        skip_if_no_content: 是否先检测章节需不需要扩写（默认True）
        segment_save_callback: 分段保存回调 async (text, seg_done, seg_total) -> None
    """
    if model is None:
        model = config.DEFAULT_MODEL

    # 如果启用了跳过检测，先快速判断是否需要扩写
    if skip_if_no_content:
        if progress_callback:
            await progress_callback(0.05, "正在检测是否需要扩写...")
        check = await quick_check_needs_expansion(chapter_content, model=model)
        if not check.get("needs_expansion", True):
            logger.info(f"章节无需扩写: {check.get('reason', '')}")
            if progress_callback:
                await progress_callback(1.0, f"无需扩写: {check.get('reason', '')}")
            return chapter_content  # 返回原文

    context_info = ""
    if prev_chapter_summary:
        context_info = f"=== 上下文摘要（只用于人设/关系/设定，不要复述）===\n{_trim_summary_context(prev_chapter_summary)}\n"
    coverage_hint = _build_expansion_coverage_hint(chapter_title, chapter_content)
    if coverage_hint:
        context_info += coverage_hint
    strategy_instruction = _strategy_instruction(quality)

    next_chapter_info = ""
    if next_chapter_opening:
        next_chapter_info = (
            f"=== 下一章开头（仅供参考衔接，不要输出下一章内容）===\n"
            f"{next_chapter_opening}\n"
            f"⚠️注意：你的还原内容结尾必须能自然衔接到上面的下一章开头。"
            f"不要在章末凭空添加下一章才出现的情节。"
        )

    # 动态计算可用空间
    max_chars = config.get_max_content_chars(model)
    # 估算实际 prompt 开销（系统提示词 + 用户模板 + 策略说明 + 上下文信息）
    prompt_overhead = 1500
    available_for_content = max_chars - prompt_overhead

    # 默认综合模式的 one-pass 上限：超过则强制分段，防止输出过长导致质量下降
    one_pass_limit = config.ONE_PASS_MAX_CHARS
    effective_limit = min(available_for_content, one_pass_limit)
    required_count = _infer_required_process_count(chapter_title, chapter_content)

    if len(chapter_content) <= effective_limit:
        # 章节够短，一次处理
        messages = [
            {"role": "system", "content": _one_pass_system_prompt(quality)},
            {"role": "user", "content": _render_prompt(
                _prompt_value("one_pass_user"),
                strategy_instruction=strategy_instruction,
                context_info=context_info,
                chapter_title=chapter_title,
                chapter_content=chapter_content,
                next_chapter_info=next_chapter_info,
            )},
        ]

        if progress_callback:
            await progress_callback(0.3, "正在扩写...")

        temperature = _select_temperature("one_pass", chapter_content, quality)
        result = await chat_completion(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )

        min_chars = 0
        if min_chars and len(result or "") < min_chars:
            logger.warning(
                "一次扩写输出过短，准备重试: title=%s output=%s min=%s",
                chapter_title,
                len(result or ""),
                min_chars,
            )
            if progress_callback:
                await progress_callback(0.72, f"输出偏短（{len(result or '')}/{min_chars}字），正在重写补足...")
            retry_context = (
                context_info
                + "=== 重写要求 ===\n"
                + f"上一次输出只有 {len(result or '')} 字，低于本章最低目标 {min_chars} 字。"
                + "这次必须重写完整章节，逐次覆盖所有连续关键过程，不能只续写、不能摘要化、不能输出标题或说明。\n"
            )
            retry_messages = [
                {"role": "system", "content": _one_pass_system_prompt(quality)},
                {"role": "user", "content": _render_prompt(
                    _prompt_value("one_pass_user"),
                    strategy_instruction=strategy_instruction,
                    context_info=retry_context,
                    chapter_title=chapter_title,
                    chapter_content=chapter_content,
                    next_chapter_info=next_chapter_info,
                )},
            ]
            retry_result = await chat_completion(
                retry_messages,
                model=model,
                temperature=temperature,
                max_tokens=config.OUTPUT_RESERVED_TOKENS,
            )
            if len(retry_result or "") > len(result or ""):
                result = retry_result

        result = await _retry_with_integrity_guard(
            messages=messages,
            source_text=chapter_content,
            output_text=result,
            model=model,
            temperature=temperature,
            forbidden_context=next_chapter_opening,
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )

        if progress_callback:
            await progress_callback(1.0, "完成")

        return result
    else:
        # 长章节，启用场景感知分段处理
        return await _expand_long_chapter(
            chapter_title, chapter_content, prev_chapter_summary, model,
            quality,
            progress_callback, next_chapter_opening=next_chapter_opening,
            segment_save_callback=segment_save_callback,
        )


async def _expand_long_chapter(
    chapter_title: str,
    chapter_content: str,
    prev_chapter_summary: str,
    model: str,
    quality: str = "balanced",
    progress_callback: Callable = None,
    next_chapter_opening: str = "",
    segment_save_callback: Callable = None,
) -> str:
    """基于场景感知的分段扩写长章节

    改进点：
    1. 按场景边界分段，不在场景中间切断
    2. 使用前一分段的最后一个场景作为上下文参考（而非重叠内容）
    3. 跟踪每个分段对应的确切段落索引，合并时不产生重复
    4. 支持分段中间保存回调
    5. 动态计算分段大小
    """
    paragraphs = split_into_paragraphs(chapter_content)
    strategy_instruction = _strategy_instruction(quality)

    # 第一步：检测场景边界
    scenes = _detect_scenes(paragraphs)
    logger.info(f"长章节分段: {len(paragraphs)}个段落, {len(scenes)}个场景")

    # 动态计算可用空间来决定分段大小
    max_chars = config.get_max_content_chars(model)
    # 默认综合模式的分段目标大小
    default_segment_size = config.DEFAULT_SEGMENT_SIZE
    # 每个分段预留上下文和 prompt 空间
    prompt_overhead = 1500
    dynamic_segment_size = min(
        default_segment_size,
        max_chars - config.CONTEXT_BEFORE_CHARS - config.CONTEXT_AFTER_CHARS - prompt_overhead,
    )
    dynamic_segment_size = max(dynamic_segment_size, config.SEGMENT_MIN_SIZE)

    # 第二步：基于场景构建分段
    segments = _build_segments_from_scenes(paragraphs, scenes, segment_size=dynamic_segment_size)
    logger.info(f"构建了 {len(segments)} 个分段 (目标大小: {dynamic_segment_size}字)")
    segment_summaries = [
        build_local_chapter_summary(
            f"{chapter_title} 第{idx + 1}/{len(segments)}段",
            segment["text"],
        )
        for idx, segment in enumerate(segments)
    ]

    if not segments:
        logger.warning("分段构建结果为空，回退到整章处理")
        nci = ""
        if next_chapter_opening:
            nci = (
                f"=== 下一章开头（仅供参考衔接，不要输出下一章内容）===\n"
                f"{next_chapter_opening}\n"
                f"⚠️注意：你的还原内容结尾必须能自然衔接到上面的下一章开头。"
            )
        messages = [
            {"role": "system", "content": _one_pass_system_prompt(quality)},
            {"role": "user", "content": _render_prompt(
                _prompt_value("one_pass_user"),
                strategy_instruction=strategy_instruction,
                context_info="",
                chapter_title=chapter_title,
                chapter_content=chapter_content[:max_chars],
                next_chapter_info=nci,
            )},
        ]
        fallback_temperature = _select_temperature("one_pass", chapter_content, quality)
        fallback_result = await chat_completion(
            messages,
            model=model,
            temperature=fallback_temperature,
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )
        return await _retry_with_integrity_guard(
            messages=messages,
            source_text=chapter_content,
            output_text=fallback_result,
            model=model,
            temperature=fallback_temperature,
            forbidden_context=next_chapter_opening,
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )

    # 第三步：逐分段扩写
    expanded_by_segment = []

    for seg_idx, segment in enumerate(segments):
        seg_text = segment['text']
        context_scene_indices = segment['context_scene_indices']

        # 构建上下文信息
        context_info = _build_segment_context_block(
            chapter_title=chapter_title,
            segments=segments,
            segment_summaries=segment_summaries,
            seg_idx=seg_idx,
        ) + "\n"
        if seg_idx == 0 and prev_chapter_summary:
            context_info += f"=== 上下文摘要（只用于人设/关系/设定，不要复述）===\n{_trim_summary_context(prev_chapter_summary)}\n"
        if seg_idx == 0:
            context_info += _build_expansion_coverage_hint(chapter_title, chapter_content)
        elif seg_idx > 0:
            # 优先使用上一分段的扩写结果作为上下文（而非原文），保证连续性
            if expanded_by_segment:
                prev_result = expanded_by_segment[-1]
                context_tail = _continuity_tail(prev_result)
                context_info += f"=== 前文状态（前一段扩写结果末尾，只用于接续，严禁复述）===\n{context_tail}\n"
            elif context_scene_indices:
                # 回退：如果没有扩写结果可用，使用原文场景段落
                context_text = '\n\n'.join(paragraphs[i] for i in context_scene_indices)
                context_text = trim_to_sentence_boundary(
                    context_text, CONTINUITY_CONTEXT_CHARS, from_end=True
                )
                context_info += f"=== 前文状态（已处理部分末尾，只用于接续，严禁复述）===\n{context_text}\n"

        # 如果是最后一个分段，注入下一章开头上下文
        next_info = ""
        if seg_idx == len(segments) - 1 and next_chapter_opening:
            next_info = (
                f"=== 下一章开头（仅供参考衔接，不要输出下一章内容）===\n"
                f"{next_chapter_opening}\n"
                f"⚠️注意：你的还原内容结尾必须能自然衔接到上面的下一章开头。"
            )

        messages = [
            {"role": "system", "content": _one_pass_system_prompt(quality)},
            {"role": "user", "content": _render_prompt(
                _prompt_value("one_pass_user"),
                strategy_instruction=strategy_instruction,
                context_info=context_info,
                chapter_title=f"{chapter_title}（第{seg_idx + 1}/{len(segments)}段）",
                chapter_content=seg_text,
                next_chapter_info=next_info,
            )},
        ]

        if progress_callback:
            progress = (seg_idx + 0.3) / len(segments)
            await progress_callback(progress, f"正在扩写第{seg_idx + 1}/{len(segments)}段...")

        temperature = _select_temperature("one_pass", seg_text, quality)
        result = await chat_completion(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )
        result = await _retry_with_integrity_guard(
            messages=messages,
            source_text=seg_text,
            output_text=result,
            model=model,
            temperature=temperature,
            forbidden_context=next_chapter_opening if seg_idx == len(segments) - 1 else "",
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )
        expanded_by_segment.append(result)

        # 每段完成后保存中间结果
        if segment_save_callback:
            intermediate_text = '\n\n'.join(expanded_by_segment)
            try:
                await segment_save_callback(intermediate_text, seg_idx + 1, len(segments))
            except Exception as e:
                logger.warning(f"分段保存回调失败: {e}")

        if progress_callback:
            progress = (seg_idx + 1) / len(segments)
            await progress_callback(progress, f"第{seg_idx + 1}段完成")

    # 第四步：合并分段结果（每个分段对应不重叠的段落，直接拼接即可）
    final_text = '\n\n'.join(expanded_by_segment)
    final_text, _ = _strip_forbidden_context_leak(final_text, next_chapter_opening)
    return final_text


# ========== 两阶段精细扩写 ==========

async def expand_chapter_detailed(
    chapter_title: str,
    chapter_content: str,
    prev_chapter_summary: str = "",
    model: str = None,
    quality: str = "balanced",
    progress_callback: Callable = None,
    next_chapter_opening: str = "",
    skip_if_no_content: bool = True,
    segment_save_callback: Callable = None,
) -> str:
    """两阶段扩写：先分析识别隐含内容位置，再针对性扩写（更精准但消耗更多配额）

    Args:
        chapter_title: 章节标题
        chapter_content: 章节内容
        prev_chapter_summary: 前一章摘要
        model: 模型名称
        progress_callback: 进度回调
        next_chapter_opening: 下一章开头文本
        skip_if_no_content: 是否先检测章节需不需要扩写
        segment_save_callback: 分段保存回调
    """
    if model is None:
        model = config.DEFAULT_MODEL
    quality = _normalize_quality(quality)

    # 如果启用了跳过检测，先快速判断是否需要扩写
    if skip_if_no_content:
        if progress_callback:
            await progress_callback(0.03, "正在检测是否需要扩写...")
        check = await quick_check_needs_expansion(chapter_content, model=model, force_model=True)
        if not check.get("needs_expansion", True):
            logger.info(f"章节无需扩写: {check.get('reason', '')}")
            if progress_callback:
                await progress_callback(1.0, f"无需扩写: {check.get('reason', '')}")
            return chapter_content

    if progress_callback:
        await progress_callback(0.05, "正在分析章节内容...")

    # 第一阶段：分析章节，识别隐含性描写段落
    analysis = await analyze_chapter(chapter_content, model=model, force_model=True)

    if not analysis.get("has_implicit_content", False):
        if progress_callback:
            await progress_callback(1.0, "未检测到需要扩写的内容")
        return chapter_content

    # 第二阶段：逐区域扩写
    paragraphs = split_into_paragraphs(chapter_content)
    sections = _normalize_analysis_sections(analysis.get("sections", []), len(paragraphs))
    characters = analysis.get("characters", [])

    if not sections:
        if progress_callback:
            await progress_callback(1.0, "未检测到需要扩写的内容")
        return chapter_content

    logger.info(f"精细扩写: 检测到 {len(sections)} 处需要扩写, 涉及角色: {characters}")

    # 构建角色信息提示
    character_info = ""
    if characters:
        character_info = f"【本章涉及角色】：{'、'.join(characters)}"

    # 标记需要扩写的段落（使用原始段落列表的副本）
    expanded_paragraphs = list(paragraphs)

    for sec_idx, section in enumerate(sections):
        start = section.get("start_para", 0)
        end = section.get("end_para", start)
        start = max(0, min(start, len(expanded_paragraphs) - 1))
        end = max(start, min(end, len(expanded_paragraphs) - 1))

        # 获取需要扩写的段落文本
        section_content = "\n\n".join(expanded_paragraphs[start:end + 1])

        # 获取前文上下文——使用已扩写的段落内容，确保后续段落能衔接前段扩写结果
        context_before_parts = expanded_paragraphs[:start]
        context_before = "\n\n".join(context_before_parts)
        if context_before:
            context_before = trim_to_sentence_boundary(
                context_before, min(config.CONTEXT_BEFORE_CHARS, 2200), from_end=True
            )

        # 获取后文上下文（使用句子边界裁剪）
        context_after_parts = expanded_paragraphs[end + 1:min(len(expanded_paragraphs), end + 6)]
        context_after = "\n\n".join(context_after_parts)
        if context_after:
            context_after = trim_to_sentence_boundary(
                context_after, config.CONTEXT_AFTER_CHARS, from_end=False
            )

        # 构建附加指令
        additional_parts = []
        desc = section.get('description', '')
        sec_type = section.get('type', '')
        intensity = section.get('intensity', '')
        involved = section.get('characters_involved', [])
        metaphor = section.get('metaphor_mapping', '')

        if desc:
            additional_parts.append(f"分析提示：{desc}")
        additional_parts.append(_strategy_instruction(quality))
        if sec_type:
            additional_parts.append(f"省略类型：{sec_type}")
        if intensity:
            additional_parts.append(f"省略程度：{intensity}")
        if involved:
            additional_parts.append(f"涉及角色：{'、'.join(involved)}")
        if metaphor:
            additional_parts.append(f"隐喻映射：{metaphor}")

        # 如果不是第一段扩写，明确提示AI接续前文
        if sec_idx > 0:
            additional_parts.append(
                "⚠️重要：前文已经过扩写还原，你的还原内容必须自然接续前文末尾的情节和动作，"
                "不要重复前文已经写过的内容（如接吻、前戏等已写过的部分不要再写），"
                "直接从前文结束处的情节继续往下推进。"
            )

        additional = '\n'.join(additional_parts) if additional_parts else ""

        # 构建扩写请求
        messages = [
            {"role": "system", "content": _section_expand_system_prompt(
                quality=quality, character_info=character_info,
            )},
            {"role": "user", "content": _render_prompt(
                SECTION_EXPAND_USER_PROMPT,
                context_before=context_before if context_before else "（章节开头）",
                section_content=section_content,
                context_after=context_after if context_after else (
                    (
                        f"（章节结尾）\n\n=== 下一章开头（仅供衔接参考）===\n"
                        f"{next_chapter_opening}\n"
                        f"⚠️注意：你的还原内容结尾必须能自然衔接到下一章开头。"
                    )
                    if (sec_idx == len(sections) - 1 and next_chapter_opening) else "（章节结尾）"
                ),
                additional_instruction=additional,
            )},
        ]

        if progress_callback:
            progress = 0.15 + 0.85 * (sec_idx + 0.3) / len(sections)
            await progress_callback(progress, f"正在扩写第{sec_idx + 1}/{len(sections)}处...")

        expanded_section = await chat_completion(
            messages,
            model=model,
            temperature=_select_temperature("detailed", section_content, quality),
            max_tokens=config.OUTPUT_RESERVED_TOKENS,
        )

        # 将扩写结果替换回段落列表
        expanded_section_paras = split_into_paragraphs(expanded_section)
        expanded_paragraphs[start:end + 1] = expanded_section_paras

        # 由于替换后段落数量可能变化，需要调整后续 section 的索引
        offset = len(expanded_section_paras) - (end - start + 1)
        if offset != 0:
            for future_sec in sections[sec_idx + 1:]:
                future_sec["start_para"] = future_sec.get("start_para", 0) + offset
                future_sec["end_para"] = future_sec.get("end_para", 0) + offset

        # 每处扩写完成后保存中间结果
        if segment_save_callback:
            intermediate_text = merge_paragraphs(expanded_paragraphs)
            try:
                await segment_save_callback(intermediate_text, sec_idx + 1, len(sections))
            except Exception as e:
                logger.warning(f"分段保存回调失败: {e}")

        if progress_callback:
            progress = 0.15 + 0.85 * (sec_idx + 1) / len(sections)
            await progress_callback(progress, f"第{sec_idx + 1}处扩写完成")

        logger.info(
            f"第{sec_idx + 1}处扩写完成: "
            f"原{end - start + 1}段 -> {len(expanded_section_paras)}段, "
            f"偏移量={offset}"
        )

    final_text = merge_paragraphs(expanded_paragraphs)
    final_text, leaked = _strip_forbidden_context_leak(final_text, next_chapter_opening)
    issues = _source_coverage_issues(chapter_content, final_text)
    if leaked or issues:
        issue_text = "；".join((["输出结尾复制了参考上下文"] if leaked else []) + issues)
        raise ExpansionIntegrityError(f"精细扩写完整性校验失败：{issue_text}")
    return final_text


# ========== 段落重写（流式） ==========

async def stream_rewrite_paragraph(
    paragraph_content: str,
    context_before: str,
    context_after: str,
    instruction: str = "",
    model: str = None,
):
    """流式重写段落

    上下文使用句子边界裁剪，确保不在句子中间截断。
    """
    if not instruction:
        instruction = (
            "将本段隐晦/省略/一笔带过的内容展开为完整详细描写。"
            "要求：不要写成动作清单；补足动机、停顿、呼吸、感官细节、对白、心理递进和事后余韵。"
            "每个关键变化都要有起因、反应、承接和后果。"
            "亲密段落展开到2000-4000字，普通段落展开到1000-2000字。"
        )

    # 对上下文进行句子边界裁剪
    trimmed_before = ""
    if context_before:
        trimmed_before = trim_to_sentence_boundary(
            context_before, min(config.CONTEXT_BEFORE_CHARS, 2200), from_end=True
        )

    trimmed_after = ""
    if context_after:
        trimmed_after = trim_to_sentence_boundary(
            context_after, config.CONTEXT_AFTER_CHARS, from_end=False
        )

    messages = [
        {"role": "system", "content": _prompt_value("rewrite_system").replace("{immersive_style_guide}", "").strip()},
        {"role": "user", "content": _render_prompt(
            _prompt_value("rewrite_user"),
            instruction=instruction,
            context_before=trimmed_before if trimmed_before else "（文章开头）",
            paragraph_content=paragraph_content,
            context_after=trimmed_after if trimmed_after else "（文章结尾）",
        )},
    ]

    async for chunk in stream_completion(messages, model=model, temperature=_select_temperature("rewrite", paragraph_content)):
        yield chunk


async def stream_expand_single_paragraph(
    paragraph_content: str,
    instruction: str,
    model: str = None,
):
    """Expand only one selected paragraph, without sending chapter context."""
    instruction = (instruction or "").strip()
    if not instruction:
        instruction = "根据当前段落内容补充细节，扩写这一段。"

    messages = [
        {"role": "system", "content": _prompt_value("rewrite_system").replace("{immersive_style_guide}", "").strip()},
        {
            "role": "user",
            "content": (
                "只改写下面这一段，不要引用、续写或补全章节其他段落。\n"
                "用户补充描述：{instruction}\n\n"
                "=== 目标段落 ===\n"
                "{paragraph_content}\n\n"
                "直接输出改写后的单段内容："
            ).format(
                instruction=instruction,
                paragraph_content=paragraph_content,
            ),
        },
    ]

    async for chunk in stream_completion(messages, model=model, temperature=_select_temperature("rewrite", paragraph_content)):
        yield chunk


# ========== 章节摘要生成 ==========

async def generate_chapter_summary(chapter_content: str, model: str = None) -> str:
    """生成增强的章节摘要（用于给后续章节提供上下文）

    摘要包含：
    - 主要角色及其当前情绪状态
    - 关键情节发展
    - 场景/环境描述
    - 亲密场景的进展状态（特别重要：如果章节末尾有未完结的亲密场景）

    对过长内容：取开头 + 中间关键段 + 结尾（结尾更重要，因为要衔接下一章）
    """
    if model is None:
        model = config.DEFAULT_MODEL

    max_chars = config.get_max_content_chars(model)
    trimmed_content = chapter_content

    if len(chapter_content) > max_chars:
        # 三段采样：开头 + 中间 + 结尾
        head_size = max_chars // 4
        mid_size = max_chars // 4
        tail_size = max_chars // 2  # 结尾更重要

        head = trim_to_sentence_boundary(chapter_content, head_size, from_end=False)

        # 中间部分：取全文中间位置
        mid_start = len(chapter_content) // 2 - mid_size // 2
        mid_end = mid_start + mid_size
        mid_raw = chapter_content[max(0, mid_start):mid_end]
        mid = trim_to_sentence_boundary(mid_raw, mid_size, from_end=False)

        tail = trim_to_sentence_boundary(chapter_content, tail_size, from_end=True)

        trimmed_content = (
            head
            + "\n\n[...前半部分省略...]\n\n"
            + mid
            + "\n\n[...后半部分省略...]\n\n"
            + tail
        )

    messages = [
        {"role": "system", "content": _prompt_value("summary_system")},
        {"role": "user", "content": trimmed_content},
    ]

    summary = await chat_completion(messages, model=model, temperature=_select_temperature("summary", trimmed_content))
    logger.info(f"章节摘要生成完成，长度: {len(summary)}字")
    return summary
