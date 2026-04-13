"""DeepSeek API 客户端 —— 异步流式 + 层级感知的结构化 JSON 输出模式。

对论文 Markdown 文本执行"原子化切分 + 中文小标题 + 动态层级"处理，
返回带有 type / level 标记的 JSON 数组，供 docx_generator 渲染。

支持：
  - AsyncOpenAI 异步调用（配合 asyncio.gather 并发）
  - 流式传输（stream=True）以应对 DeepSeek 高流量 keep-alive 场景
  - 基于 HierarchyProfile 的动态 prompt 注入
  - 自适应并发信号回传
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from openai import (
    AsyncOpenAI,
    APIError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

import config

if TYPE_CHECKING:
    from hierarchy_analyzer import HierarchyProfile

logger = logging.getLogger(__name__)

# ── System Prompt 基础模板 ────────────────────────────────

_BASE_SYSTEM_PROMPT = """\
你是一个专业的学术论文解析与结构重建专家。
请对用户输入的论文 Markdown 文本进行"原子化切分 + 中文小标题标签"处理，并识别其逻辑层级。

你需要输出一个 JSON 数组，每个元素代表文档中的一个排版块。
数据结构定义：
[
  {{
    "type": "heading",
    "level": 2,
    "text": "Abstract 或 1. Introduction 等章节标题"
  }},
  {{
    "type": "annotation",
    "level": 3,
    "cn_subtitle": "【中文小标题】",
    "en_text": "英文原子句原句。"
  }}
]

字段说明：
- type: "title" 表示论文主标题, "heading" 表示章节/子章节标题, "annotation" 表示原子化标注。
- level: 根据原论文的章节嵌套关系动态推断（1=论文标题, 2=一级章节, 3=二级, 4=三级...），不要固定死。
- annotation 的 cn_subtitle 必须用【】包裹，en_text 保留英文原句。

{hierarchy_section}

严格要求：
1. 将正文段落拆分为独立的原子句（通常按原句拆分）。
2. 为每个原子句提炼一个高度概括其核心意思的中文小标题。
3. 只输出 JSON 数组，不要输出任何解释性文字、markdown 标记或代码块包裹。
4. 确保 JSON 格式合法，可被直接 json.loads() 解析。"""

_DEFAULT_HIERARCHY_SECTION = """\
层级使用规则：
- level=1 仅用于论文主标题
- level=2 用于一级章节标题（如 Abstract, Introduction, Methods 等）
- level>=3 用于 annotation 的中文小标题和英文原句"""

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=config.DEEPSEEK_KEEPALIVE_TIMEOUT,
        )
    return _client


def build_system_prompt(profile: HierarchyProfile | None = None) -> str:
    """根据 HierarchyProfile 动态构建 system prompt。"""
    if profile is not None:
        hierarchy_section = profile.describe_for_prompt()
    else:
        hierarchy_section = _DEFAULT_HIERARCHY_SECTION
    return _BASE_SYSTEM_PROMPT.format(hierarchy_section=hierarchy_section)


# ── JSON 提取与容错 ──────────────────────────────────────

def _extract_json_array(text: str) -> list[dict] | None:
    """从 DeepSeek 响应文本中提取 JSON 数组。

    依次尝试：直接解析 → 提取 ```json 代码块 → 正则匹配 [...] 片段。
    """
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _fallback_parse_plaintext(text: str, base_level: int) -> list[dict]:
    """纯文本 fallback：按旧格式（【标题】\\n英文句）逐行解析，转为 JSON blocks。"""
    blocks: list[dict] = []
    label_re = re.compile(r"^【(.+?)】\s*$")
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        m = label_re.match(line)
        if m:
            cn = f"【{m.group(1)}】"
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            en = lines[i].strip() if i < len(lines) else ""
            blocks.append({
                "type": "annotation",
                "level": base_level + 1,
                "cn_subtitle": cn,
                "en_text": en,
            })
            if en:
                i += 1
        else:
            blocks.append({
                "type": "annotation",
                "level": base_level + 1,
                "cn_subtitle": "",
                "en_text": line,
            })
            i += 1
    return blocks


def _make_placeholder_block(heading_level: int, section_heading: str = "") -> dict:
    """生成一个占位符 block，标记该章节自动解析失败。"""
    hint = f"（章节: {section_heading}）" if section_heading else ""
    return {
        "type": "annotation",
        "level": max(3, heading_level + 1),
        "cn_subtitle": f"【⚠ 此章节自动解析失败，需人工处理{hint}】",
        "en_text": "",
    }


def _validate_blocks(
    blocks: list[dict],
    profile: HierarchyProfile | None = None,
) -> list[dict]:
    """对 JSON blocks 做 schema 校验，基于 profile 约束 level 范围。"""
    min_anno_level = profile.annotation_base_level if profile else 3
    max_level = profile.max_heading_level if profile else 6

    valid = []
    for b in blocks:
        if not isinstance(b, dict) or "type" not in b:
            logger.warning("跳过无效 block（缺少 type 字段）: %s", b)
            continue
        b.setdefault("level", min_anno_level)
        if b["type"] == "annotation":
            b["level"] = max(min_anno_level, min(b["level"], max_level))
            b.setdefault("cn_subtitle", "")
            b.setdefault("en_text", "")
        else:
            b["level"] = max(1, min(b["level"], max_level))
            b.setdefault("text", "")
        valid.append(b)
    return valid


# ── 流式收集 + keep-alive 感知 ────────────────────────────

async def _stream_collect(response) -> tuple[str, dict]:
    """从流式响应中收集完整文本，同时统计 keep-alive 和 token 信息。

    返回: (完整文本, 统计字典)
    """
    collected: list[str] = []
    stats = {
        "keepalive_count": 0,
        "first_token_time": None,
        "total_chunks": 0,
        "start_time": time.monotonic(),
    }

    async for chunk in response:
        stats["total_chunks"] += 1
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            if stats["first_token_time"] is None:
                stats["first_token_time"] = time.monotonic()
            collected.append(delta)
        else:
            stats["keepalive_count"] += 1

    stats["elapsed"] = time.monotonic() - stats["start_time"]
    if stats["first_token_time"]:
        stats["first_token_latency"] = stats["first_token_time"] - stats["start_time"]
    else:
        stats["first_token_latency"] = stats["elapsed"]

    return "".join(collected), stats


# ── 核心异步 API 调用 ────────────────────────────────────

async def process_chunk_json_async(
    text: str,
    section_heading: str = "",
    heading_level: int = 2,
    profile: HierarchyProfile | None = None,
) -> tuple[list[dict], dict]:
    """异步流式调用 DeepSeek，返回 (结构化 JSON blocks, 统计信息)。

    参数:
        text: 待处理的 Markdown 文本
        section_heading: 当前章节标题
        heading_level: 当前章节标题的层级
        profile: 从模板学习到的层级结构
    """
    if profile:
        min_annotation_level = max(profile.annotation_base_level, heading_level + 1)
        max_level = profile.max_heading_level
    else:
        min_annotation_level = max(3, heading_level + 1)
        max_level = 6

    next_level = min(min_annotation_level + 1, max_level)

    user_msg = (
        f"当前章节标题: \"{section_heading}\" (层级 level={heading_level})\n"
        f"请对以下内容进行原子化切分，annotation 的 level 应 >= {min_annotation_level}（最大不超过 {max_level}）。\n"
        f"对于概括性标注使用 level={min_annotation_level}，对于展开说明/步骤/子分类使用 level={next_level}。\n"
        f"注意：章节标题已由上游程序处理，你的输出中不要包含当前章节标题的 heading block，只输出 annotation blocks。\n\n"
        f"{text}"
    )

    system_prompt = build_system_prompt(profile)
    client = _get_client()
    last_error: Exception | None = None
    last_stats: dict = {}

    for attempt in range(1, config.DEEPSEEK_MAX_RETRIES + 1):
        try:
            logger.info(
                "调用 DeepSeek API（第 %d 次）章节: %s …",
                attempt, section_heading[:40],
            )

            if config.DEEPSEEK_STREAM_ENABLED:
                response = await client.chat.completions.create(
                    model=config.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.2,
                    stream=True,
                )
                raw, stats = await _stream_collect(response)
                last_stats = stats
                logger.info(
                    "DeepSeek 返回 %d 字符 (流式 %d chunks, %.1fs, keep-alive %d)",
                    len(raw), stats["total_chunks"],
                    stats["elapsed"], stats["keepalive_count"],
                )
            else:
                t0 = time.monotonic()
                response = await client.chat.completions.create(
                    model=config.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.2,
                )
                elapsed = time.monotonic() - t0
                raw = response.choices[0].message.content.strip()
                last_stats = {"elapsed": elapsed, "keepalive_count": 0}
                logger.info("DeepSeek 返回 %d 字符 (%.1fs)", len(raw), elapsed)

            blocks = _extract_json_array(raw)
            if blocks is not None:
                validated = _validate_blocks(blocks, profile)
                logger.info("成功解析 %d 个 JSON blocks", len(validated))
                return validated, last_stats

            logger.warning("JSON 解析失败，插入占位符 block")
            return [_make_placeholder_block(heading_level, section_heading)], last_stats

        except (APIError, APITimeoutError, RateLimitError) as exc:
            if isinstance(exc, APIStatusError) and exc.status_code == 400:
                logger.error("DeepSeek API 400 错误（不可重试）: %s", exc)
                return [_make_placeholder_block(heading_level, section_heading)], last_stats

            last_error = exc
            delay = config.DEEPSEEK_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "DeepSeek API 调用失败 (%s)，%d 秒后重试… (%d/%d)",
                exc, delay, attempt, config.DEEPSEEK_MAX_RETRIES,
            )
            await asyncio.sleep(delay)

    logger.error(
        "DeepSeek API 在 %d 次重试后仍然失败: %s",
        config.DEEPSEEK_MAX_RETRIES, last_error,
    )
    return [_make_placeholder_block(heading_level, section_heading)], last_stats


# ── 同步兼容包装 ─────────────────────────────────────────

def process_chunk_json(
    text: str,
    section_heading: str = "",
    heading_level: int = 2,
    profile: HierarchyProfile | None = None,
) -> list[dict]:
    """同步包装：兼容旧的 ThreadPoolExecutor 调用方式。"""
    blocks, _ = asyncio.run(
        process_chunk_json_async(text, section_heading, heading_level, profile)
    )
    return blocks
