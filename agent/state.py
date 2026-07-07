"""研究状态 —— 15个字段，在 LangGraph 节点间流转"""
import operator
from typing import Annotated, List, TypedDict

from langchain_core.messages import BaseMessage


class ResearchState(TypedDict):
    query: str
    user_id: str
    memory_context: str
    messages: Annotated[List[BaseMessage], operator.add]
    intent: str                        # "direct" | "multiagent"
    plan: str                          # 研究计划文本
    search_queries: List[str]          # 搜索词列表
    refined_queries: List[str]         # analyst 生成的细化搜索词（用于补充搜索迭代）
    search_results: str                # 原始搜索结果文本
    evidence: List[dict]               # [{source_id, title, url, snippet, domain}]
    source_index: List[dict]           # [{source_id, label, url, source_type}]
    analysis: str                      # 分析结论文本
    needs_more_research: bool          # 是否需要补充搜索
    findings: List[dict]               # [{claim, source_ids, confidence}]
    evidence_scores: List[dict]        # [{source_id, relevance, freshness, authority, reliability, reason}]
    draft: str
    final: str
    iteration: int
    max_iterations: int


def create_initial_state(
    query: str,
    max_iterations: int = 2,
    user_id: str = "default",
    memory_context: str = "",
) -> ResearchState:
    return ResearchState(
        query=query,
        user_id=user_id,
        memory_context=memory_context,
        messages=[],
        intent="",
        plan="",
        search_queries=[],
        refined_queries=[],
        search_results="",
        evidence=[],
        source_index=[],
        analysis="",
        needs_more_research=False,
        findings=[],
        evidence_scores=[],
        draft="",
        final="",
        iteration=0,
        max_iterations=max_iterations,
    )
