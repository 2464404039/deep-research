"""LangGraph 工作流定义 —— 6个节点，条件路由"""
from dataclasses import dataclass
from functools import partial

from langgraph.graph import END, START, StateGraph

from .nodes import (
    analyst_node,
    direct_answer_node,
    intent_node,
    plan_node,
    web_scout_node,
    writer_node,
)
from .state import ResearchState


@dataclass
class AgentBundle:
    intent_router: any
    planner: any
    web_scout: any
    analyst: any
    writer: any
    direct_llm: any = None  # 裸 LLM，用于简单问答（跳过 Agent 层）


def _bind(node_fn, agent, name):
    """将 agent 绑定到节点函数"""
    def wrapper(state: ResearchState) -> dict:
        return node_fn(state, agent, name)
    wrapper.__name__ = name
    return wrapper


def _route_after_intent(state: ResearchState) -> str:
    intent = state.get("intent", "direct")
    if intent == "direct":
        return "direct"
    return "plan"


def _should_continue(state: ResearchState) -> str:
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 2)

    if iteration > max_iter:
        return "writer"
    if state.get("needs_more_research"):
        return "web_scout"
    return "writer"


def build_graph(agents: AgentBundle, checkpointer=None):
    """构建并编译 LangGraph 工作流"""
    workflow = StateGraph(ResearchState)

    # 添加节点
    workflow.add_node("intent", _bind(intent_node, agents.intent_router, "intent_router"))
    workflow.add_node("direct_answer", _bind(direct_answer_node, agents.direct_llm or agents.writer, "direct_llm"))
    workflow.add_node("plan", _bind(plan_node, agents.planner, "planner"))
    workflow.add_node("web_scout", _bind(web_scout_node, agents.web_scout, "web_scout"))
    workflow.add_node("analyst", _bind(analyst_node, agents.analyst, "analyst"))
    workflow.add_node("writer", _bind(writer_node, agents.writer, "writer"))

    # 边和条件路由
    workflow.add_edge(START, "intent")

    workflow.add_conditional_edges(
        "intent",
        _route_after_intent,
        {"direct": "direct_answer", "plan": "plan"},
    )

    workflow.add_edge("plan", "web_scout")
    workflow.add_edge("web_scout", "analyst")

    workflow.add_conditional_edges(
        "analyst",
        _should_continue,
        {"writer": "writer", "web_scout": "web_scout"},
    )

    workflow.add_edge("direct_answer", END)
    workflow.add_edge("writer", END)

    return workflow.compile(checkpointer=checkpointer)
