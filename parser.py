import re
import os
import logging
from typing import List, Dict, Optional

import chardet

logger = logging.getLogger(__name__)

# 章节标题正则模式（按优先级排序）
CHAPTER_PATTERNS = [
    # 中文数字章节：第一章、第二十三章 等
    re.compile(
        r'^[\s　]*(第[一二三四五六七八九十百千零〇]+章[\s　：:]*.*?)[\s　]*$',
        re.MULTILINE,
    ),
    # 阿拉伯数字章节：第1章、第123章 等
    re.compile(
        r'^[\s　]*(第\d+章[\s　：:]*.*?)[\s　]*$',
        re.MULTILINE,
    ),
    # 英文 Chapter
    re.compile(
        r'^[\s　]*(Chapter\s+\d+[\s.:：]*.*?)[\s　]*$',
        re.MULTILINE | re.IGNORECASE,
    ),
    # 中文卷标题
    re.compile(
        r'^[\s　]*(卷[一二三四五六七八九十]+[\s　：:]*.*?)[\s　]*$',
        re.MULTILINE,
    ),
    # 纯数字开头：1. 或 1、
    re.compile(
        r'^[\s　]*(\d+[.、][\s　]*.*?)[\s　]*$',
        re.MULTILINE,
    ),
]

# 自动分段大小
AUTO_SEGMENT_SIZE = 3000


def detect_encoding(raw_bytes: bytes) -> str:
    """使用 chardet 检测编码"""
    result = chardet.detect(raw_bytes)
    encoding = result.get("encoding", "utf-8")
    confidence = result.get("confidence", 0)
    logger.info(f"Detected encoding: {encoding} (confidence: {confidence})")

    if encoding is None:
        return "utf-8"

    # 规范化编码名
    encoding = encoding.lower()
    # chardet 有时返回 gb2312 但实际内容需要 gbk 才能解码
    if encoding in ("gb2312", "gbk", "gb18030"):
        return "gb18030"  # gb18030 是 gbk/gb2312 的超集

    return encoding


def read_file(filepath: str) -> str:
    """读取文件，自动检测编码"""
    with open(filepath, "rb") as f:
        raw = f.read()

    encoding = detect_encoding(raw)

    # 尝试检测到的编码
    for enc in [encoding, "utf-8", "gb18030", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # 最后手段
    return raw.decode("utf-8", errors="replace")


def decode_bytes(raw: bytes) -> str:
    """解码字节，自动检测编码"""
    encoding = detect_encoding(raw)

    for enc in [encoding, "utf-8", "gb18030", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


def clean_text(text: str) -> str:
    """清理文本：去除多余空白行，但保留段落分隔"""
    # 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 去除每行首尾空白（保留缩进风格的全角空格）
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        cleaned_lines.append(stripped)
    text = "\n".join(cleaned_lines)
    # 将连续3个以上空行压缩为2个
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    # 去除开头和末尾的空白
    text = text.strip()
    return text


def extract_title_from_filename(filename: str) -> str:
    """从文件名提取标题"""
    if not filename:
        return "未命名小说"
    # 去掉扩展名
    name = os.path.splitext(filename)[0]
    # 去掉常见前缀/后缀
    name = name.strip()
    return name if name else "未命名小说"


def find_chapters(text: str) -> List[Dict[str, str]]:
    """
    识别章节，返回 [{"title": str, "content": str, "start": int}]
    """
    # 尝试每个模式
    for pattern in CHAPTER_PATTERNS:
        matches = list(pattern.finditer(text))
        if len(matches) >= 2:
            # 找到足够多的章节标题，使用此模式
            logger.info(f"Found {len(matches)} chapters using pattern: {pattern.pattern[:50]}...")
            return _build_chapters_from_matches(text, matches)

    # 没找到章节，返回空列表
    return []


def _build_chapters_from_matches(
    text: str, matches: List[re.Match]
) -> List[Dict[str, str]]:
    """根据正则匹配结果构建章节列表"""
    chapters = []

    # 检查第一个章节标题前是否有内容
    first_match_start = matches[0].start()
    pre_content = text[:first_match_start].strip()
    if pre_content and len(pre_content) > 50:
        chapters.append({
            "title": "序章",
            "content": pre_content,
        })

    # 构建每个章节
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        content_start = match.end()

        # 内容到下一个章节标题之前，或到文件末尾
        if i + 1 < len(matches):
            content_end = matches[i + 1].start()
        else:
            content_end = len(text)

        content = text[content_start:content_end].strip()

        # 跳过空章节
        if not content:
            continue

        chapters.append({
            "title": title,
            "content": content,
        })

    return chapters


def auto_segment(text: str) -> List[Dict[str, str]]:
    """将没有章节标记的文本按字数自动分段"""
    segments = []
    # 按段落分割
    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    current_content = []
    current_length = 0
    seg_index = 1

    for para in paragraphs:
        current_content.append(para)
        current_length += len(para)

        if current_length >= AUTO_SEGMENT_SIZE:
            segments.append({
                "title": f"第{seg_index}节",
                "content": "\n\n".join(current_content),
            })
            current_content = []
            current_length = 0
            seg_index += 1

    # 剩余内容
    if current_content:
        # 如果只有一个很短的段，合并到上一个
        remaining_text = "\n\n".join(current_content)
        if segments and current_length < AUTO_SEGMENT_SIZE // 3:
            segments[-1]["content"] += "\n\n" + remaining_text
        else:
            segments.append({
                "title": f"第{seg_index}节",
                "content": remaining_text,
            })

    return segments


def parse_novel(text: str, filename: str = "") -> Dict:
    """
    解析小说文本，返回结构化数据

    返回:
        {
            "title": str,
            "chapters": [{"title": str, "content": str}]
        }
    """
    # 清理文本
    text = clean_text(text)

    if not text:
        return {
            "title": extract_title_from_filename(filename),
            "chapters": [],
        }

    # 提取标题
    title = extract_title_from_filename(filename)

    # 尝试识别章节
    chapters = find_chapters(text)

    # 如果没有识别到章节，自动分段
    if not chapters:
        logger.info("No chapters found, auto-segmenting by word count")
        chapters = auto_segment(text)

    # 确保每个章节都有内容
    chapters = [ch for ch in chapters if ch.get("content", "").strip()]

    return {
        "title": title,
        "chapters": chapters,
    }


def parse_novel_from_bytes(raw: bytes, filename: str = "") -> Dict:
    """从字节数据解析小说"""
    text = decode_bytes(raw)
    return parse_novel(text, filename)
