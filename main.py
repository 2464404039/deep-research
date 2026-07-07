"""DeepResearch —— CLI 入口"""
import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver

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


def run():
    settings = Settings()

    # 创建 LLM
    llm = ChatOpenAI(
        model=settings.model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )

    # 构建 5 个 Agent
    agents = AgentBundle(
        intent_router=create_agent(
            llm, tools=[], system_prompt=PROMPTS["intent_router"]
        ),
        planner=create_agent(
            ChatOpenAI(model=settings.model, api_key=settings.deepseek_api_key,
                       base_url=settings.deepseek_base_url, temperature=0.3),
            tools=[], system_prompt=PROMPTS["planner"]
        ),
        web_scout=create_agent(
            ChatOpenAI(model=settings.model, api_key=settings.deepseek_api_key,
                       base_url=settings.deepseek_base_url, temperature=0.4),
            tools=[fetch_page_tool], system_prompt=PROMPTS["web_scout"]
        ),
        analyst=create_agent(
            ChatOpenAI(model=settings.model, api_key=settings.deepseek_api_key,
                       base_url=settings.deepseek_base_url, temperature=0.3),
            tools=[search_supplement_tool], system_prompt=PROMPTS["analyst"]
        ),
        writer=create_agent(
            ChatOpenAI(model=settings.model, api_key=settings.deepseek_api_key,
                       base_url=settings.deepseek_base_url, temperature=0.4),
            tools=[], system_prompt=PROMPTS["writer"]
        ),
    )

    # SQLite Checkpointer
    db_path = ROOT / settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _ctx = SqliteSaver.from_conn_string(str(db_path))
    checkpointer = _ctx.__enter__()
    checkpointer.setup()

    # 编译图
    graph = build_graph(agents, checkpointer)

    # 记忆系统
    memory: MemoryStore | None = None
    if settings.enable_memory:
        memory = MemoryStore(db_path=str(ROOT / settings.db_path))

    # 解析参数
    parser = argparse.ArgumentParser(description="DeepResearch")
    parser.add_argument("--query", type=str, default=None, help="单次查询")
    parser.add_argument("--user", type=str, default="default", help="用户ID")
    args = parser.parse_args()

    def run_query(query: str, thread_id: str) -> str:
        """执行一次查询，带记忆上下文"""
        memory_context = ""
        if memory:
            memory_context = memory.build_memory_context(args.user, thread_id, query)

        state = create_initial_state(
            query=query,
            max_iterations=settings.max_iterations,
            user_id=args.user,
            memory_context=memory_context,
        )
        result = graph.invoke(state, {"configurable": {"thread_id": thread_id}})
        final = result["final"]

        if memory:
            memory.persist_turn(args.user, thread_id, query, final)

        return final

    if args.query:
        answer = run_query(args.query, args.user)
        print(f"\n{'='*60}")
        print(answer)
        print(f"{'='*60}\n")
    else:
        # 交互模式
        print("DeepResearch — 输入 /quit 退出\n")
        import uuid
        thread_id = str(uuid.uuid4())[:8]
        while True:
            try:
                query = input("你: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not query:
                continue
            if query.lower() in {"/quit", "/exit", "退出"}:
                break

            answer = run_query(query, thread_id)
            print(f"\nAI: {answer}\n")


if __name__ == "__main__":
    run()
