# Atomic Segmentation — 学术论文 PDF 原子化切分标注管线

将英文学术论文 PDF 自动解析为结构化的 Word 文档：每个英文原子句配对一个高度概括的中文小标题，保持原论文的层级结构，输出文档可直接被 WPS/Word 目录识别。

## 管线流程

```
PDF ─── Doc2X ──→ Markdown ─── 分块 ──→ DeepSeek API ──→ JSON blocks ──→ DOCX
                                  ↑                            ↓
                           模板层级学习 ←── 示例 .docx ──→ HierarchyProfile
```

1. **Doc2X 解析** — 上传 PDF 至 Doc2X API，获取带标题层级的 Markdown 文本。
2. **Markdown 分块** — 按标题拆分章节，自动过滤参考文献等非正文部分，超长段落按句子边界切分。
3. **模板层级学习** — 分析示例 Word 文档，提取各层级的角色、字体样式和层级转换模式，构建 `HierarchyProfile`。
4. **DeepSeek 原子化** — 异步流式调用 DeepSeek API，基于 `HierarchyProfile` 动态构建 prompt，将每个章节切分为带中文小标题的原子句 JSON 数组。
5. **DOCX 渲染** — 加载模板保留样式定义，按 JSON blocks 的 type/level 动态排版，强制写入 `w:outlineLvl` 确保 WPS 目录兼容。

## 项目结构

```
├── main.py                 # 主入口：管线编排、异步并发控制、进度跟踪
├── config.py               # 所有配置项（API 密钥从 .env 读取）
├── doc2x_client.py         # Doc2X API 客户端（PDF → Markdown）
├── deepseek_client.py      # DeepSeek API 客户端（异步流式 + 层级感知 JSON）
├── hierarchy_analyzer.py   # 模板层级分析器（.docx → HierarchyProfile）
├── docx_generator.py       # Word 渲染引擎（JSON blocks → .docx）
├── test_integration.py     # 集成测试套件
├── requirements.txt        # Python 依赖
└── .env                    # API 密钥（不纳入版本控制）
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API 密钥

在项目根目录创建 `.env` 文件：

```
DOC2X_API_KEY=your_doc2x_api_key
DEEPSEEK_API_KEY=your_deepseek_api_key
```

### 3. 准备模板文档

将已标注好层级结构的示例 Word 文档放在项目目录下。默认模板路径在 `config.py` 中通过 `TEMPLATE_PATH` 指定。

### 4. 运行

```bash
python main.py paper.pdf
python main.py paper.pdf -o output.docx
python main.py paper.pdf --template custom_template.docx
```

## 核心特性

### 自适应并发

根据 DeepSeek API 响应延迟动态调整并发数（默认 4–16），应对高流量场景下的 keep-alive 等待：

- 平均延迟 < 5s → 自动提高并发
- 平均延迟 > 30s → 自动降低并发
- 并发范围可通过环境变量 `DEEPSEEK_MIN_WORKERS` / `DEEPSEEK_MAX_WORKERS` 调节

### 流式传输

启用 `stream=True` 接收 DeepSeek 响应，正确处理高流量时的 SSE keep-alive 注释，避免 10 分钟超时断连。

### WPS 目录兼容

通过底层 OxmlElement 操作，对每个段落强制写入 `w:outlineLvl` 属性：

- Heading 段落：`outlineLvl` = level - 1（0-based）
- Normal 段落：`outlineLvl` = 9（明确标记为正文）
- Heading 1–6 样式定义中同步写入对应的 `outlineLvl`

### 容错与降级

- DeepSeek JSON 解析失败时自动插入占位符 block，标记需人工处理的章节
- API 调用失败自动指数退避重试（最多 3 次）
- 400 错误（不可重试）直接降级为占位符

## 配置参考

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `DEEPSEEK_STREAM_ENABLED` | `True` | 启用流式传输 |
| `DEEPSEEK_MIN_WORKERS` | `4` | 最小并发数 |
| `DEEPSEEK_MAX_WORKERS` | `16` | 最大并发数 |
| `DEEPSEEK_KEEPALIVE_TIMEOUT` | `600s` | 等待推理开始的最大时长 |
| `DEEPSEEK_INFER_TIMEOUT` | `300s` | 推理完成最大时长 |
| `CHUNK_MAX_CHARS` | `2000` | 长段落分块字符上限 |
| `HIERARCHY_ANALYSIS_ENABLED` | `True` | 启用模板层级学习 |

## 测试

```bash
python -m pytest test_integration.py -v
```

## 层级分析调试

独立运行层级分析器查看模板结构：

```bash
python hierarchy_analyzer.py template.docx
```
