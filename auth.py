"""轻量并发 + 频率限制 —— 按用户限流，内存计数器"""
import time
import threading
import logging
from collections import defaultdict

logger = logging.getLogger("deepresearch.auth")


class RateLimiter:
    """按 user_id 频率限制 + 全局并发限制。本地/内网 IP 自动绕过（仅作 fallback）。"""

    _LOCAL_NETS = (
        "127.", "::1", "localhost",
        "192.168.", "10.",
        "172.16.", "172.17.", "172.18.", "172.19.",
        "172.20.", "172.21.", "172.22.", "172.23.",
        "172.24.", "172.25.", "172.26.", "172.27.",
        "172.28.", "172.29.", "172.30.", "172.31.",
    )

    def __init__(self, per_user_limit: int = 4, per_user_window: int = 3600,
                 global_concurrency: int = 3):
        self.per_user_limit = per_user_limit
        self.per_user_window = per_user_window   # 秒
        self.global_concurrency = global_concurrency

        self._user_requests: dict[str, list[float]] = defaultdict(list)
        self._active_count = 0
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        """清理过期记录"""
        window_start = now - self.per_user_window
        self._user_requests[key] = [
            t for t in self._user_requests[key] if t > window_start
        ]
        if not self._user_requests[key]:
            del self._user_requests[key]

    def _is_local(self, ip: str) -> bool:
        for prefix in self._LOCAL_NETS:
            if ip.startswith(prefix):
                return True
        return False

    def acquire(self, user_id: str, ip: str = "") -> str | None:
        """尝试获取执行许可。
        按 user_id 限流，user_id 缺失时 fallback 到 IP。
        返回 None 表示通过，返回字符串表示拒绝原因。
        """
        # 本地/内网跳过限流
        if ip and self._is_local(ip):
            return None

        key = user_id or ip or "unknown"
        now = time.time()

        with self._lock:
            # 第1层：全局并发
            if self._active_count >= self.global_concurrency:
                return "当前使用人数较多，请稍后再试"

            # 第2层：按用户频率
            self._prune(key, now)
            if len(self._user_requests[key]) >= self.per_user_limit:
                return f"每小时最多 {self.per_user_limit} 次深度研究，请稍后再试"

            # 通过
            self._active_count += 1
            self._user_requests[key].append(now)

        return None

    def release(self) -> None:
        """执行完成，释放并发槽位"""
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1

    def status(self) -> dict:
        """查看当前状态"""
        with self._lock:
            now = time.time()
            active_users = 0
            for key in list(self._user_requests.keys()):
                self._prune(key, now)
                if key in self._user_requests:
                    active_users += 1
            return {
                "active_requests": self._active_count,
                "active_users": active_users,
                "max_concurrency": self.global_concurrency,
                "per_user_limit": self.per_user_limit,
                "window_minutes": self.per_user_window // 60,
            }


# 单例
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        import os
        limit = int(os.getenv("RATE_LIMIT_PER_HOUR", "4"))
        concurrency = int(os.getenv("RATE_LIMIT_CONCURRENCY", "3"))
        _limiter = RateLimiter(per_user_limit=limit, global_concurrency=concurrency)
        logger.info("频率限制: %d次/小时/用户, 全局并发 %d", limit, concurrency)
    return _limiter
