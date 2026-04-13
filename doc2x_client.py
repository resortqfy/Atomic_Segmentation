"""Doc2X API v2 客户端 —— 将本地 PDF 解析为 Markdown 文本。

完整流程：preupload → PUT 上传 → 轮询解析 → 发起导出(md) → 轮询导出 → 下载文本
参考: https://github.com/NoEdgeAI/doc2x-doc/blob/main/Python/requests/pdf2file.py
"""

import io
import logging
import time
import zipfile

import requests

import config

logger = logging.getLogger(__name__)


class Doc2XError(Exception):
    """Doc2X API 调用过程中的异常。"""


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.DOC2X_API_KEY}"}


# ── Step 1: 预上传，获取预签名 URL 和 UID ────────────────
def _preupload() -> dict:
    url = f"{config.DOC2X_BASE_URL}/api/v2/parse/preupload"
    resp = requests.post(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "success":
        raise Doc2XError(f"预上传失败: {data}")
    logger.info("预上传成功，uid=%s", data["data"]["uid"])
    return data["data"]


# ── Step 2: 通过预签名 URL 上传 PDF 二进制流 ─────────────
def _upload_file(pdf_path: str, presigned_url: str) -> None:
    with open(pdf_path, "rb") as f:
        resp = requests.put(presigned_url, data=f, timeout=300)
    resp.raise_for_status()
    logger.info("PDF 上传完成: %s", pdf_path)


# ── Step 3: 轮询解析状态直到 success / failed ────────────
def _wait_for_parse(uid: str) -> dict:
    url = f"{config.DOC2X_BASE_URL}/api/v2/parse/status"
    for i in range(config.DOC2X_MAX_POLL_RETRIES):
        resp = requests.get(url, headers=_headers(), params={"uid": uid}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "success":
            raise Doc2XError(f"查询解析状态失败: {data}")

        status = data["data"].get("status")
        if status == "success":
            logger.info("PDF 解析完成")
            return data["data"]
        if status == "failed":
            raise Doc2XError(f"PDF 解析失败: {data['data'].get('detail')}")

        progress = data["data"].get("progress", "?")
        logger.info("解析中… 进度: %s  (%d/%d)", progress, i + 1, config.DOC2X_MAX_POLL_RETRIES)
        time.sleep(config.DOC2X_POLL_INTERVAL)

    raise Doc2XError(f"解析超时，已轮询 {config.DOC2X_MAX_POLL_RETRIES} 次")


# ── Step 4: 发起导出为 Markdown ──────────────────────────
def _export_to_markdown(uid: str) -> None:
    url = f"{config.DOC2X_BASE_URL}/api/v2/convert/parse"
    payload = {"uid": uid, "to": "md", "formula_mode": "normal"}
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "success":
        raise Doc2XError(f"发起 Markdown 导出失败: {data}")
    logger.info("已发起 Markdown 导出请求")


# ── Step 5: 轮询导出结果并下载 Markdown（ZIP 格式）──────
def _wait_and_download_markdown(uid: str) -> str:
    """Doc2X 导出 Markdown 时返回的是 ZIP 包（含 output.md + images/）。"""
    url = f"{config.DOC2X_BASE_URL}/api/v2/convert/parse/result"
    for i in range(config.DOC2X_MAX_POLL_RETRIES):
        resp = requests.get(url, headers=_headers(), params={"uid": uid}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "success":
            raise Doc2XError(f"查询导出结果失败: {data}")

        status = data["data"].get("status")
        if status == "success":
            file_url = data["data"]["url"]
            logger.info("导出完成，正在下载 ZIP…")
            zip_resp = requests.get(file_url, timeout=120)
            zip_resp.raise_for_status()
            return _extract_markdown_from_zip(zip_resp.content)
        if status == "failed":
            raise Doc2XError("Markdown 导出失败")

        logger.info("导出处理中… (%d/%d)", i + 1, config.DOC2X_MAX_POLL_RETRIES)
        time.sleep(config.DOC2X_POLL_INTERVAL)

    raise Doc2XError(f"导出超时，已轮询 {config.DOC2X_MAX_POLL_RETRIES} 次")


def _extract_markdown_from_zip(zip_bytes: bytes) -> str:
    """从 Doc2X 返回的 ZIP 包中提取 output.md 的文本内容。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        md_files = [n for n in zf.namelist() if n.endswith(".md")]
        if not md_files:
            raise Doc2XError(f"ZIP 包中未找到 .md 文件，包含: {zf.namelist()}")
        md_name = md_files[0]
        logger.info("从 ZIP 中提取: %s", md_name)
        return zf.read(md_name).decode("utf-8")


# ── 对外高层接口 ─────────────────────────────────────────
def parse_pdf_to_markdown(pdf_path: str) -> str:
    """将本地 PDF 文件上传至 Doc2X 并返回解析后的 Markdown 文本。"""
    upload_data = _preupload()
    presigned_url = upload_data["url"]
    uid = upload_data["uid"]

    _upload_file(pdf_path, presigned_url)
    _wait_for_parse(uid)
    _export_to_markdown(uid)
    markdown_text = _wait_and_download_markdown(uid)

    logger.info("成功获取 Markdown 文本，长度 %d 字符", len(markdown_text))
    return markdown_text
