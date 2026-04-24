import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── API 密钥 ──────────────────────────────────────────────
DOC2X_API_KEY = os.getenv("DOC2X_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# ── Doc2X 配置 ────────────────────────────────────────────
DOC2X_BASE_URL = "https://v2.doc2x.noedgeai.com"
DOC2X_POLL_INTERVAL = 3          # 轮询间隔（秒）
DOC2X_MAX_POLL_RETRIES = 100     # 最大轮询次数（约 5 分钟）

# ── DeepSeek 配置 ─────────────────────────────────────────
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_MAX_RETRIES = 3         # API 调用失败最大重试次数
DEEPSEEK_RETRY_BASE_DELAY = 2    # 指数退避基础延迟（秒）
# 单次回复最大 token（过小易截断 JSON 尾部）；0 表示不传参，使用接口默认
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192"))

# ── 并发与流式配置 ────────────────────────────────────────
DEEPSEEK_STREAM_ENABLED = True
DEEPSEEK_MIN_WORKERS = int(os.getenv("DEEPSEEK_MIN_WORKERS", "4"))
DEEPSEEK_MAX_WORKERS = int(os.getenv("DEEPSEEK_MAX_WORKERS", "16"))
DEEPSEEK_ADAPTIVE_CONCURRENCY = True

# ── 超时配置（秒）────────────────────────────────────────
DEEPSEEK_KEEPALIVE_TIMEOUT = 600      # 10 分钟：等待推理开始的最大时长
DEEPSEEK_INFER_TIMEOUT = 300          # 5 分钟：推理完成最大时长
DEEPSEEK_FIRST_TOKEN_WARN = 120       # 2 分钟无 token 时警告

# ── 自适应并发阈值 ────────────────────────────────────────
ADAPTIVE_FAST_THRESHOLD = 5.0         # 响应 < 5s → 可增加并发
ADAPTIVE_SLOW_THRESHOLD = 30.0        # 响应 > 30s → 减少并发
ADAPTIVE_ADJUST_INTERVAL = 3          # 每 N 个请求评估一次

# ── 文本分块配置 ──────────────────────────────────────────
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "2000"))

# ── JSON 解析纠错（额外一次 API，仅当本地解析失败时）────────────────
DEEPSEEK_JSON_REPAIR_ON_FAIL = os.getenv(
    "DEEPSEEK_JSON_REPAIR_ON_FAIL", "1"
).lower() not in ("0", "false", "no")

# ── 解析失败时的 NDJSON / 文本样本（默认关闭，避免污染仓库）────────────
ATOMIC_DEBUG_PARSE_LOG = os.getenv(
    "ATOMIC_DEBUG_PARSE_LOG", ""
).lower() in ("1", "true", "yes")
ATOMIC_DEBUG_SESSION_ID = os.getenv("ATOMIC_DEBUG_SESSION_ID", "").strip()

# ── Word 模板配置 ─────────────────────────────────────────
TEMPLATE_PATH = "合并_正文标注_20251006_第3届.docx"

# ── 层级分析配置 ──────────────────────────────────────────
HIERARCHY_ANALYSIS_ENABLED = True

# ── 日志配置 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
