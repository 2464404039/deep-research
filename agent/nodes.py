"""LangGraph 节点函数 —— 每个节点对应一个 Agent 的执行逻辑"""
import json
import logging
import re
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from .state import ResearchState

logger = logging.getLogger("deepresearch")

# ── JSON 解析工具 ──────────────────────────────────────────────


def _extract_json_block(text: str) -> str | None:
    """从 LLM 输出中提取 JSON——支持 code fence 和裸 JSON"""
    if not text:
        return None
    # 优先提取 ```json ... ``` 中的内容
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 尝试直接找 { ... } 块
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return m.group(0).strip()
    return None


def _load_json(text: str | None, fallback: dict | None = None) -> dict:
    """安全解析 JSON，失败返回 fallback"""
    if fallback is None:
        fallback = {}
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = _extract_json_block(text)
        if extracted:
            try:
                return json.loads(extracted)
            except json.JSONDecodeError:
                pass
    return fallback


# ── 意图检测 ────────────────────────────────────────────────────

_RESEARCH_KEYWORDS = [
    "调研", "分析", "对比", "方案", "趋势", "预测",
    "深度", "多角度", "全面", "评估", "研究", "展望",
    "优缺点", "利弊", "区别", "哪个好",
    "行业", "市场", "前景", "发展", "策略", "规划",
]

_FOLLOWUP_PATTERNS = [
    "根据这个", "根据上面", "根据刚才", "基于这个", "基于上面", "基于刚才",
    "刚才那个", "上面那个", "那个报告", "这个报告", "之前的",
    "那你觉得", "你觉得", "你认为", "你怎么看",
    "给我建议", "有什么建议", "推荐一下",
    "总结一下", "概括一下", "帮我整理",
    "根据报告",
]


def _detect_intent(query: str) -> str:
    """基于关键词的意图预判。如果命中追问模式，直接返回 direct"""
    # 追问/建议/总结类 → 不需要重新搜索
    for pat in _FOLLOWUP_PATTERNS:
        if pat in query:
            return "direct"
    # 研究类关键词 → 可能需要深度搜索
    for kw in _RESEARCH_KEYWORDS:
        if kw in query:
            return "multiagent"
    return "direct"


# ── 搜索词生成 ──────────────────────────────────────────────────

_FALLBACK_TEMPLATES = [
    "{} 最新趋势",
    "{} 是什么",
    "{} 深入分析",
    "{} 对比",
    "{} 案例",
]


def _derive_search_queries(query: str) -> list[str]:
    """当 LLM 未能生成搜索词时，用模板生成备选（自动注入当前年份确保时效性）"""
    year = datetime.now().year
    queries = []
    for t in _FALLBACK_TEMPLATES[:3]:
        q = t.format(query)
        # 确保包含当前年份的时间限定
        if str(year) not in q:
            q = f"{q} {year}"
        queries.append(q)
    return queries


# ── 辅助：注入记忆上下文 ────────────────────────────────────────


def _with_memory(state: ResearchState, content: str) -> str:
    mc = state.get("memory_context", "").strip()
    if mc:
        return f"{content}\n\n[跨会话记忆]\n{mc}"
    return content


# ── 辅助：构建源索引 ───────────────────────────────────────────


def _build_source_index(evidence: list[dict]) -> list[dict]:
    """从 evidence 列表生成格式化源索引"""
    index = []
    for item in evidence:
        sid = item.get("source_id", "")
        index.append({
            "source_id": sid,
            "label": f"[{sid}] {item.get('title', '未知来源')[:50]}",
            "url": item.get("url", ""),
            "source_type": item.get("source_type", "web"),
        })
    return index


def _extract_supplement_sources(messages: list) -> list[dict]:
    """从 agent 消息历史中提取 search_supplement 工具返回的搜索结果，
    解析为 source_index 条目（source_id 格式: SUPP-1, SUPP-2, ...）"""
    import json as _json
    entries = []
    supp_idx = 0
    for msg in messages:
        if hasattr(msg, "type") and msg.type == "tool":
            content = str(getattr(msg, "content", ""))
            if not content or "补充搜索结果" not in content:
                continue
            url_matches = re.findall(r'URL:\s*(https?://[^\s\n]+)', content)
            title_matches = re.findall(r'\*\*(.+?)\*\*', content)
            for i, url in enumerate(url_matches):
                supp_idx += 1
                title = title_matches[i] if i < len(title_matches) else url.split("/")[2]
                entries.append({
                    "source_id": f"SUPP-{supp_idx}",
                    "label": f"[SUPP-{supp_idx}] {title[:50]}",
                    "url": url,
                    "source_type": "web",
                })
    return entries


# ── 节点函数 ────────────────────────────────────────────────────


def intent_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """意图路由：分类为 direct（简单问答）或 multiagent（深度研究）"""
    keyword_route = _detect_intent(state["query"])

    # 关键词判断为 direct（无研究词汇）→ 直接走，LLM 无权推翻
    if keyword_route == "direct":
        logger.info("[intent] 关键词=direct，跳过 LLM 路由")
        return {"intent": "direct", "messages": []}

    # 关键词判断为 multiagent → 让 LLM 确认（避免误伤带研究意图的无关键词 query）
    human = HumanMessage(content=f"判断以下问题的路由：{state['query']}")
    try:
        result = agent.invoke({"messages": [human]})
        last_msg = result["messages"][-1]
        payload = _load_json(last_msg.content, {"route": keyword_route, "reason": "关键词匹配"})
        route = str(payload.get("route", keyword_route)).strip().lower()
        if route not in {"direct", "multiagent"}:
            route = keyword_route
    except Exception:
        route = keyword_route

    logger.info("[intent] 路由=%s (关键词预判=%s)", route, keyword_route)
    return {"intent": route, "messages": [human]}


def direct_answer_node(state: ResearchState, llm: Any, name: str) -> dict:
    """简单问答：裸 LLM 调用，跳过 Agent 层"""
    from langchain_core.messages import HumanMessage, SystemMessage

    # 注入记忆上下文（让 LLM 知道之前聊过什么）
    mc = state.get("memory_context", "").strip()
    system_text = "你是一个友好的AI助手。简短自然地回复用户的问题，不要拉长回复。"
    if mc:
        system_text += f"\n\n[对话记忆]\n{mc}"

    human = HumanMessage(content=state["query"])
    system = SystemMessage(content=system_text)
    try:
        result = llm.invoke([system, human])
        final = result.content
    except Exception as exc:
        final = f"抱歉，处理请求时出错：{exc}"

    logger.info("[direct_answer] 完成")
    return {"final": final, "messages": [human]}


def plan_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """规划：拆解问题，生成搜索词列表"""
    today = datetime.now().strftime("%Y年%m月%d日")
    human = HumanMessage(
        content=_with_memory(state, f"当前日期：{today}\n\n需要研究的问题：{state['query']}")
    )
    try:
        result = agent.invoke({"messages": [human]})
        payload = _load_json(result["messages"][-1].content)
        plan_text = str(payload.get("plan_text", ""))
        search_queries = list(payload.get("search_queries", []))
    except Exception:
        plan_text = f"对「{state['query']}」进行多角度研究"
        search_queries = []

    if not search_queries:
        search_queries = _derive_search_queries(state["query"])

    # 限制搜索词数量（放宽到9个以覆盖对比类多维度查询）
    search_queries = search_queries[:9]

    logger.info("[plan] 搜索词(%d): %s", len(search_queries), " | ".join(search_queries))
    return {
        "plan": plan_text,
        "search_queries": search_queries,
        "iteration": 0,
        "messages": [human],
    }


def web_scout_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """网络搜索：批量搜索 → 去重 → 摘要给 LLM → LLM 用 fetch_page 工具自主抓取 → 提取证据"""
    from .tools import search_all, filter_defunct, enrich_records_with_content

    # 优先使用 analyst 生成的细化搜索词（针对性补搜），否则用规划器的原始搜索词
    refined = state.get("refined_queries", [])
    if refined:
        queries = refined
        logger.info("[web_scout] 使用 analyst 细化搜索词(%d): %s", len(queries), " | ".join(queries))
    else:
        queries = state["search_queries"]
    next_iteration = state["iteration"] + 1
    raw_records = search_all(queries)
    today = datetime.now().strftime("%Y年%m月%d日")
    raw_records = filter_defunct(raw_records)

    if not raw_records:
        logger.info("[web_scout] 无搜索结果")
        return {
            "search_results": "（无搜索结果）",
            "evidence": [],
            "source_index": [],
            "messages": [],
            "iteration": next_iteration,
        }

    # 统计搜索源分布
    sources = {}
    for rec in raw_records:
        s = rec.get("source", "unknown")
        sources[s] = sources.get(s, 0) + 1
    logger.info("[web_scout] 搜索源分布: %s，共 %d 条", dict(sources), len(raw_records))

    # ── 构建摘要 prompt（不含全文，让 LLM 用 fetch_page 工具自主选择）──
    lines = ["以下是搜索到的网页记录（仅摘要）。你可以使用 fetch_page 工具获取任意 URL 的完整正文："]
    for rec in raw_records:
        pub_date = rec.get("date", "")
        date_info = f"发布日期: {pub_date}" if pub_date else "发布日期: 未知"
        lines.append(
            f"source_id: {rec['source_id']}\n"
            f"title: {rec['title']}\n"
            f"url: {rec['url']}\n"
            f"domain: {rec.get('domain', '')}\n"
            f"{date_info}\n"
            f"摘要: {rec['snippet'][:400]}\n"
            f"---"
        )
    raw_text = "\n".join(lines)

    human = HumanMessage(
        content=f"当前日期：{today}\n\n用户问题：{state['query']}\n\n{raw_text}\n\n"
                f"请先根据摘要判断哪些页面最相关，然后用 fetch_page 工具只抓取 3-5 篇最关键的页面全文来提炼证据。"
    )

    try:
        result = agent.invoke({"messages": [human]})
        payload = _load_json(result["messages"][-1].content)
        evidence = list(payload.get("evidence", []))
        source_index = list(payload.get("source_index", []))
    except Exception:
        evidence = []
        source_index = []

    # ── Fallback: 检查 LLM 是否调用了 fetch_page ──
    tool_messages = [
        m for m in result.get("messages", [])
        if hasattr(m, "tool_calls") and m.tool_calls
    ]
    fetch_calls = [
        tc for msg in tool_messages
        for tc in (msg.tool_calls if isinstance(msg.tool_calls, list) else [])
        if tc.get("name") == "fetch_page_tool"
    ]
    logger.info("[web_scout] LLM fetch_page 调用: %d 次", len(fetch_calls))

    # Fallback 1: LLM 一次都没调 fetch_page → Python 盲抓前 5 条
    if not fetch_calls and not evidence:
        logger.info("[web_scout] LLM 未调用 fetch_page，fallback 抓取前5条")
        raw_records = enrich_records_with_content(raw_records, max_fetch=5)
        for rec in raw_records[:8]:
            content = rec.get("full_content", "") or rec.get("snippet", "")
            evidence.append({
                "source_id": rec["source_id"],
                "title": rec["title"],
                "url": rec["url"],
                "snippet": content[:400],
                "domain": rec.get("domain", ""),
                "date": rec.get("date", ""),
            })

    # Fallback 2: LLM 调了 fetch_page 但没产出 evidence → 从 tool results 拼
    if fetch_calls and not evidence:
        logger.info("[web_scout] LLM fetch_page 成功但未产出JSON，从 tool results fallback")
        for msg in result.get("messages", []):
            if hasattr(msg, "type") and msg.type == "tool" and hasattr(msg, "content"):
                content = str(msg.content)
                if content and len(content) > 50:
                    # 尝试从 tool result 中提取 URL
                    url_match = re.search(r'https?://[^\s\n]{10,}', content[:200])
                    fallback_url = url_match.group(0) if url_match else ""
                    evidence.append({
                        "source_id": f"WEB-FB-{len(evidence) + 1}",
                        "title": fallback_url.split("/")[2] if fallback_url else "抓取页面",
                        "url": fallback_url,
                        "snippet": content[:500],
                        "domain": fallback_url.split("/")[2] if fallback_url else "",
                        "date": today,
                    })

    if not source_index:
        source_index = _build_source_index(evidence)

    logger.info("[web_scout] 证据(%d) 源(%d)", len(evidence), len(source_index))
    return {
        "search_results": raw_text,
        "evidence": evidence,
        "source_index": source_index,
        "messages": [human],
        "iteration": next_iteration,
    }


def analyst_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """分析：评估证据质量 → 形成结论 → 判断是否需要补充搜索（可用 search_supplement 工具）"""
    evidence_text = json.dumps(state["evidence"], ensure_ascii=False, indent=2)
    source_text = json.dumps(state["source_index"], ensure_ascii=False, indent=2)

    today = datetime.now().strftime("%Y年%m月%d日")
    human = HumanMessage(
        content=_with_memory(
            state,
            f"当前日期：{today}\n\n"
            f"用户问题：{state['query']}\n\n"
            f"研究计划：{state['plan']}\n\n"
            f"当前迭代：第{state['iteration'] + 1}轮（共{state['max_iterations']}轮）\n\n"
            f"证据列表：\n{evidence_text}\n\n"
            f"来源索引：\n{source_text}\n\n"
            f"请分析证据，形成结论。如果发现某个维度缺少具体数据，先用 search_supplement 工具定向补搜。"
            f"如果当前是最后一轮迭代，即使补搜后证据不足也要设置 needs_more_research: false。",
        )
    )

    try:
        result = agent.invoke({"messages": [human]})
        payload = _load_json(result["messages"][-1].content)
        analysis = str(payload.get("analysis_text", ""))
        needs_more = bool(payload.get("needs_more_research", False))
        findings = list(payload.get("findings", []))
        evidence_scores = list(payload.get("evidence_scores", []))
        refined_queries = list(payload.get("suggested_queries", []))
    except Exception:
        analysis = "分析过程中出现错误，请参考以下证据。"
        needs_more = False
        findings = []
        evidence_scores = []
        refined_queries = []
        result = None

    # ── 提取 search_supplement 工具返回的补充来源 ──
    supp_sources = []
    if result is not None:
        agent_messages = result.get("messages", [])
        supp_sources = _extract_supplement_sources(agent_messages)
        # 统计 tool_calls 中 search_supplement 的调用次数
        supp_calls = sum(
            1 for msg in agent_messages
            if hasattr(msg, "tool_calls") and isinstance(msg.tool_calls, list)
            for tc in msg.tool_calls if tc.get("name") == "search_supplement_tool"
        )
        if supp_calls:
            logger.info("[analyst] search_supplement 调用: %d 次, 新增来源: %d 个",
                        supp_calls, len(supp_sources))

    # 合并补充来源到 source_index
    merged_source_index = list(state["source_index"])
    if supp_sources:
        merged_source_index.extend(supp_sources)

    # 达到最大迭代次数时强制停止
    if state["iteration"] >= state["max_iterations"]:
        needs_more = False

    if not analysis:
        analysis = f"共收集到 {len(state['evidence'])} 条证据，涵盖 {len(merged_source_index)} 个来源。"

    # 统计评分
    if evidence_scores:
        high = sum(1 for s in evidence_scores if s.get("reliability", 0) >= 0.7)
        low = sum(1 for s in evidence_scores if s.get("reliability", 0) < 0.4)
        logger.info("[analyst] 证据评分: 高信度=%d 低信度=%d | 需要更多研究=%s | findings=%d",
                    high, low, needs_more, len(findings))
    else:
        logger.info("[analyst] 需要更多研究=%s | findings=%d", needs_more, len(findings))

    # ── 硬过滤：低评分证据直接丢弃，不足时强制补充搜索 ──
    if evidence_scores:
        # 建立 source_id → reliability 索引
        score_map = {s["source_id"]: s.get("reliability", 0) for s in evidence_scores}

        # 过滤 evidence：仅保留 reliability >= 0.4 的
        original_evidence = state["evidence"]
        filtered_evidence = [
            e for e in original_evidence
            if score_map.get(e.get("source_id", ""), 0) >= 0.4
        ]
        dropped = len(original_evidence) - len(filtered_evidence)
        if dropped:
            logger.info("[analyst] 硬过滤丢弃 %d 条低信度证据 (reliability<0.4)", dropped)

        # 过滤 evidence_scores：仅保留有对应证据的评分（排除 SUPP 等孤儿来源的评分）
        evidence_ids = {e["source_id"] for e in filtered_evidence}
        filtered_scores = [
            s for s in evidence_scores
            if s.get("reliability", 0) >= 0.4
            and s.get("source_id", "") in evidence_ids
        ]

        # 过滤 source_index：只保留在过滤后 evidence 中出现的 + 所有补搜来源
        valid_ids = {e["source_id"] for e in filtered_evidence}
        filtered_source_index = [
            si for si in merged_source_index
            if si.get("source_id", "") in valid_ids or str(si.get("source_id", "")).startswith("SUPP-")
        ]
        dropped_si = len(merged_source_index) - len(filtered_source_index)
        if dropped_si:
            logger.info("[analyst] source_index 同步裁剪 %d 条", dropped_si)

        # 检查是否证据充足：至少 3 条高信度(reliability≥0.7)或 5 条中信度(reliability≥0.4)
        high_quality = sum(1 for s in filtered_scores if s.get("reliability", 0) >= 0.7)
        total_quality = len(filtered_scores)
        insufficient = (high_quality < 3 and total_quality < 5)

        if insufficient and state["iteration"] < state["max_iterations"]:
            logger.info("[analyst] 证据不足(高信度%d条/总计%d条)，强制补充搜索", high_quality, total_quality)
            needs_more = True
    else:
        filtered_evidence = state["evidence"]
        filtered_scores = evidence_scores
        filtered_source_index = merged_source_index
        insufficient = True
        if state["iteration"] < state["max_iterations"]:
            needs_more = True

    if refined_queries:
        logger.info("[analyst] 生成细化搜索词(%d): %s", len(refined_queries), " | ".join(refined_queries))

    return {
        "analysis": analysis,
        "needs_more_research": needs_more,
        "refined_queries": refined_queries,
        "findings": findings,
        "evidence": filtered_evidence,
        "evidence_scores": filtered_scores,
        "source_index": filtered_source_index,
        "messages": [human],
    }


def writer_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """撰写：生成最终的 Markdown 研究报告"""
    findings_text = json.dumps(state["findings"], ensure_ascii=False, indent=2)
    source_text = json.dumps(state["source_index"], ensure_ascii=False, indent=2)

    today = datetime.now().strftime("%Y年%m月%d日")
    human = HumanMessage(
        content=(
            f"当前日期：{today}\n\n"
            f"研究问题：{state['query']}\n\n"
            f"研究计划：{state['plan']}\n\n"
            f"分析结论：{state['analysis']}\n\n"
            f"关键发现：\n{findings_text}\n\n"
            f"可用来源：\n{source_text}\n\n"
            f"请撰写一份完整的 Markdown 研究报告。"
        )
    )

    try:
        result = agent.invoke({"messages": [human]})
        report = result["messages"][-1].content
    except Exception as exc:
        report = f"报告生成失败：{exc}\n\n## 分析摘要\n\n{state['analysis']}"

    # 清理可能的 JSON 残余
    report = re.sub(r"^```(?:markdown|json)?\s*\n?", "", report)
    report = re.sub(r"\n?```\s*$", "", report)

    logger.info("[writer] 报告完成 | 长度=%d字", len(report))
    return {
        "draft": report,
        "final": report,
        "messages": [human],
    }


def _build_reference_section(source_index: list[dict], report: str) -> str:
    """构建参考资料部分——列出所有搜索到的来源"""
    if not source_index:
        return ""

    lines = ["---\n", "## 📚 参考资料\n"]
    for item in source_index:
        label = item.get("label", "")
        url = item.get("url", "")
        title = label or item.get("source_id", "未知来源")
        # 清理 label 中的 source_id 前缀
        title = re.sub(r'^\[.*?\]\s*', '', title).strip()
        if url:
            lines.append(f"- [{title}]({url})")
        else:
            lines.append(f"- {title}")

    return "\n".join(lines)
