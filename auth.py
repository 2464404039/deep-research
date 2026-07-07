"""轻量并发 + 频率限制 —— 内存计数器，零依赖重启清零"""
import time
import threading
import logging
from collections import defaultdict

logger = logging.getLogger("deepresearch.auth")


class RateLimiter:
    """IP 级别频率限制 + 全局并发限制。本地/内网 IP 自动绕过。"""

    _LOCAL_NETS = (
        "127.", "::1", "localhost",
        "192.168.", "10.",
        "172.16.", "172.17.", "172.18.", "172.19.",
        "172.20.", "172.21.", "172.22.", "172.23.",
        "172.24.", "172.25.", "172.26.", "172.27.",
        "172.28.", "172.29.", "172.30.", "172.31.",
    )

    def __init__(self, per_ip_limit: int = 5, per_ip_window: int = 3600,
                 global_concurrency: int = 3):
        self.per_ip_limit = per_ip_limit
        self.per_ip_window = per_ip_window      # 秒
        self.global_concurrency = global_concurrency

        self._ip_requests: dict[str, list[float]] = defaultdict(list)
        self._active_count = 0
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> None:
        """清理过期记录"""
        window_start = now - self.per_ip_window
        self._ip_requests[ip] = [
            t for t in self._ip_requests[ip] if t > window_start
        ]
        if not self._ip_requests[ip]:
            del self._ip_requests[ip]

    def _is_local(self, ip: str) -> bool:
        """本地/内网 IP 绕过频率限制"""
        for prefix in self._LOCAL_NETS:
            if ip.startswith(prefix):
                return True
        return False

    def acquire(self, ip: str) -> str | None:
        """尝试获取执行许可。返回 None 表示通过，返回字符串表示拒绝原因"""
        # 本地/内网访问不做限制
        if self._is_local(ip):
            return None

        now = time.time()

        with self._lock:
            # 第1层：全局并发
            if self._active_count >= self.global_concurrency:
                return "当前使用人数较多，请稍后再试"

            # 第2层：单 IP 频率
            self._prune(ip, now)
            if len(self._ip_requests[ip]) >= self.per_ip_limit:
                return f"每小时最多 {self.per_ip_limit} 次深度研究，请稍后再试"

            # 通过 → 计数
            self._active_count += 1
            self._ip_requests[ip].append(now)

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
            active_ips = 0
            for ip in list(self._ip_requests.keys()):
                self._prune(ip, now)
                if ip in self._ip_requests:
                    active_ips += 1
            return {
                "active_requests": self._active_count,
                "active_ips": active_ips,
                "max_concurrency": self.global_concurrency,
                "per_ip_limit": self.per_ip_limit,
                "window_minutes": self.per_ip_window // 60,
            }


# 单例
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        import os
        limit = int(os.getenv("RATE_LIMIT_PER_HOUR", "3"))
        concurrency = int(os.getenv("RATE_LIMIT_CONCURRENCY", "3"))
        _limiter = RateLimiter(per_ip_limit=limit, global_concurrency=concurrency)
        logger.info("频率限制: %d次/小时/IP, 全局并发 %d", limit, concurrency)
    return _limiter
