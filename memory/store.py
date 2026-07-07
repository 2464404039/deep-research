"""SQLite记忆存储 —— 对话历史 + 用户画像"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("deepresearch.memory")


class MemoryStore:
    """统一的 SQLite 记忆存储，管理对话历史和用户画像"""

    def __init__(self, db_path: str = "data/memory.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_tables(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_conv_thread
                    ON conversations(user_id, thread_id, created_at);

                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)

    # ── 对话消息 ──────────────────────────────────────────────

    def add_message(self, user_id: str, thread_id: str, role: str, content: str) -> None:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    conn.execute(
                        "INSERT INTO conversations (user_id, thread_id, role, content) VALUES (?,?,?,?)",
                        (user_id, thread_id, role, content),
                    )
            except Exception as exc:
                logger.warning("保存消息失败: %s", exc)

    def get_recent_messages(self, user_id: str, thread_id: str, limit: int = 10) -> list[dict]:
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT role, content FROM conversations "
                    "WHERE user_id=? AND thread_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (user_id, thread_id, limit),
                ).fetchall()
            return [{"role": r, "content": c} for r, c in reversed(rows)]
        except Exception as exc:
            logger.warning("读取消息失败: %s", exc)
            return []

    def count_messages(self, user_id: str, thread_id: str) -> int:
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE user_id=? AND thread_id=?",
                    (user_id, thread_id),
                ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    # ── 用户画像 ──────────────────────────────────────────────

    def get_or_create_profile(self, user_id: str) -> dict:
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT profile_json FROM user_profile WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            if row:
                return json.loads(row[0])
        except Exception:
            pass

        # 创建默认画像
        default = {"preferences": {}, "facts": {}, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO user_profile (user_id, profile_json) VALUES (?,?)",
                    (user_id, json.dumps(default, ensure_ascii=False)),
                )
        except Exception:
            pass
        return default

    def update_profile(self, user_id: str, updates: dict) -> None:
        profile = self.get_or_create_profile(user_id)
        profile.update(updates)
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO user_profile (user_id, profile_json, updated_at) "
                    "VALUES (?,?,datetime('now'))",
                    (user_id, json.dumps(profile, ensure_ascii=False)),
                )
        except Exception as exc:
            logger.warning("更新画像失败: %s", exc)

    # ── 记忆上下文构建 ────────────────────────────────────────

    def get_recent_messages_global(self, user_id: str, limit: int = 6) -> list[dict]:
        """获取用户所有 thread 中的最近消息（跨会话记忆）"""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT role, content FROM conversations "
                    "WHERE user_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [{"role": r, "content": c} for r, c in reversed(rows)]
        except Exception as exc:
            logger.warning("读取全局消息失败: %s", exc)
            return []

    def build_memory_context(self, user_id: str, thread_id: str, query: str,
                             llm=None) -> str:
        """构建注入 LLM prompt 的记忆上下文文本。如果提供 llm，长回答会被压缩为要点。"""
        parts = []

        # 1. 用户画像
        profile = self.get_or_create_profile(user_id)
        facts = profile.get("facts", {})
        prefs = profile.get("preferences", {})
        if facts or prefs:
            lines = []
            if facts:
                lines.append(f"已知信息：{json.dumps(facts, ensure_ascii=False)}")
            if prefs:
                lines.append(f"用户偏好：{json.dumps(prefs, ensure_ascii=False)}")
            parts.append("\n".join(lines))

        # 2. 最近对话（跨 thread 查询，全文注入，长回答 LLM 压缩）
        recent = self.get_recent_messages_global(user_id, limit=8)
        if recent:
            lines = ["最近对话："]
            for msg in recent:
                role_label = "用户" if msg["role"] == "user" else "AI"
                content = msg['content']
                # 超过 500 字的长回答用 LLM 压缩为要点，用户问题原文保留
                if role_label == "AI" and len(content) > 500 and llm:
                    content = self._compress_content(content, llm)
                lines.append(f"{role_label}: {content}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    def _compress_content(self, text: str, llm) -> str:
        """用 LLM 将长文压缩为要点，保留关键数据、数字和结论"""
        try:
            from langchain_core.messages import HumanMessage
            resp = llm.invoke([HumanMessage(content=
                f"将以下内容压缩为一段话（不超过300字），保留所有具体数字、价格、日期、版本号和核心结论：\n\n{text}"
            )])
            return resp.content.strip()
        except Exception:
            return text[:500]  # LLM 压缩失败 → 退到截断

    # ── 持久化一轮对话 ────────────────────────────────────────

    def persist_turn(self, user_id: str, thread_id: str, query: str, answer: str) -> None:
        """保存用户问题和 AI 回答，并从问题中提取长期记忆"""
        self.add_message(user_id, thread_id, "user", query)
        self.add_message(user_id, thread_id, "assistant", answer)

        # 简单提取：如果用户提到了关于自己的事实，存入画像
        profile = self.get_or_create_profile(user_id)
        facts = profile.get("facts", {})

        # 关键词匹配提取
        patterns = {
            "姓名": ["我叫", "我是", "我的名字是"],
            "职业": ["我做", "我的工作是", "从事", "我的职位是"],
            "技能": ["我会", "我擅长", "我懂", "我用过"],
            "学历": ["我毕业于", "我的学校", "我学的是", "我的专业"],
            "地点": ["我住在", "我在城市", "我在"],
        }
        for key, keywords in patterns.items():
            for kw in keywords:
                if kw in query:
                    facts[key] = query
                    break

        if facts != profile.get("facts", {}):
            self.update_profile(user_id, {"facts": facts})


