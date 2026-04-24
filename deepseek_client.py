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
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

_PKG_DIR = Path(__file__).resolve().parent
_AGENT_DEBUG_LOG_PRIMARY = str(_PKG_DIR / ".cursor" / "parse-debug.ndjson")
_AGENT_DEBUG_LOG_MIRROR = str(_PKG_DIR / "debug-parse-mirror.ndjson")
_PARSE_FAILURE_APPEND_LOG = str(_PKG_DIR / "atomic-json-parse-failures.log")


def _append_parse_failure_human(
    section_heading: str,
    raw: str,
    balanced_count: int,
    last_err: str | None,
) -> None:
    """排障时追加失败样本；需设置 ATOMIC_DEBUG_PARSE_LOG=1。"""
    if not config.ATOMIC_DEBUG_PARSE_LOG:
        return
    try:
        with open(_PARSE_FAILURE_APPEND_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"\n{'=' * 60}\n"
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {section_heading[:120]}\n"
                f"len={len(raw)} balanced_spans={balanced_count} last_err={last_err!r}\n"
                f"--- head (2000) ---\n{raw[:2000]}\n"
            )
    except OSError:
        pass


def _agent_debug_ndjson(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict,
    run_id: str = "parse-debug",
) -> None:
    if not config.ATOMIC_DEBUG_PARSE_LOG:
        return
    payload: dict[str, Any] = {
        "timestamp": int(time.time() * 1000),
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "runId": run_id,
    }
    if config.ATOMIC_DEBUG_SESSION_ID:
        payload["sessionId"] = config.ATOMIC_DEBUG_SESSION_ID
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    for path in (_AGENT_DEBUG_LOG_PRIMARY, _AGENT_DEBUG_LOG_MIRROR):
        try:
            if path == _AGENT_DEBUG_LOG_PRIMARY:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as _df:
                _df.write(line)
        except OSError:
            pass

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
3. 只输出 JSON，不要输出任何解释性文字、markdown 标记、代码块包裹或推理过程标签（如 think 块）。
4. 优先输出 JSON 数组；若使用 JSON 对象包裹数组，请使用键名 "blocks" 存放数组。
5. 确保 JSON 格式合法，可被直接 json.loads() 解析。"""

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


def _max_tokens_kw() -> dict[str, int]:
    n = getattr(config, "DEEPSEEK_MAX_TOKENS", 0)
    if isinstance(n, int) and n > 0:
        return {"max_tokens": n}
    return {}


def build_system_prompt(profile: HierarchyProfile | None = None) -> str:
    """根据 HierarchyProfile 动态构建 system prompt。"""
    if profile is not None:
        hierarchy_section = profile.describe_for_prompt()
    else:
        hierarchy_section = _DEFAULT_HIERARCHY_SECTION
    return _BASE_SYSTEM_PROMPT.format(hierarchy_section=hierarchy_section)


# ── JSON 提取与容错 ──────────────────────────────────────

# 标签名拆开拼接，避免部分环境对敏感标签名的误处理
_think_tag = "think"
_redacted_tag = "redacted_thinking"
_LLM_NOISE_PATTERNS: tuple[str, ...] = (
    rf"<{_think_tag}\b[^>]*>.*?</{_think_tag}>",
    rf"<{_redacted_tag}\b[^>]*>.*?</{_redacted_tag}>",
    r"<reasoning\b[^>]*>.*?</reasoning>",
    r"<thought\b[^>]*>.*?</thought>",
)


def _strip_common_llm_wrappers(text: str) -> str:
    """去掉模型偶发输出的推理标签、think 块等，避免前缀污染 JSON。"""
    t = text.strip().lstrip("\ufeff")
    for _ in range(8):
        prev = t
        for pat in _LLM_NOISE_PATTERNS:
            t = re.sub(pat, "", t, flags=re.DOTALL | re.IGNORECASE)
        t = t.strip()
        if t == prev:
            break
    return t


def _repair_json_array_text(s: str) -> str:
    """轻量修复：多余尾逗号（在 ] 或 } 前）。不替换字符串值内的弯引号，以免破坏合法 JSON。"""
    t = s.strip()
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r",(\s*[\]}])", r"\1", t)
    return t


def _escape_raw_controls_in_json_strings(s: str) -> str:
    """将 JSON 字符串值内的裸换行/回车/制表转为 \\n \\t，修复模型常犯的 Invalid control character。"""
    out: list[str] = []
    i = 0
    n = len(s)
    in_string = False
    escape = False
    while i < n:
        c = s[i]
        if in_string:
            if escape:
                out.append(c)
                escape = False
            elif c == "\\":
                out.append(c)
                escape = True
            elif c == '"':
                out.append(c)
                in_string = False
            elif c == "\n":
                out.append("\\n")
            elif c == "\r":
                if i + 1 < n and s[i + 1] == "\n":
                    out.append("\\n")
                    i += 1
                else:
                    out.append("\\n")
            elif c == "\t":
                out.append("\\t")
            else:
                out.append(c)
        else:
            out.append(c)
            if c == '"':
                in_string = True
        i += 1
    return "".join(out)


def _escape_invalid_json_backslashes_in_strings(s: str) -> str:
    """将 JSON 字符串值内非法 \\ 转义（如 LaTeX \\mu、\\mathbf）改为 \\\\，避免 Invalid \\escape。"""
    out: list[str] = []
    i = 0
    n = len(s)
    in_string = False
    while i < n:
        c = s[i]
        if not in_string:
            out.append(c)
            if c == '"':
                in_string = True
            i += 1
            continue
        if c == '"':
            out.append(c)
            in_string = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "u" and i + 5 < n and all(
                ch in "0123456789abcdefABCDEF" for ch in s[i + 2 : i + 6]
            ):
                out.append(s[i : i + 6])
                i += 6
                continue
            if nxt in '"\\/bfnrt':
                out.append(c)
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _json_loads_variants(s: str) -> tuple[Any, str | None]:
    """依次尝试多种修复后的 json.loads；返回 (data, None) 或 (None, 最后一条错误信息)。"""
    variants: list[str] = []
    seen: set[str] = set()
    for v in (
        s.strip(),
        _repair_json_array_text(s),
        _escape_raw_controls_in_json_strings(s.strip()),
        _escape_raw_controls_in_json_strings(_repair_json_array_text(s)),
        _escape_invalid_json_backslashes_in_strings(s.strip()),
        _escape_invalid_json_backslashes_in_strings(_repair_json_array_text(s)),
        _escape_invalid_json_backslashes_in_strings(
            _escape_raw_controls_in_json_strings(s.strip())
        ),
        _escape_raw_controls_in_json_strings(
            _escape_invalid_json_backslashes_in_strings(s.strip())
        ),
    ):
        if v and v not in seen:
            seen.add(v)
            variants.append(v)
    last_err: str | None = None
    for v in variants:
        try:
            return json.loads(v), None
        except json.JSONDecodeError as e:
            last_err = f"{e.msg} (pos {e.pos})"
    try:
        from json_repair import repair_json  # type: ignore[import-not-found]
    except ImportError:
        repair_json = None
    if repair_json is not None:
        for base in variants:
            try:
                fixed = repair_json(base)
                if isinstance(fixed, str) and fixed.strip():
                    return json.loads(fixed), None
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return None, last_err


def _balanced_top_level_array_end(s: str, start: int) -> int | None:
    """从 start（必须为 '['）扫描，返回与之匹配的 ']' 下标；截断或失衡则 None。"""
    if start >= len(s) or s[start] != "[":
        return None
    depth = 0
    i = start
    n = len(s)
    in_string = False
    escape = False
    while i < n:
        c = s[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _balanced_json_object_end(s: str, start: int) -> int | None:
    """从 start（必须为 '{'）扫描，返回与之匹配的 '}' 下标。"""
    if start >= len(s) or s[start] != "{":
        return None
    depth = 0
    i = start
    n = len(s)
    in_string = False
    escape = False
    while i < n:
        c = s[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _balanced_array_candidates(s: str) -> list[str]:
    """枚举文本中每个 '[' 开始的平衡数组片段，去重后按长度降序（优先更长、更可能是主输出）。"""
    spans: list[str] = []
    n = len(s)
    for start in range(n):
        if s[start] != "[":
            continue
        end = _balanced_top_level_array_end(s, start)
        if end is not None:
            spans.append(s[start : end + 1])
    seen: set[str] = set()
    uniq: list[str] = []
    for sp in spans:
        if sp not in seen:
            seen.add(sp)
            uniq.append(sp)
    uniq.sort(key=len, reverse=True)
    return uniq


def _is_nonempty_block_list(data: Any) -> bool:
    """模型输出须为非空、元素均为对象的 JSON 数组（排除 []、[1,2] 等误解析）。"""
    return (
        isinstance(data, list)
        and len(data) > 0
        and all(isinstance(x, dict) for x in data)
    )


def _coerce_wrapped_object_to_block_list(data: Any) -> list[dict] | None:
    """顶层为数组，或 JSON 模式常见的 {\"blocks\":[...]} 等包装。"""
    if _is_nonempty_block_list(data):
        return data
    if isinstance(data, dict):
        for key in (
            "blocks",
            "data",
            "items",
            "annotations",
            "result",
            "content",
            "output",
            "sections",
        ):
            v = data.get(key)
            if _is_nonempty_block_list(v):
                return v
    return None


def _try_parse_dict_object_list(s: str) -> list[dict] | None:
    """解析 JSON（数组或包装对象），得到非空 dict 元素列表。"""
    data, _ = _json_loads_variants(s)
    if data is None:
        return None
    return _coerce_wrapped_object_to_block_list(data)


def _salvage_type_dicts_from_text(text: str) -> list[dict] | None:
    """数组整体损坏时，扫描平衡 {...} 片段，解析含 type 字段的对象（支持截断数组或 NDJSON）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"\{", text):
        k = m.start()
        end = _balanced_json_object_end(text, k)
        if end is None:
            continue
        sl = text[k : end + 1]
        if sl in seen or '"type"' not in sl:
            continue
        data, _ = _json_loads_variants(sl)
        if isinstance(data, dict) and "type" in data:
            seen.add(sl)
            out.append(data)
    return out if out else None


def _extract_json_array(text: str, section_heading: str = "") -> list[dict] | None:
    """从 DeepSeek 响应文本中提取 JSON 数组。

    依次尝试：直接解析 → 轻量修复后再解析 → 代码块 → 平衡括号候选（避免贪婪正则吃到文内 [12] 等）
    → 最后保留贪婪正则兜底。
    """
    text = _strip_common_llm_wrappers(text)
    stripped = text.strip()
    decode_errors: list[dict] = []

    if (
        stripped.startswith("{")
        and stripped.endswith("}")
        and not stripped.startswith("[")
    ):
        top_raw, _ = _json_loads_variants(stripped)
        if top_raw is not None:
            coerced_top = _coerce_wrapped_object_to_block_list(top_raw)
            if coerced_top is not None:
                return coerced_top
        wrapped = _try_parse_dict_object_list("[" + stripped + "]")
        if wrapped is not None:
            return wrapped

    if stripped.startswith("["):
        try:
            direct_data = json.loads(stripped)
        except json.JSONDecodeError as e:
            decode_errors.append({
                "path": "direct",
                "msg": e.msg,
                "pos": e.pos,
                "lineno": e.lineno,
                "colno": e.colno,
            })
        else:
            coerced = _coerce_wrapped_object_to_block_list(direct_data)
            if coerced is not None:
                return coerced
        repaired_list = _try_parse_dict_object_list(stripped)
        if repaired_list is not None:
            return repaired_list

    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        fenced = m.group(1).strip()
        try:
            fenced_data = json.loads(fenced)
        except json.JSONDecodeError as e:
            decode_errors.append({
                "path": "fence",
                "msg": e.msg,
                "pos": e.pos,
                "lineno": e.lineno,
                "colno": e.colno,
                "fenced_len": len(fenced),
            })
        else:
            coerced_f = _coerce_wrapped_object_to_block_list(fenced_data)
            if coerced_f is not None:
                return coerced_f
        fenced_list = _try_parse_dict_object_list(fenced)
        if fenced_list is not None:
            return fenced_list

    balanced_spans = _balanced_array_candidates(text)
    last_variant_err: str | None = None
    for span in balanced_spans:
        strict_list = _try_parse_dict_object_list(span)
        if strict_list is not None:
            return strict_list
        data, verr = _json_loads_variants(span)
        if verr:
            last_variant_err = verr
        if data is not None:
            coerced_b = _coerce_wrapped_object_to_block_list(data)
            if coerced_b is not None:
                return coerced_b

    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        bracket_slice = m.group(0)
        try:
            greedy_data = json.loads(bracket_slice)
        except json.JSONDecodeError as e:
            decode_errors.append({
                "path": "bracket_regex",
                "msg": e.msg,
                "pos": e.pos,
                "lineno": e.lineno,
                "colno": e.colno,
                "slice_len": len(bracket_slice),
            })
        else:
            coerced_g = _coerce_wrapped_object_to_block_list(greedy_data)
            if coerced_g is not None:
                return coerced_g
        greedy_list = _try_parse_dict_object_list(bracket_slice)
        if greedy_list is not None:
            return greedy_list

    salvaged = _salvage_type_dicts_from_text(text)
    if salvaged is not None:
        return salvaged

    preview = 240
    pos_hint = decode_errors[0]["pos"] if decode_errors else None
    snip = ""
    if pos_hint is not None and isinstance(pos_hint, int) and 0 <= pos_hint < len(stripped):
        lo = max(0, pos_hint - 60)
        hi = min(len(stripped), pos_hint + 60)
        snip = stripped[lo:hi]
    _agent_debug_ndjson(
        "H1-H5",
        "deepseek_client.py:_extract_json_array",
        "all_json_paths_failed",
        {
            "section_heading": section_heading[:80],
            "raw_len": len(text),
            "stripped_len": len(stripped),
            "starts_with_bracket": stripped.startswith("["),
            "ends_with_bracket": stripped.rstrip().endswith("]"),
            "first_bracket_idx": text.find("["),
            "last_bracket_idx": text.rfind("]"),
            "decode_errors": decode_errors,
            "stripped_head": stripped[:preview],
            "stripped_tail": stripped[-preview:] if len(stripped) > preview else "",
            "error_pos_snippet": snip,
            "fence_block_found": bool(
                re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
            ),
            "balanced_span_count": len(balanced_spans),
            "balanced_longest_len": len(balanced_spans[0]) if balanced_spans else 0,
            "last_variant_json_error": last_variant_err,
            "type_marker_count": text.count('"type"'),
        },
        run_id="post-fix",
    )

    logger.warning(
        "JSON 提取失败（诊断）章节=%r balanced=%d type_markers=%d last_err=%s head=%.120s",
        section_heading[:60],
        len(balanced_spans),
        text.count('"type"'),
        last_variant_err,
        stripped[:120],
    )

    _append_parse_failure_human(
        section_heading, text, len(balanced_spans), last_variant_err
    )

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


_REPAIR_JSON_SYSTEM = """你是一个 JSON 修复器。用户会给出一段本应合法但可能有语法错误的文本（多为 JSON 数组或对象）。
请只输出修复后的完整 JSON：要么是 [...] 数组（元素为带 type、level 等字段的对象），要么是 {"blocks":[...]} 对象。
不要 markdown 代码块，不要推理标签，不要解释。"""


async def _async_repair_json_via_model(
    client: AsyncOpenAI,
    broken_raw: str,
) -> str | None:
    """本地解析失败时，再请求模型只做 JSON 语法纠错（额外消耗 1 次调用）。"""
    cap = 28000
    user = (
        "请修复为合法 JSON（保留所有字段与字符串内容，仅修正引号、逗号、转义、缺失括号等问题）：\n\n"
        f"{broken_raw[:cap]}"
    )
    try:
        if config.DEEPSEEK_STREAM_ENABLED:
            response = await client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": _REPAIR_JSON_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                stream=True,
                **_max_tokens_kw(),
            )
            text, _ = await _stream_collect(response)
        else:
            response = await client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": _REPAIR_JSON_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                **_max_tokens_kw(),
            )
            text = (response.choices[0].message.content or "").strip()
        out = _strip_common_llm_wrappers(text)
        return out if out else None
    except (APIError, APITimeoutError, RateLimitError, TypeError, ValueError) as exc:
        logger.warning("JSON 纠错 API 失败: %s", exc)
        return None


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
                    **_max_tokens_kw(),
                )
                raw, stats = await _stream_collect(response)
                raw = _strip_common_llm_wrappers(raw)
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
                    **_max_tokens_kw(),
                )
                elapsed = time.monotonic() - t0
                raw = _strip_common_llm_wrappers(
                    (response.choices[0].message.content or "").strip()
                )
                last_stats = {"elapsed": elapsed, "keepalive_count": 0}
                logger.info("DeepSeek 返回 %d 字符 (%.1fs)", len(raw), elapsed)

            blocks = _extract_json_array(raw, section_heading=section_heading)
            if blocks:
                validated = _validate_blocks(blocks, profile)
                if validated:
                    logger.info("成功解析 %d 个 JSON blocks", len(validated))
                    return validated, last_stats

            if "【" in raw:
                fb = _fallback_parse_plaintext(raw, heading_level)
                if fb:
                    validated = _validate_blocks(fb, profile)
                    if validated:
                        logger.info(
                            "JSON 解析失败，已用【】纯文本回退：%d 个 blocks",
                            len(validated),
                        )
                        return validated, last_stats

            if attempt < config.DEEPSEEK_MAX_RETRIES:
                logger.warning(
                    "章节 %r 解析失败，主模型重试 %d/%d（下次为新一次生成）",
                    section_heading[:50],
                    attempt,
                    config.DEEPSEEK_MAX_RETRIES,
                )
                continue

            if (
                config.DEEPSEEK_JSON_REPAIR_ON_FAIL
                and raw
                and len(raw.strip()) > 20
            ):
                logger.info(
                    "最后一次尝试：模型 JSON 纠错（额外 1 次请求）… 章节: %s",
                    section_heading[:40],
                )
                fixed = await _async_repair_json_via_model(client, raw)
                if fixed:
                    blocks = _extract_json_array(
                        fixed, section_heading=section_heading
                    )
                    if blocks:
                        validated = _validate_blocks(blocks, profile)
                        if validated:
                            logger.info(
                                "模型纠错后成功解析 %d 个 JSON blocks",
                                len(validated),
                            )
                            return validated, last_stats
                    logger.warning(
                        "JSON 纠错后仍无法解析 len=%d 章节=%r head=%.150s",
                        len(fixed),
                        section_heading[:50],
                        fixed[:150],
                    )
                else:
                    logger.warning(
                        "JSON 纠错无有效返回 章节=%r",
                        section_heading[:50],
                    )

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
