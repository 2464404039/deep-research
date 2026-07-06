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

    # ── 对话压缩 ──────────────────────────────────────────────

    def compress_conversation(self, user_id: str, thread_id: str, summary_llm=None) -> str | None:
        """压缩旧消息：保留最近10条，旧消息用LLM（如果提供）或规则压缩为摘要"""
        total = self.count_messages(user_id, thread_id)
        if total <= 20:
            return None  # 不需要压缩

        # 取旧消息（跳过最近10条）
        try:
            with self._get_conn() as conn:
                old_rows = conn.execute(
                    "SELECT role, content FROM conversations "
                    "WHERE user_id=? AND thread_id=? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (user_id, thread_id, total - 10),
                ).fetchall()
        except Exception:
            return None

        if not old_rows:
            return None

        old_text = "\n".join(f"{r}: {c[:200]}" for r, c in old_rows)

        if summary_llm:
            try:
                from langchain_core.messages import HumanMessage

                resp = summary_llm.invoke([
                    HumanMessage(content=f"将以下对话历史压缩为一段话（不超过200字），保留关键事实和用户偏好：\n\n{old_text}")
                ])
                summary = resp.content.strip()
            except Exception:
                summary = _rule_compress(old_rows)
        else:
            summary = _rule_compress(old_rows)

        # 删除旧消息，只保留最近10条
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "DELETE FROM conversations WHERE id IN ("
                    "SELECT id FROM conversations WHERE user_id=? AND thread_id=? "
                    "ORDER BY created_at ASC LIMIT ?"
                    ")",
                    (user_id, thread_id, total - 10),
                )
        except Exception:
            pass

        # 在对话中插入一条摘要消息
        self.add_message(user_id, thread_id, "system", f"[对话摘要] {summary}")
        logger.info("对话压缩完成: 旧消息=%d → 摘要(%d字)", len(old_rows), len(summary))
        return summary

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

    def build_memory_context(self, user_id: str, thread_id: str, query: str) -> str:
        """构建注入 LLM prompt 的记忆上下文文本"""
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

        # 2. 最近对话
        recent = self.get_recent_messages(user_id, thread_id, limit=6)
        if recent:
            lines = ["最近对话："]
            for msg in recent:
                role_label = "用户" if msg["role"] == "user" else "AI"
                lines.append(f"{role_label}: {msg['content'][:300]}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

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

        # 压缩检查
        self.compress_conversation(user_id, thread_id)


def _rule_compress(messages: list[tuple]) -> str:
    """规则压缩：提取关键主题"""
    topics = set()
    for _, content in messages:
        if len(content) > 5:
            topics.add(content[:50])
    return "对话涉及主题: " + "；".join(list(topics)[:5])
