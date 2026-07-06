# DeepResearch

轻量级多 Agent 深度研究助手 —— 自动搜索、分析、撰写带引用和可信度评分的专业研究报告。

## 架构

```
用户 Query → intent_router → { 简单问答, 深度研究 }
深度研究路径:
  planner → web_scout → analyst ⇄ writer → Markdown 报告
              ↑______________↓
           (证据不足时自动补充搜索)
```

### 5 个 AI Agent

| Agent | 职责 | 输出 |
|-------|------|------|
| Intent Router | 判断问题复杂度，路由到快速问答或深度研究 | `route` |
| Planner | 拆解问题维度，生成带时间限定 + 官网优先的搜索策略 | `search_queries[]` |
| Web Scout | 三源联邦搜索 → 去重 → 全文抓取 → 结构化证据提取 | `evidence[]`, `source_index[]` |
| Analyst | 三维度评分（相关性/时效性/权威性）→ 硬过滤低分证据 → 判断是否需要补充搜索 | `findings[]`, `evidence_scores[]` |
| Writer | 撰写带数据、表格、编号引用的 Markdown 研究报告 | `final` |

### 搜索系统

- **Bocha API** — 主力搜索，Year 时间窗口，提取 `datePublished` 元数据
- **DuckDuckGo** — 免费辅助，`timelimit='y'` 近一年结果
- **DeepSeek web_search** — LLM 级联网搜索，要求返回日期和最新信息
- 失效过滤（404/下架/deprecated），时效性交由 LLM Agent 评分判断

### 证据可信度模型

```
reliability = relevance × 0.4 + freshness × 0.3 + authority × 0.3
```
- 🟢 高信度 (≥0.7)：官网/政府/学术/权威媒体
- 🟡 中信度 (≥0.4)：企业网站/知名博客
- 🔴 低信度 (<0.4)：**自动丢弃**，不足时触发补充搜索

## 快速开始

### 本地运行

```bash
# 1. 克隆
git clone https://github.com/your-username/deepresearch.git
cd deepresearch

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 DEEPSEEK_API_KEY 和 BOCHA_API_KEY（可选）

# 4. CLI 模式
python main.py --query "对比 ChatGPT 和 DeepSeek 最新定价"

# 5. Web 服务
python server.py
# 浏览器打开 http://localhost:8764
```

### Docker 部署（推荐用于云服务器）

```bash
# 1. 克隆并配置
git clone https://github.com/your-username/deepresearch.git
cd deepresearch
cp .env.example .env
# 编辑 .env 填入真实 API Key

# 2. 启动
docker compose up -d

# 3. 查看日志
docker compose logs -f

# 4. 停止
docker compose down
```

服务默认监听 `http://<服务器IP>:8764`。

## 项目结构

```
deepresearch/
├── .env.example          # 配置模板
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py               # CLI 入口
├── server.py             # Web 服务 (FastAPI + SSE 流式)
├── agent/
│   ├── config.py         # 配置类 (pydantic-settings)
│   ├── state.py          # ResearchState 状态定义
│   ├── prompts.py        # 5 个 Agent 系统提示词
│   ├── tools.py          # 搜索工具 (博查/DDG/DeepSeek) + 过滤 + 抓取
│   ├── nodes.py          # 6 个 LangGraph 节点函数
│   └── graph.py          # StateGraph 工作流定义
├── memory/
│   └── store.py          # SQLite 对话记忆
├── static/
│   ├── index.html        # 前端页面
│   ├── app.js            # SSE 客户端 + Markdown 渲染
│   └── style.css         # 样式
└── data/                 # SQLite 数据库 (自动创建)
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | - | 默认为 `https://api.deepseek.com/v1` |
| `MODEL` | - | 默认为 `deepseek-chat` |
| `BOCHA_API_KEY` | - | 博查搜索 API Key（不填则仅用 DDG + DeepSeek） |
| `MAX_ITERATIONS` | - | 搜索-分析最大循环次数，默认 2 |
| `ENABLE_MEMORY` | - | 是否启用跨会话记忆，默认 true |
| `HOST` | - | 服务监听地址，默认 `0.0.0.0` |
| `PORT` | - | 服务端口，默认 `8764` |

## 技术栈

| 层 | 技术 |
|----|------|
| 编排 | LangGraph StateGraph |
| LLM | DeepSeek Chat API |
| 搜索 | 博查 API / DuckDuckGo / DeepSeek web_search |
| 存储 | SQLite (LangGraph Checkpoint + 记忆) |
| Web | FastAPI + SSE 流式传输 |
| 前端 | 原生 HTML/CSS/JS（零 JS 依赖） |

## License

MIT
