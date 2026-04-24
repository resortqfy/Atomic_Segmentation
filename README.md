# Atomic Segmentation — 学术论文 PDF 原子化切分标注管线

将英文学术论文 PDF 自动解析为结构化 Word 文档：每个英文原子句配有中文小标题标注，文档层级结构与文字样式从示例模板中学习，输出兼容 WPS 目录识别。

## 管线流程

```
PDF ──Doc2X──▶ Markdown ──分块──▶ DeepSeek API ──JSON──▶ 模板驱动 DOCX
                                   (异步流式 + 自适应并发)
```

1. **Doc2X 解析** — 上传 PDF 至 Doc2X API，获取带标题层级的 Markdown 文本
2. **层级学习** — 从示例 `.docx` 模板中提取 `HierarchyProfile`（层级角色、每级字体/字号/粗体/东亚字体、行距）
3. **Markdown 分块** — 按标题拆分为章节，过滤参考文献等非正文部分，超长文本按段落/句子边界二次切分
4. **DeepSeek 原子化** — 异步流式调用 DeepSeek API，将每个章节拆分为原子句并生成中文小标题，输出结构化 JSON
5. **模板渲染** — 基于 `profile.style_map` 逐级应用 run 级字体覆写 + 段落级行距，强制写入 `w:outlineLvl` 确保 WPS 兼容

## 项目结构

```
├── main.py                 # 主入口，编排整条管线
├── config.py               # 配置集中管理（API 密钥、并发、超时等）
├── doc2x_client.py         # Doc2X API 客户端（PDF → Markdown）
├── deepseek_client.py      # DeepSeek API 客户端（异步流式 + JSON 结构化）
├── hierarchy_analyzer.py   # 模板层级分析器（提取 HierarchyProfile + 众数聚合）
├── docx_generator.py       # Word 渲染引擎（逐级样式覆写 + OxmlElement 操作）
├── test_integration.py     # 集成测试套件（74 项断言）
├── requirements.txt        # Python 依赖
└── 合并_正文标注_*.docx     # 示例模板文档
```

## 快速开始

### 1. 安装依赖

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

其中包含可选依赖 `json-repair`，用于在模型输出 JSON 轻微损坏时提高解析成功率。

### 2. 配置 API 密钥

在项目根目录创建 `.env` 文件：

```
DOC2X_API_KEY=your_doc2x_api_key
DEEPSEEK_API_KEY=your_deepseek_api_key
```

### 2. 配置 API 密钥

在项目根目录创建 `.env` 文件：

```env
DOC2X_API_KEY=your_doc2x_api_key
DEEPSEEK_API_KEY=your_deepseek_api_key
```

**API 平台官网及获取说明：**

*   **Doc2X API**：
    *   官网地址：[Doc2X 官方网站](https://doc2x.noedgeai.com/)
    *   获取方法：注册并登录账号后，进入“开发者”或“API 管理”控制台，点击生成新的 API Token/Key 并复制。
*   **DeepSeek API**：
    *   官网地址：[DeepSeek 开放平台](https://platform.deepseek.com/)
    *   获取方法：注册并登录账号后，在左侧导航栏找到“API Keys”菜单，点击“创建 API Key”，生成后请妥善保存。


### 3. 运行

```bash
# 基本用法
python main.py paper.pdf

# 指定输出路径
python main.py paper.pdf -o output.docx

# 使用自定义模板
python main.py paper.pdf --template my_template.docx
```

输出文件默认命名为 `<论文名>_atomized.docx`。

## 核心特性

### 模板层级学习与样式保真

`hierarchy_analyzer.py` 从示例 Word 文档中自动提取：
- 各级标题的字体名、字号、粗体、东亚字体、行距等 run/段落级属性
- 层级角色映射（主标题 / 章节标题 / 中文小标题）
- 正文段落的字体信息（仅从 Normal 样式段落中聚合）
- 层级转换模式（如 Level 2 → Level 3 → Level 4）

每个属性通过 **众数聚合**（`_compute_majority_style`）跨该级别所有段落取最常见值，避免单一段落的偶然覆写造成偏差。

提取结果构建为 `HierarchyProfile`，驱动两个下游环节：
- 注入 DeepSeek 的 system prompt（层级约束）
- 渲染时逐级设置 run 级字体和段落行距（样式保真）

可作为独立 CLI 工具调试层级结构：

```bash
python hierarchy_analyzer.py 合并_正文标注_20251006_第3届.docx
```

以示例模板为例，学习到的每级样式：

| Level | 角色 | 字体 | 字号 | 粗体 | 东亚字体 |
|-------|------|------|------|------|----------|
| 1 | 论文主标题 | Times New Roman | 14pt | 是 | Times New Roman |
| 2 | 一级章节标题 | Times New Roman | 14pt | 是 | Times New Roman |
| 3 | 中文小标题 | Times New Roman | 12pt | 是 | 宋体 |
| 4 | 中文小标题 | Times New Roman | 12pt | 是 | Times New Roman |
| 5 | 中文小标题 | Times New Roman | 12pt | 否 | (继承) |
| 6 | 中文小标题 | Arial | 9pt | 否 | 黑体 |
| Normal | 英文正文 | Times New Roman | 12pt | 否 | 微软雅黑 (fallback) |

### 样式渲染机制

渲染引擎 `docx_generator.py` 对每个段落执行两层样式设置：

1. **段落样式** — 设置 `Heading {level}` 或 `Normal`，继承模板中的段前段后间距、大纲级别等段落属性
2. **Run 级字体覆写** — 通过 `_apply_profile_fonts()` 从 `profile.style_map[level]` 读取字体名/字号/粗体/东亚字体，显式写入 `w:rFonts`、`w:sz`、`w:b` 等 OxmlElement 属性，确保输出与示例文档视觉一致

### 异步流式 API 调用

- 使用 `openai.AsyncOpenAI` + `stream=True` 调用 DeepSeek
- 感知 keep-alive 心跳消息，正确处理 DeepSeek 高流量排队场景
- 10 分钟连接超时 + 5 分钟推理超时，与 DeepSeek 官方限制对齐
- 指数退避重试（最多 3 次），400 错误不重试

### 自适应并发控制

`AdaptiveSemaphore` 根据实际 API 响应延迟动态调整并发数：
- 响应快（< 5s）→ 增加并发（上限 16）
- 响应慢（> 30s）→ 减少并发（下限 4）
- 每 3 个请求评估一次，平滑调整
- 并发范围可通过环境变量 `DEEPSEEK_MIN_WORKERS` / `DEEPSEEK_MAX_WORKERS` 配置

### WPS 目录兼容

通过 OxmlElement 底层操作确保输出文档在 WPS Office 中可正常生成目录：
- 每个 Heading 段落显式写入 `w:outlineLvl`（0-based）
- 每个 Normal 段落写入 `w:outlineLvl="9"`（正文级别）
- 模板加载时校正 Heading 1-6 样式定义中的 `outlineLvl`

## 配置说明

`config.py` 中的主要配置项（均可通过 `.env` 或环境变量覆盖）：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek 模型名 |
| `DEEPSEEK_STREAM_ENABLED` | `True` | 启用流式传输 |
| `DEEPSEEK_MIN_WORKERS` | `4` | 最小并发数 |
| `DEEPSEEK_MAX_WORKERS` | `16` | 最大并发数 |
| `DEEPSEEK_KEEPALIVE_TIMEOUT` | `600` | 等待推理开始最大时长（秒） |
| `DEEPSEEK_INFER_TIMEOUT` | `300` | 推理完成最大时长（秒） |
| `CHUNK_MAX_CHARS` | `2000` | 文本分块最大字符数（可用环境变量覆盖） |
| `DEEPSEEK_MAX_TOKENS` | `8192` | 单次回复 `max_tokens`；设为 `0` 则不传参、用接口默认 |
| `DEEPSEEK_JSON_REPAIR_ON_FAIL` | `1`（开启） | 本地解析失败时是否在末次尝试调用模型纠错；`0`/`false`/`no` 关闭 |
| `ATOMIC_DEBUG_PARSE_LOG` | 关闭 | 设为 `1`/`true`/`yes` 时写入 NDJSON 与 `atomic-json-parse-failures.log`（已 `.gitignore`） |
| `ATOMIC_DEBUG_SESSION_ID` | 空 | 可选；写入调试 NDJSON 的 `sessionId` 字段 |
| `HIERARCHY_ANALYSIS_ENABLED` | `True` | 启用模板层级学习 |
| `TEMPLATE_PATH` | `合并_正文标注_*.docx` | 默认模板路径 |

## 输出格式

DeepSeek 输出的 JSON block 结构：

```json
[
  {
    "type": "heading",
    "level": 2,
    "text": "1. Introduction"
  },
  {
    "type": "annotation",
    "level": 3,
    "cn_subtitle": "【三元融合催化研究背景】",
    "en_text": "The development of trifusion catalysts has attracted significant attention..."
  }
]
```

渲染为 Word 文档时：
- `heading` → Heading {level} 样式 + `profile.style_map[level]` run 级字体覆写
- `annotation.cn_subtitle` → Heading {level} 样式 + `profile.style_map[level]` run 级字体覆写
- `annotation.en_text` → Normal 样式 + `profile.en_text_style` run 级字体覆写

## 测试

```bash
python test_integration.py
```

74 项断言覆盖：层级分析、众数聚合、prompt 构建、JSON 校验、DOCX 渲染、run 级字体保真验证、自适应并发、文本分块、outlineLvl 写入验证等。

## 依赖

- **Doc2X API** — PDF 到 Markdown 转换（需付费 API Key）
- **DeepSeek API** — 原子化切分与中文标注（需 API Key）
- **python-docx** — Word 文档读写与 OxmlElement 操作
- **openai** — AsyncOpenAI 客户端
- **httpx** — 异步 HTTP 传输层
