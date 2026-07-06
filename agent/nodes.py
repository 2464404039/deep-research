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
    "调研", "分析", "对比", "报告", "方案", "趋势", "预测",
    "深度", "多角度", "全面", "评估", "研究", "展望",
    "优缺点", "利弊", "区别", "哪个好", "推荐", "建议",
    "行业", "市场", "前景", "发展", "策略", "规划",
]


def _detect_intent(query: str) -> str:
    """基于关键词的意图预判"""
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


# ── 节点函数 ────────────────────────────────────────────────────


def intent_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """意图路由：分类为 direct（简单问答）或 multiagent（深度研究）"""
    keyword_route = _detect_intent(state["query"])
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

    logger.info("[intent] 路由=%s", route)
    return {"intent": route, "messages": [human]}


def direct_answer_node(state: ResearchState, agent: Any, agent_name: str) -> dict:
    """简单问答：直接回复用户"""
    human = HumanMessage(content=state["query"])
    try:
        result = agent.invoke({"messages": [human]})
        final = result["messages"][-1].content
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
    """网络搜索：批量搜索 → 去重 → LLM 提取结构化证据"""
    from .tools import search_all

    queries = state["search_queries"]
    next_iteration = state["iteration"] + 1
    raw_records = search_all(queries)

    # 软过滤：仅移除明确失效/404的内容，时效性交由分析师Agent判断
    from .tools import filter_defunct, enrich_records_with_content
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
    logger.info("[web_scout] 搜索源分布: %s", dict(sources))

    # 抓取前10条结果的完整页面内容
    raw_records = enrich_records_with_content(raw_records, max_fetch=10)

    # 构建输入文本给 LLM（包含完整页面内容和发布日期）
    lines = ["以下是搜索到的网页记录："]
    for rec in raw_records:
        full = rec.get("full_content", "")
        pub_date = rec.get("date", "")
        date_info = f"发布日期: {pub_date}" if pub_date else "发布日期: 未知"
        content_block = f"完整内容: {full[:2000]}" if full else f"摘要: {rec['snippet'][:300]}"
        lines.append(
            f"source_id: {rec['source_id']}\n"
            f"title: {rec['title']}\n"
            f"url: {rec['url']}\n"
            f"{date_info}\n"
            f"{content_block}\n"
            f"---"
        )
    raw_text = "\n".join(lines)

    human = HumanMessage(
        content=f"当前日期：{today}\n\n用户问题：{state['query']}\n\n{raw_text}\n\n请从以上网页记录中提取与研究问题相关的证据。"
    )

    try:
        result = agent.invoke({"messages": [human]})
        payload = _load_json(result["messages"][-1].content)
        evidence = list(payload.get("evidence", []))
        source_index = list(payload.get("source_index", []))
    except Exception:
        evidence = []
        source_index = []

    # 如果 LLM 没提取出证据，直接用原始记录做 fallback
    if not evidence:
        for rec in raw_records[:8]:
            evidence.append({
                "source_id": rec["source_id"],
                "title": rec["title"],
                "url": rec["url"],
                "snippet": rec["snippet"][:300],
                "domain": rec.get("domain", ""),
                "date": rec.get("date", ""),
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
    """分析：评估证据质量 → 形成结论 → 判断是否需要补充搜索"""
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
            f"请分析证据，形成结论。如果当前是最后一轮迭代，即使证据不足也要设置 needs_more_research: false。",
        )
    )

    try:
        result = agent.invoke({"messages": [human]})
        payload = _load_json(result["messages"][-1].content)
        analysis = str(payload.get("analysis_text", ""))
        needs_more = bool(payload.get("needs_more_research", False))
        findings = list(payload.get("findings", []))
        evidence_scores = list(payload.get("evidence_scores", []))
    except Exception:
        analysis = "分析过程中出现错误，请参考以下证据。"
        needs_more = False
        findings = []
        evidence_scores = []

    # 达到最大迭代次数时强制停止
    if state["iteration"] >= state["max_iterations"]:
        needs_more = False

    if not analysis:
        analysis = f"共收集到 {len(state['evidence'])} 条证据，涵盖 {len(state['source_index'])} 个来源。"

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

        # 过滤 evidence_scores
        filtered_scores = [s for s in evidence_scores if s.get("reliability", 0) >= 0.4]

        # 过滤 source_index：只保留在过滤后 evidence 中出现的
        valid_ids = {e["source_id"] for e in filtered_evidence}
        filtered_source_index = [
            si for si in state["source_index"]
            if si.get("source_id", "") in valid_ids
        ]
        dropped_si = len(state["source_index"]) - len(filtered_source_index)
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
        filtered_source_index = state["source_index"]
        insufficient = True
        if state["iteration"] < state["max_iterations"]:
            needs_more = True

    return {
        "analysis": analysis,
        "needs_more_research": needs_more,
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
