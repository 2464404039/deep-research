"""DeepInquire —— FastAPI Web 服务 + SSE 流式输出"""
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

from agent.config import Settings
from agent.graph import AgentBundle, build_graph
from agent.prompts import PROMPTS
from agent.state import create_initial_state
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
    web_scout=create_agent(_make_llm(0.4), tools=[], system_prompt=PROMPTS["web_scout"]),
    analyst=create_agent(_make_llm(0.3), tools=[], system_prompt=PROMPTS["analyst"]),
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

app = FastAPI(title="DeepInquire")
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
    return JSONResponse({"message": "DeepInquire API", "docs": "/docs"})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "deepinquire"}


_NODE_MESSAGES = {
    "intent": "意图路由 — 正在分析问题类型...",
    "direct_answer": "快速应答 — 正在生成回复...",
    "plan": "研究规划 — 正在拆解问题、制定搜索计划...",
    "web_scout": "网络搜索 — 正在检索最新资料...",
    "analyst": "分析评估 — 正在评估证据、形成结论...",
    "writer": "报告撰写 — 正在生成深度研究报告...",
}

_NODE_ICONS = {
    "intent": "🧭",
    "direct_answer": "💬",
    "plan": "📋",
    "web_scout": "🔍",
    "analyst": "🧠",
    "writer": "✍️",
}


@app.post("/api/v1/research/stream")
async def research_stream(request: Request):
    body = await request.json()
    query = str(body.get("query", "")).strip()
    user_id = str(body.get("user_id", "default"))
    thread_id = str(body.get("thread_id", "default"))

    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

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

                for update in _graph.stream(state, config, stream_mode="updates"):
                    if not isinstance(update, dict):
                        continue
                    for node_name, node_output in update.items():
                        msg = _NODE_MESSAGES.get(node_name, f"{node_name} 执行中")
                        evt = {"type": "phase", "node": node_name, "message": msg, "icon": _NODE_ICONS.get(node_name, "")}

                        # 附加上下文信息
                        if isinstance(node_output, dict):
                            if node_name == "plan":
                                queries = node_output.get("search_queries", [])
                                evt["detail"] = f"生成 {len(queries)} 个搜索词：{'、'.join(queries[:4])}"
                                evt["search_queries"] = queries
                            elif node_name == "web_scout":
                                evid = node_output.get("evidence", [])
                                sidx = node_output.get("source_index", [])
                                evt["detail"] = f"搜索到 {len(evid)} 条证据，{len(sidx)} 个来源"
                                evt["evidence_count"] = len(evid)
                            elif node_name == "analyst":
                                findings = node_output.get("findings", [])
                                scores = node_output.get("evidence_scores", [])
                                needs = node_output.get("needs_more_research", False)
                                high = sum(1 for s in scores if s.get("reliability", 0) >= 0.7)
                                action = "需要补充搜索" if needs else "证据充分，准备撰写报告"
                                evt["detail"] = f"证据评分: {len(scores)}条 | 高信度{high}条 | {len(findings)}个结论 | {action}"
                                evt["findings_count"] = len(findings)
                                evt["evidence_scores"] = scores
                            elif node_name == "writer":
                                evt["detail"] = "正在生成 Markdown 研究报告..."

                        emit(evt)

                # 获取最终结果
                result = _graph.invoke(state, config)
                final = result.get("final", "")
                source_index = result.get("source_index", [])
                evidence = result.get("evidence", [])
                evidence_scores = result.get("evidence_scores", [])

                if _memory:
                    _memory.persist_turn(user_id, thread_id, query, final)

                emit({
                    "type": "final",
                    "query": query,
                    "final": final,
                    "source_index": source_index,
                    "evidence": evidence,
                    "evidence_scores": evidence_scores,
                })
            except Exception as exc:
                emit({"type": "error", "message": str(exc)})
            finally:
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
