# DeepResearch 架构文档

## 整体架构

```
┌─────────────────────────────────────────────────────┐
│                    用户入口                          │
│         CLI (main.py)  │  Web (server.py)           │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│                  LangGraph 编排层                     │
│                                                     │
│   StateGraph(ResearchState)  19 个字段在节点间流转     │
│                                                     │
│   ┌─────────┐   ┌──────┐   ┌───────────┐           │
│   │ intent  │──▶│ plan │──▶│ web_scout │           │
│   └────┬────┘   └──────┘   └─────┬─────┘           │
│        │                         │                  │
│        │ direct_answer            ▼                  │
│        │                   ┌──────────┐             │
│        │              ┌───│ analyst  │──┐          │
│        │              │   └──────────┘  │          │
│        │              │       │         │          │
│        │              │  needs_more     │ 充分      │
│        │              │  refined_qry    │          │
│        │              └──────┘         │          │
│        │                               ▼          │
│        └──────────────────────────▶ writer         │
│                                     │              │
│                                     ▼              │
│                                    END             │
└─────────────────────────────────────────────────────┘
```

---

## 6 个节点

### intent — 意图路由

```
用户 query
    │
    ├── 关键词命中追问模式 (FOLLOWUP_PATTERNS)
    │   → 直接返回 direct，不调 LLM
    │
    └── 关键词未命中 → 调 Intent Router Agent
        └── 返回 {"route": "direct" | "multiagent"}
```

**路由规则**：
- `direct`：问候、常识问答、追问/建议/总结类、引用已生成报告的后续问题
- `multiagent`：需要最新信息、需精确横向对比、用户明确要求搜索

### direct_answer — 直接回复

```
裸 LLM (llm.invoke)，不经过 Agent tool-calling 层
        │
        ├── SystemMessage: "你是友好的AI助手..."
        ├── 注入 memory_context（对话历史）
        └── HumanMessage: query
        │
        ▼
    直接返回 final 文本
```

### plan — 研究规划

```
Planner Agent (无 Tool)
        │
        ├── 输入: query + 当前日期
        ├── LLM 推理: 拆解问题维度 × 版本 × 角度
        └── 输出 JSON: {plan_text, search_queries[]}
                │
                ▼
        8-10 个搜索词，含:
        - 精确月份时间限定 ("2026年7月")
        - 每个产品的每个版本单独搜索
        - 官网优先搜索词
        - latest news 类捕获最新动态
```

### web_scout — 网络取证

```
Web Scout Agent (search_tool + fetch_page_tool)
        │
        ├── 输入: search_queries (或 refined_queries)
        │
        ├── agent.invoke() 内部 LLM 自主循环:
        │   │
        │   ├── search_tool("query_1") → 三源搜索，返回摘要
        │   ├── search_tool("query_2") → ...
        │   ├── ... (每个搜索词一次)
        │   │
        │   ├── 审阅所有摘要，挑 3-5 篇
        │   ├── fetch_page_tool(url) → 抓全文 × 3-5
        │   └── 提炼 evidence JSON
        │
        └── Fallback: LLM 不调工具 → 硬编码 search_all + 盲抓
```

### analyst — 证据分析

```
Analyst Agent (search_supplement_tool)
        │
        ├── 输入: evidence[] + source_index[] + query + plan
        │
        ├── agent.invoke() 内部 LLM 自主循环:
        │   │
        │   ├── 对每条证据三维评分 (relevance × freshness × authority)
        │   ├── 发现缺口 → search_supplement_tool("缺少的维度") 补搜
        │   └── 输出 JSON: {evidence_scores[], findings[], needs_more, suggested_queries[]}
        │
        └── 节点函数后处理:
            ├── 硬过滤 reliability < 0.4 的证据
            ├── filtered_scores 与 filtered_evidence 对齐（排除 SUPP 孤儿评分）
            ├── 提取补搜结果 URL → source_index (SUPP-1, SUPP-2)
            ├── needs_more 且未达 max_iterations → refined_queries 回传 web_scout
            └── 否则 → 流转 writer
```

### writer — 报告撰写

```
Writer Agent (无 Tool)
        │
        ├── 输入: findings + analysis + source_index + query
        ├── 生成 Markdown 报告:
        │   ├── 版本细分对比表格（每个模型的每个版本单独一行）
        │   ├── 价格统一人民币 (¥)
        │   ├── 关键数字加粗
        │   ├── 证据不足时 ⚠️ 诚实声明
        │   └── 无引用标签（<sup> 等标记）
        └── 输出: final (Markdown 文本)
```

---

## 3 个 LangChain Tool

```
┌──────────────────────────────────────────────────────────┐
│                    Tool 调用全景                           │
│                                                          │
│   Web Scout                          Analyst             │
│   ─────────                          ───────             │
│                                                          │
│   search_tool(query)                 search_supplement   │
│   ├─ search_all([query], 4)          _tool(query)        │
│   ├─ 三源搜索 (博查+DDG+DS)          ├─ search_all(q, 3) │
│   ├─ 去重合并                        ├─ 仅返回摘要       │
│   └─ 返回: 格式化摘要文本             └─ 不抓全文        │
│       (source_id + 标题              (轻量快速补搜)       │
│        + URL + 摘要 + 日期)                              │
│                                                          │
│   fetch_page_tool(url)                                   │
│   ├─ urllib 抓取页面                                      │
│   ├─ 去 script/style/nav                                │
│   ├─ 截断 8000 字                                        │
│   └─ 返回: 纯文本正文                                     │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 搜索系统

```
search_all(queries, count_per)
    │
    ├── 对每个 query:
    │   ├── bocha_web_search(query, count)
    │   │   ├── freshness="Month" (博查 API)
    │   │   └── < 3 条 → fallback "Year"
    │   │
    │   ├── ddg_search(query, count)
    │   │   └── timelimit='m' (DuckDuckGo)
    │   │
    │   └── deepseek_web_search(query)
    │       └── 仅前 4 个搜索词 (省 API)
    │
    ├── URL 去重 (seen_urls set)
    ├── 分配 source_id (WEB-1, WEB-2, ...)
    └── 返回: [{source_id, title, url, snippet, domain, date, source}]
```

---

## 证据可信度模型

```
reliability = relevance × 0.4 + freshness × 0.3 + authority × 0.3
```

| 分数 | 等级 | 处理 |
|------|------|------|
| ≥ 0.7 | 🟢 高信度 | 优先采用 |
| ≥ 0.4 | 🟡 中信度 | 可用 |
| < 0.4 | 🔴 低信度 | 丢弃 |

**评分依据**：
- relevance：与问题的直接相关程度
- freshness：距当前日期远近，本月=1，半年=0.8，今年=0.7，3年=0.1
- authority：官网/gov/edu ≥ 0.9，知名媒体 0.7-0.8，博客/论坛 0.2-0.4

---

## 记忆系统

```
memory/store.py → MemoryStore (SQLite)

┌─────────────────┐     ┌──────────────────┐
│  conversations  │     │   user_profile    │
├─────────────────┤     ├──────────────────┤
│ user_id         │     │ user_id (PK)     │
│ thread_id       │     │ profile_json     │
│ role            │     │ updated_at       │
│ content         │     └──────────────────┘
│ created_at      │
└─────────────────┘

每次问答结束后 (persist_turn):

  1. 存消息 → conversations (user + assistant 各一条)
  2. 关键词提取画像:
     "我叫/我是/我做/我会/我毕业于/我住在..."
     → user_profile.facts = {"职业": "...", "技能": "..."}
  3. 压缩检查:
     if 消息数 > 20:
       旧消息 LLM 压缩为摘要 → [对话摘要] system 消息
       只保留最近 10 条

每次请求开始时 (build_memory_context):

  返回文本注入 prompt:
  ┌─────────────────────────────────┐
  │ [用户画像]                       │
  │ 已知信息: {"职业": "后端开发"}      │
  │ 用户偏好: {}                     │
  │                                  │
  │ [最近对话]                       │
  │ 用户: 分析2026年AI就业趋势         │
  │ AI: ...报告摘要前300字...          │
  └─────────────────────────────────┘
```

---

## 频率限制

```
auth.py → RateLimiter (纯内存，重启清零)

┌──────────────────────────────────────┐
│  acquire(ip)                         │
│                                      │
│  1. _is_local(ip) → True → 直接放行 │
│     (127.*, ::1, localhost,          │
│      192.168.*, 10.*, 172.16-31.*)   │
│                                      │
│  2. active_count ≥ 3 → 拒绝          │
│     "当前使用人数较多，请稍后再试"       │
│                                      │
│  3. IP 窗口内请求 ≥ 5 → 拒绝          │
│     "每小时最多 5 次深度研究"          │
│                                      │
│  4. 通过 → active_count++            │
│         记录时间戳                     │
└──────────────────────────────────────┘

release() → active_count--
```

---

## 创建和调用链

```
main.py / server.py
    │
    ├── ChatOpenAI(model, api_key, base_url)
    │   └── DeepSeek Chat API (OpenAI 兼容)
    │
    ├── create_agent(llm, tools=[...], system_prompt)
    │   ├── Intent Router:  tools=[]
    │   ├── Planner:        tools=[]
    │   ├── Web Scout:      tools=[search_tool, fetch_page_tool]
    │   ├── Analyst:        tools=[search_supplement_tool]
    │   └── Writer:         tools=[]
    │
    ├── AgentBundle(router, planner, web_scout, analyst, writer, direct_llm)
    │
    ├── build_graph(agents, checkpointer)
    │   └── StateGraph 6 节点 + 2 条件边 → CompiledGraph
    │
    ├── graph.stream(state, config)    ← server SSE 流式
    └── graph.invoke(state, config)    ← CLI 同步
```

---

## 前端 SSE 流式协议

```
POST /api/v1/research/stream
Body: {query, user_id?, thread_id?}

响应: text/event-stream

事件类型:

  phase (status="start")
    {type, node, status, message, icon}
    → 工作流日志: icon + message + spinner

  phase (status="done")
    {type, node, status, message, icon, detail}
    → 工作流日志: icon + message + 结果摘要
    → intent 完成: "简单问答" / "深度研究，启动多 Agent 协作"
    → plan 完成:    "生成 8 个搜索方向"
    → web_scout 完成: "搜索到 12 条证据，8 个来源"
    → analyst 完成:  "高信度 5/12 条，6 个关键结论"
    → writer 完成:   "共 1850 字"

  reloop
    {type, node="reloop", message, detail}
    → 分析师触发补充搜索循环 (黄色高亮条)

  final
    {type, query, final, source_index, evidence, evidence_scores, quality, quality_detail}
    → 渲染 Markdown 报告 + 来源列表 + 质量提示

  error
    {type, message}
    → 错误提示 (红色)
```

---

## 数据流总览

```
用户 query
    │
    ▼
┌──────────┐   memory_context (注入)    ┌──────────────┐
│  intent  │◄──────────────────────────│  MemoryStore │
└────┬─────┘                           └──────┬───────┘
     │ route                                   │
     ▼                                         │
┌──────────┐                                   │
│   plan   │                                   │
└────┬─────┘                                   │
     │ search_queries[8-10]                    │
     ▼                                         │
┌──────────┐  ┌────────────┐                  │
│web_scout │──│search_tool │→ 博查+DDG+DS     │
│          │──│fetch_page  │→ 网页全文         │
└────┬─────┘  └────────────┘                  │
     │ evidence[] + source_index[]             │
     ▼                                         │
┌──────────┐  ┌────────────────────┐          │
│ analyst  │──│search_supplement   │→ 补搜     │
│          │  └────────────────────┘          │
│          │  评分+过滤+refined_queries         │
└────┬─────┘                                   │
     │ findings[] + analysis                   │
     ▼                                         │
┌──────────┐                                   │
│  writer  │                                   │
└────┬─────┘                                   │
     │ final (Markdown)                        │
     ▼                                         │
  报告输出 ───── persist_turn ─────────────► 存记忆
```
