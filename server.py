"""DeepResearch —— FastAPI Web 服务 + SSE 流式输出"""
import asyncio
import logging
import sys
from pathlib import Path
from threading import Thread
from typing import AsyncIterator

from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver

from auth import get_limiter
from agent.config import Settings
from agent.graph import AgentBundle, build_graph
from agent.prompts import PROMPTS
from agent.state import create_initial_state
from agent.tools import fetch_page_tool, search_supplement_tool
from memory.store import MemoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

settings = Settings()

# ── 构建 LLM 和 Agent ──────────────────────────────────────

_llm = ChatOpenAI(
    model=settings.model,
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
)


def _make_llm(temp: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temp,
    )


_agents = AgentBundle(
    intent_router=create_agent(_llm, tools=[], system_prompt=PROMPTS["intent_router"]),
    planner=create_agent(_make_llm(0.3), tools=[], system_prompt=PROMPTS["planner"]),
    web_scout=create_agent(_make_llm(0.4), tools=[fetch_page_tool], system_prompt=PROMPTS["web_scout"]),
    analyst=create_agent(_make_llm(0.3), tools=[search_supplement_tool], system_prompt=PROMPTS["analyst"]),
    writer=create_agent(_make_llm(0.4), tools=[], system_prompt=PROMPTS["writer"]),
)

# ── Checkpointer ──────────────────────────────────────────

_db_path = ROOT / settings.db_path
_db_path.parent.mkdir(parents=True, exist_ok=True)
_ctx = SqliteSaver.from_conn_string(str(_db_path))
_checkpointer = _ctx.__enter__()
_checkpointer.setup()

_graph = build_graph(_agents, _checkpointer)

# ── 记忆 ─────────────────────────────────────────────────

_memory: MemoryStore | None = None
if settings.enable_memory:
    _memory = MemoryStore(db_path=str(_db_path))

# ── FastAPI ──────────────────────────────────────────────

app = FastAPI(title="DeepResearch")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 静态文件
_static_dir = ROOT / "static"
if _static_dir.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
async def index():
    index_path = _static_dir / "index.html"
    if index_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(index_path))
    return JSONResponse({"message": "DeepResearch API", "docs": "/docs"})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "deepresearch",
        "rate_limit": get_limiter().status(),
    }


_NODE_MESSAGES = {
    "intent": "意图路由 — 正在分析问题类型...",
    "direct_answer": "快速应答 — 正在生成回复...",
    "plan": "研究规划 — 正在拆解问题、制定搜索计划...",
    "web_scout": "网络搜索 — 正在检索最新资料...",
    "analyst": "分析评估 — 正在评估证据、形成结论...",
    "writer": "报告撰写 — 正在生成深度研究报告...",
}

_NODE_DONE = {
    "intent": "意图路由完成",
    "direct_answer": "回复生成完成",
    "plan": "研究计划制定完成",
    "web_scout": "搜索与证据提取完成",
    "analyst": "分析评估完成",
    "writer": "报告撰写完成",
}

_NODE_ICONS = {
    "intent": "🧭",
    "direct_answer": "💬",
    "plan": "📋",
    "web_scout": "🔍",
    "analyst": "🧠",
    "writer": "✍️",
}


def _predict_next(node_name: str, output: dict) -> str | None:
    """根据当前节点和输出预测下一个节点"""
    if node_name == "intent":
        route = output.get("intent", "multiagent")
        return "direct_answer" if route == "direct" else "plan"
    elif node_name == "direct_answer":
        return None
    elif node_name == "plan":
        return "web_scout"
    elif node_name == "web_scout":
        return "analyst"
    elif node_name == "analyst":
        if output.get("needs_more_research", False):
            return "web_scout"
        return "writer"
    elif node_name == "writer":
        return None
    return None


def _make_phase(node_name: str, status: str, detail: str = "") -> dict:
    """构建阶段事件"""
    if status == "start":
        msg = _NODE_MESSAGES.get(node_name, f"{node_name} 执行中")
    else:
        msg = _NODE_DONE.get(node_name, f"{node_name} 完成")
    return {
        "type": "phase",
        "node": node_name,
        "status": status,
        "message": msg,
        "icon": _NODE_ICONS.get(node_name, ""),
        "detail": detail,
    }


@app.post("/api/v1/research/stream")
async def research_stream(request: Request):
    body = await request.json()
    query = str(body.get("query", "")).strip()
    user_id = str(body.get("user_id", "default"))
    thread_id = str(body.get("thread_id", "default"))

    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    # 频率限制：提取客户端 IP
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    limiter = get_limiter()
    deny_reason = limiter.acquire(client_ip)
    if deny_reason:
        return JSONResponse({"error": deny_reason}, status_code=429)

    async def event_stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict] = asyncio.Queue()

        def emit(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

        def worker() -> None:
            try:
                # 记忆上下文
                memory_context = ""
                if _memory:
                    memory_context = _memory.build_memory_context(user_id, thread_id, query)

                state = create_initial_state(
                    query=query,
                    max_iterations=settings.max_iterations,
                    user_id=user_id,
                    memory_context=memory_context,
                )
                config = {"configurable": {"thread_id": thread_id}}

                # 第一个节点始终是 intent → 发送 start 事件
                emit(_make_phase("intent", "start"))

                accumulated = {}  # 累积节点输出，writer 完成后立即取用
                final_emitted = False

                for update in _graph.stream(state, config, stream_mode="updates"):
                    if not isinstance(update, dict):
                        continue
                    for node_name, node_output in update.items():
                        if isinstance(node_output, dict):
                            accumulated.update(node_output)

                        # ── 当前节点完成 → 发送 done 事件 ──
                        detail = ""
                        evt_data = {}

                        if isinstance(node_output, dict):
                            if node_name == "intent":
                                route = node_output.get("intent", "")
                                detail = "简单问答" if route == "direct" else "深度研究，启动多 Agent 协作"
                            elif node_name == "direct_answer":
                                detail = "已生成回复"
                            elif node_name == "plan":
                                queries = node_output.get("search_queries", [])
                                detail = f"生成 {len(queries)} 个搜索方向"
                                evt_data["search_queries"] = queries
                            elif node_name == "web_scout":
                                evid = node_output.get("evidence", [])
                                sidx = node_output.get("source_index", [])
                                detail = f"搜索到 {len(evid)} 条证据，{len(sidx)} 个来源"
                                evt_data["evidence_count"] = len(evid)
                            elif node_name == "analyst":
                                scores = node_output.get("evidence_scores", [])
                                findings = node_output.get("findings", [])
                                needs = node_output.get("needs_more_research", False)
                                refined = node_output.get("refined_queries", [])
                                high = sum(1 for s in scores if s.get("reliability", 0) >= 0.7)
                                if needs:
                                    detail = f"高信度 {high}/{len(scores)} 条，证据不足需补充"
                                else:
                                    detail = f"高信度 {high}/{len(scores)} 条，{len(findings)} 个关键结论"
                                evt_data["findings_count"] = len(findings)
                                evt_data["evidence_scores"] = scores

                                if needs and refined:
                                    evt_data["needs_reloop"] = True
                                    evt_data["refined_count"] = len(refined)
                            elif node_name == "writer":
                                final_text = node_output.get("final", "")
                                detail = f"共 {len(final_text)} 字"

                        done_evt = _make_phase(node_name, "done", detail)
                        done_evt.update(evt_data)
                        emit(done_evt)

                        # 需要循环 → 发 reloop 事件
                        if evt_data.get("needs_reloop"):
                            emit({
                                "type": "reloop",
                                "node": "reloop",
                                "icon": "🔄",
                                "message": "证据不足，补充搜索中...",
                                "detail": f"分析师给出 {evt_data['refined_count']} 个新搜索方向",
                            })

                        # writer/direct_answer 完成后立即发 final 事件，不等 graph.invoke
                        if node_name in ("writer", "direct_answer") and not final_emitted:
                            final_emitted = True
                            final_text = accumulated.get("final", "")
                            source_index = accumulated.get("source_index", [])
                            evidence = accumulated.get("evidence", [])
                            evidence_scores = accumulated.get("evidence_scores", [])

                            # 计算证据质量等级
                            high_count = sum(1 for s in evidence_scores if s.get("reliability", 0) >= 0.7)
                            total_scored = len(evidence_scores)
                            if high_count >= 3:
                                quality = "high"
                            elif high_count >= 1 or total_scored >= 5:
                                quality = "medium"
                            else:
                                quality = "low"

                            emit({
                                "type": "final",
                                "query": query,
                                "final": final_text,
                                "source_index": source_index,
                                "evidence": evidence,
                                "evidence_scores": evidence_scores,
                                "quality": quality,
                                "quality_detail": f"高信度 {high_count}/{total_scored} 条",
                            })

                        # ── 预测下一个节点 → 发送 start 事件 ──
                        next_node = _predict_next(node_name, node_output if isinstance(node_output, dict) else {})
                        if next_node:
                            emit(_make_phase(next_node, "start"))

                # 写记忆（graph.invoke 后的最终状态用于持久化）
                if not final_emitted:
                    result = _graph.invoke(state, config)
                    final = result.get("final", "")
                    source_index = result.get("source_index", [])
                    evidence = result.get("evidence", [])
                    evidence_scores = result.get("evidence_scores", [])
                    high_count = sum(1 for s in evidence_scores if s.get("reliability", 0) >= 0.7)
                    total_scored = len(evidence_scores)
                    if high_count >= 3:
                        quality = "high"
                    elif high_count >= 1 or total_scored >= 5:
                        quality = "medium"
                    else:
                        quality = "low"
                    emit({
                        "type": "final",
                        "query": query,
                        "final": final,
                        "source_index": source_index,
                        "evidence": evidence,
                        "evidence_scores": evidence_scores,
                        "quality": quality,
                        "quality_detail": f"高信度 {high_count}/{total_scored} 条",
                    })
                else:
                    result = _graph.invoke(state, config)
                    final = result.get("final", "")

                if _memory:
                    _memory.persist_turn(user_id, thread_id, query, final)
            except Exception as exc:
                emit({"type": "error", "message": str(exc)})
            finally:
                limiter.release()
                emit({"type": "__done__"})

        Thread(target=worker, daemon=True).start()

        while True:
            event = await queue.get()
            if event.get("type") == "__done__":
                break
            yield f"data: {__import__('json').dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=settings.host, port=settings.port, reload=True)
