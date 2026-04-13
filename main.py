"""主程序入口 —— PDF 解析 → 层级学习 → 文本分块 → 异步 DeepSeek JSON → 模板渲染 DOCX。

用法:
    python main.py paper.pdf
    python main.py paper.pdf -o output.docx
    python main.py paper.pdf --template custom_template.docx
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time

import config  # noqa: F401  (触发 logging 配置和 .env 加载)
from doc2x_client import parse_pdf_to_markdown, Doc2XError
from deepseek_client import process_chunk_json_async
from docx_generator import load_template, render_blocks_to_docx
from hierarchy_analyzer import analyze_template_hierarchy, HierarchyProfile

logger = logging.getLogger(__name__)


# ── 文本分块（携带层级信息）────────────────────────────────

def _split_markdown_into_sections(markdown: str) -> list[dict]:
    """将 Markdown 文本按标题拆分为章节块，每块携带层级信息。

    返回: [{"title": "章节名", "body": "正文内容", "heading_level": 1~6}]
    """
    heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    sections: list[dict] = []

    matches = list(heading_re.finditer(markdown))

    if not matches:
        chunks = _split_long_text(markdown)
        for i, chunk in enumerate(chunks):
            sections.append({
                "title": f"Part {i + 1}",
                "body": chunk,
                "heading_level": 2,
            })
        return sections

    preamble = markdown[: matches[0].start()].strip()
    if preamble:
        for i, chunk in enumerate(_split_long_text(preamble)):
            sections.append({
                "title": "Preamble" if i == 0 else f"Preamble (cont. {i + 1})",
                "body": chunk,
                "heading_level": 1,
            })

    for idx, m in enumerate(matches):
        hashes = m.group(1)
        title = m.group(2).strip()
        level = len(hashes)
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()

        if body:
            for i, chunk in enumerate(_split_long_text(body)):
                chunk_title = title if i == 0 else f"{title} (cont. {i + 1})"
                sections.append({
                    "title": chunk_title,
                    "body": chunk,
                    "heading_level": level,
                })
        else:
            sections.append({
                "title": title,
                "body": "",
                "heading_level": level,
            })

    return sections


def _split_long_text(text: str) -> list[str]:
    """将超长文本拆分为不超过 CHUNK_MAX_CHARS 字符的块。"""
    max_chars = config.CHUNK_MAX_CHARS
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_split_by_sentences(para, max_chars))
            continue

        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [text]


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _split_by_sentences(text: str, max_chars: int) -> list[str]:
    """按句子边界将超长段落拆分为不超过 max_chars 的块。"""
    sentences = _SENTENCE_END_RE.split(text)
    chunks: list[str] = []
    current = ""

    for sent in sentences:
        if current and len(current) + len(sent) + 1 > max_chars:
            chunks.append(current.strip())
            current = sent
        else:
            current = f"{current} {sent}" if current else sent

    if current.strip():
        chunks.append(current.strip())

    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
        else:
            for i in range(0, len(chunk), max_chars):
                final.append(chunk[i:i + max_chars])

    return final if final else [text]


# ── 非正文章节过滤 ─────────────────────────────────────────

_SKIP_SECTIONS = {
    "references", "bibliography",
    "data availability", "code availability",
    "acknowledgements", "acknowledgments",
    "author contributions", "competing interests",
    "additional information", "reporting summary",
    "supplementary information", "supplementary materials",
}


def _is_backmatter(title: str) -> bool:
    """判断章节标题是否属于参考文献/附录等非正文部分。"""
    cleaned = re.sub(r"^\d+[\.\)]\s*", "", title)
    cleaned = re.sub(r"\s*\(cont\.\s*\d+\)\s*$", "", cleaned)
    return cleaned.strip().lower() in _SKIP_SECTIONS


# ── 自适应并发控制器 ─────────────────────────────────────

class AdaptiveSemaphore:
    """基于 API 响应延迟动态调整并发量的信号量。"""

    def __init__(
        self,
        initial: int,
        min_workers: int,
        max_workers: int,
        enabled: bool = True,
    ):
        self._current = initial
        self._min = min_workers
        self._max = max_workers
        self._enabled = enabled
        self._sem = asyncio.Semaphore(initial)
        self._recent_latencies: list[float] = []
        self._adjust_interval = config.ADAPTIVE_ADJUST_INTERVAL
        self._call_count = 0
        self._lock = asyncio.Lock()

    @property
    def current_limit(self) -> int:
        return self._current

    async def acquire(self):
        await self._sem.acquire()

    def release(self):
        self._sem.release()

    async def report_latency(self, elapsed: float, keepalive_count: int):
        """每次请求完成后上报延迟和 keep-alive 次数，触发自适应调整。"""
        if not self._enabled:
            return

        async with self._lock:
            self._recent_latencies.append(elapsed)
            self._call_count += 1

            if self._call_count % self._adjust_interval != 0:
                return

            if not self._recent_latencies:
                return

            avg = sum(self._recent_latencies) / len(self._recent_latencies)
            self._recent_latencies.clear()

            old = self._current
            if avg < config.ADAPTIVE_FAST_THRESHOLD and self._current < self._max:
                self._current = min(self._current + 2, self._max)
                for _ in range(self._current - old):
                    self._sem.release()
                logger.info(
                    "自适应并发: 响应快 (avg=%.1fs) → 增加并发 %d → %d",
                    avg, old, self._current,
                )
            elif avg > config.ADAPTIVE_SLOW_THRESHOLD and self._current > self._min:
                new_target = max(self._current - 2, self._min)
                self._current = new_target
                logger.info(
                    "自适应并发: 响应慢 (avg=%.1fs) → 减少并发 %d → %d",
                    avg, old, self._current,
                )


# ── 进度跟踪器 ───────────────────────────────────────────

class ProgressTracker:
    """跟踪异步任务的完成进度，提供 ETA 估算。"""

    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.failed = 0
        self.in_progress = 0
        self._start_time = time.monotonic()
        self._lock = asyncio.Lock()

    async def start_one(self):
        async with self._lock:
            self.in_progress += 1

    async def complete_one(self, success: bool = True):
        async with self._lock:
            self.in_progress -= 1
            if success:
                self.completed += 1
            else:
                self.failed += 1
            self._log_progress()

    def _log_progress(self):
        done = self.completed + self.failed
        elapsed = time.monotonic() - self._start_time
        avg_per_task = elapsed / done if done > 0 else 0
        remaining = self.total - done
        eta = avg_per_task * remaining

        logger.info(
            "进度: %d/%d 完成, %d 失败, %d 进行中 | "
            "平均 %.1fs/任务 | 预计剩余 %.0fs",
            self.completed, self.total, self.failed, self.in_progress,
            avg_per_task, eta,
        )


# ── 异步主流程 ───────────────────────────────────────────

async def _process_all_chunks(
    api_tasks: list[dict],
    profile: HierarchyProfile | None,
    adaptive_sem: AdaptiveSemaphore,
    tracker: ProgressTracker,
) -> dict[int, list[dict]]:
    """并发调用 DeepSeek API 处理所有分块。"""
    results: dict[int, list[dict]] = {}
    failed_sections: list[str] = []

    async def _call(task: dict):
        await tracker.start_one()
        await adaptive_sem.acquire()
        try:
            blocks, stats = await process_chunk_json_async(
                task["body"],
                task["title"],
                task["effective_level"],
                profile,
            )
            results[task["idx"]] = blocks
            await tracker.complete_one(success=True)

            elapsed = stats.get("elapsed", 0)
            keepalive = stats.get("keepalive_count", 0)
            await adaptive_sem.report_latency(elapsed, keepalive)

        except Exception as exc:
            logger.error("章节 '%s' 处理失败: %s", task["title"], exc)
            failed_sections.append(task["title"])
            await tracker.complete_one(success=False)
        finally:
            adaptive_sem.release()

    await asyncio.gather(*[_call(t) for t in api_tasks])
    return results


def main():
    parser = argparse.ArgumentParser(
        description="学术论文 PDF 自动解析与原子化切分标注（动态排版管线）",
    )
    parser.add_argument("pdf", help="输入的 PDF 文件路径")
    parser.add_argument(
        "-o", "--output", default=None,
        help="输出的 Word 文件路径（默认: <pdf名>_atomized.docx）",
    )
    parser.add_argument(
        "--template", default=config.TEMPLATE_PATH,
        help=f"Word 模板文件路径（默认: {config.TEMPLATE_PATH}）",
    )
    args = parser.parse_args()

    pdf_path = args.pdf
    if not os.path.isfile(pdf_path):
        logger.error("PDF 文件不存在: %s", pdf_path)
        sys.exit(1)

    template_path = args.template
    if not os.path.isfile(template_path):
        logger.error("模板文件不存在: %s", template_path)
        sys.exit(1)

    output_path = args.output
    if not output_path:
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        output_path = f"{base}_atomized.docx"

    # ── 步骤 0: 分析模板层级结构 ─────────────────────────
    profile: HierarchyProfile | None = None
    if config.HIERARCHY_ANALYSIS_ENABLED:
        logger.info("=" * 60)
        logger.info("步骤 0/5: 分析模板文档层级结构…")
        logger.info("=" * 60)
        profile = analyze_template_hierarchy(template_path)
        logger.info("\n%s", profile.describe_for_prompt())

    # ── 步骤 1: Doc2X 解析 PDF → Markdown ───────────────
    logger.info("=" * 60)
    logger.info("步骤 1/5: 调用 Doc2X 解析 PDF…")
    logger.info("=" * 60)
    try:
        markdown_text = parse_pdf_to_markdown(pdf_path)
    except Doc2XError as exc:
        logger.error("Doc2X 解析失败: %s", exc)
        sys.exit(1)

    # ── 步骤 2: 按标题分块（含层级信息）─────────────────
    logger.info("=" * 60)
    logger.info("步骤 2/5: Markdown 分块…")
    logger.info("=" * 60)
    sections = _split_markdown_into_sections(markdown_text)
    logger.info("共拆分为 %d 个章节块", len(sections))

    # ── 步骤 3: 异步 DeepSeek JSON 结构化处理 ──────────
    logger.info("=" * 60)
    logger.info(
        "步骤 3/5: DeepSeek 原子化 + JSON 结构化（初始并发 %d，范围 %d-%d）…",
        config.DEEPSEEK_MAX_WORKERS,
        config.DEEPSEEK_MIN_WORKERS,
        config.DEEPSEEK_MAX_WORKERS,
    )
    logger.info("=" * 60)

    tasks: list[dict] = []
    for idx, sec in enumerate(sections, 1):
        title = sec["title"]
        body = sec["body"]
        level = sec["heading_level"]
        is_continuation = "(cont." in title

        if _is_backmatter(title):
            logger.info("[%d/%d] 跳过非正文章节: %s", idx, len(sections), title)
            continue

        heading_block = None
        if not is_continuation:
            heading_block = {"type": "heading", "level": level, "text": title}

        needs_api = len(body.strip()) >= 20
        if not needs_api:
            logger.info("[%d/%d] 跳过过短的章节正文: %s", idx, len(sections), title)

        effective_level = max(2, level)
        tasks.append({
            "idx": idx,
            "title": title,
            "body": body,
            "level": level,
            "effective_level": effective_level,
            "heading_block": heading_block,
            "needs_api": needs_api,
        })

    api_tasks = [t for t in tasks if t["needs_api"]]
    logger.info(
        "提交 %d 个章节到 DeepSeek（共 %d 个任务）…",
        len(api_tasks), len(tasks),
    )

    adaptive_sem = AdaptiveSemaphore(
        initial=config.DEEPSEEK_MAX_WORKERS,
        min_workers=config.DEEPSEEK_MIN_WORKERS,
        max_workers=config.DEEPSEEK_MAX_WORKERS,
        enabled=config.DEEPSEEK_ADAPTIVE_CONCURRENCY,
    )
    tracker = ProgressTracker(total=len(api_tasks))

    results = asyncio.run(
        _process_all_chunks(api_tasks, profile, adaptive_sem, tracker)
    )

    failed_sections = []
    for t in api_tasks:
        if t["idx"] not in results:
            failed_sections.append(t["title"])

    # 步骤 3c: 按原始顺序合并
    all_blocks: list[dict] = []
    for t in tasks:
        if t["heading_block"]:
            all_blocks.append(t["heading_block"])
        if t["idx"] in results:
            all_blocks.extend(results[t["idx"]])

    logger.info("JSON blocks 总计: %d 个", len(all_blocks))

    # ── 步骤 4: 加载模板 → 渲染 → 保存 ─────────────────
    logger.info("=" * 60)
    logger.info("步骤 4/5: 加载模板并渲染 Word 文档…")
    logger.info("=" * 60)

    if not all_blocks:
        logger.error("没有可渲染的 blocks，无法生成文档")
        sys.exit(1)

    doc = load_template(template_path)
    render_blocks_to_docx(doc, all_blocks, output_path, profile)

    # ── 步骤 5: 汇总 ──────────────────────────────────
    logger.info("=" * 60)
    logger.info("全部完成！输出文件: %s", output_path)
    if failed_sections:
        logger.warning("以下章节处理失败: %s", ", ".join(failed_sections))
    logger.info(
        "统计: %d 成功, %d 失败, 并发范围 %d-%d",
        tracker.completed, tracker.failed,
        config.DEEPSEEK_MIN_WORKERS, adaptive_sem.current_limit,
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
