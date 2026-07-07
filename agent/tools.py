"""工具函数 —— 三重搜索源: 博查 + DuckDuckGo + DeepSeek联网"""
import json
import logging
import os
import re as _re
import time
import urllib.error
import urllib.request

logger = logging.getLogger("deepresearch")

# ── 博查搜索 ─────────────────────────────────────────────

def bocha_web_search(query: str, count: int = 5) -> list[dict]:
    api_key = os.getenv("BOCHA_API_KEY", "").strip()
    if not api_key:
        return []

    def _do_search(freshness: str) -> list[dict]:
        payload = {"query": query, "summary": True, "freshness": freshness, "count": count}
        try:
            request = urllib.request.Request(
                url="https://api.bocha.cn/v1/web-search",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("博查搜索失败: %s", exc)
            return []

        records = []
        for item in data.get("data", {}).get("webPages", {}).get("value", []):
            pub_date = (
                item.get("datePublished")
                or item.get("dateLastCrawled")
                or item.get("date", "")
            )
            records.append({
                "title": item.get("name", ""),
                "url": item.get("url", ""),
                "snippet": item.get("summary", "") or item.get("snippet", ""),
                "domain": item.get("displayUrl", "") or item.get("siteName", ""),
                "date": pub_date,
                "source": "bocha",
            })
        return records

    # 先用 Month 搜最新结果
    records = _do_search("Month")
    # 如果近一月结果太少，fallback 到 Year 补充
    if len(records) < 3:
        logger.info("博查 Month 仅 %d 条结果，fallback 到 Year 补充", len(records))
        year_records = _do_search("Year")
        seen_urls = {r["url"] for r in records}
        for r in year_records:
            if r["url"] not in seen_urls:
                records.append(r)

    return records


# ── DuckDuckGo 搜索 ──────────────────────────────────────

def ddg_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            # timelimit='m' 限制为近一月结果，确保搜到最新信息
            results = list(ddgs.text(query, max_results=max_results, timelimit='m'))
    except Exception as exc:
        logger.warning("DDG搜索失败: %s", exc)
        return []

    records = []
    for r in results:
        # 提取发布时间（DDG 返回的 date 字段，格式通常为 "2026-07-01" 或 "1 day ago"）
        pub_date = r.get("date", "")
        records.append({
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
            "domain": r.get("href", "").split("/")[2] if "/" in r.get("href", "") else "",
            "date": pub_date,
            "source": "ddg",
        })
    return records


# ── DeepSeek 联网搜索 ────────────────────────────────────

def deepseek_web_search(query: str) -> list[dict]:
    """调用 DeepSeek API 的联网搜索能力，尝试解析返回的搜索结果"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "user",
                "content": (
                    f"请联网搜索以下问题，优先返回最新、最近发布的信息，并以JSON格式返回前5条搜索结果：{query}\n\n"
                    f"严格按此JSON格式输出，不要输出其他内容：\n"
                    f'{{"results":[{{"title":"标题","url":"https://...","snippet":"摘要(100字内)","date":"发布日期(YYYY-MM-DD格式，如无法确定则留空)"}}]}}'
                ),
            }],
            tools=[{"type": "web_search", "web_search": {"enable": True}}],
            temperature=0.1,
            timeout=30,
        )
        content = response.choices[0].message.content
    except Exception as exc:
        logger.warning("DeepSeek联网搜索失败: %s", exc)
        return []

    # 尝试解析 JSON
    try:
        # 去掉可能的 markdown code fence
        content = _re.sub(r'```(?:json)?\s*\n?', '', content)
        content = _re.sub(r'\n?```', '', content)
        data = json.loads(content)
        results = data.get("results", [])
    except json.JSONDecodeError:
        # JSON 解析失败，尝试提取 URL
        logger.info("DeepSeek搜索结果JSON解析失败，尝试提取URL")
        results = []
        urls = _re.findall(r'https?://[^\s\)\]，。]+', content)
        for url in urls[:5]:
            results.append({"title": "", "url": url, "snippet": "", "date": ""})

    records = []
    for r in results:
        url = r.get("url", "")
        if url:
            records.append({
                "title": r.get("title", ""),
                "url": url,
                "snippet": r.get("snippet", ""),
                "domain": url.split("/")[2] if "/" in url else "",
                "date": r.get("date", ""),
                "source": "deepseek",
            })
    return records


# ── 统一搜索入口 ─────────────────────────────────────────

def search_all(queries: list[str], count_per: int = 4) -> list[dict]:
    """三重搜索：博查 + DDG + DeepSeek，合并去重"""
    seen_urls = set()
    all_records = []

    for idx, q in enumerate(queries):
        if idx > 0:
            time.sleep(0.8)

        # 并行调用三个搜索源
        bocha_results = bocha_web_search(q, count=count_per)
        ddg_results = ddg_search(q, max_results=count_per)
        # DeepSeek 只对前 4 个搜索词调用（节省API调用）
        ds_results = deepseek_web_search(q) if idx < 4 else []

        for rec in bocha_results + ddg_results + ds_results:
            url = rec.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                rec["source_id"] = f"WEB-{len(all_records) + 1}"
                all_records.append(rec)

        logger.info("搜索[%d/%d] %s → bocha:%d ddg:%d ds:%d 累计:%d",
                    idx + 1, len(queries), q[:40],
                    len(bocha_results), len(ddg_results), len(ds_results), len(all_records))

    return all_records


# ── 日期解析辅助 ─────────────────────────────────────────

def _parse_date(date_str: str):
    """尝试从多种格式中解析日期，返回 datetime 对象或 None"""
    if not date_str or not isinstance(date_str, str):
        return None
    from datetime import datetime as dt
    # 常见格式列表
    formats = [
        "%Y-%m-%d",         # 2026-07-06
        "%Y-%m-%dT%H:%M:%S",  # ISO 8601
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",         # 2026/07/06
        "%d %b %Y",         # 06 Jul 2026
        "%b %d, %Y",        # Jul 06, 2026
        "%B %d, %Y",        # July 06, 2026
        "%d %B %Y",         # 06 July 2026
        "%Y年%m月%d日",      # 2026年07月06日
    ]
    for fmt in formats:
        try:
            return dt.strptime(date_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    # 最后尝试 ISO 格式（可能带时区）
    try:
        return dt.fromisoformat(date_str.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    return None


# ── 失效内容过滤 ─────────────────────────────────────────

def filter_defunct(records: list[dict]) -> list[dict]:
    """软过滤：仅移除明确失效/404/下架的内容，不做日期硬截断。
    时效性判断交由下游分析师 Agent 根据 date 元数据 + 正文内容综合评分。"""
    kept = []

    for rec in records:
        text = (rec.get("title", "") + " " + rec.get("snippet", "")).lower()
        title = rec.get("title", "")

        # 仅检测明确失效/不可用的标记
        defunct_markers = [
            "下架", "已停用", "已关闭", "不再提供", "已失效", "已过期",
            "deprecated", "discontinued", "no longer available",
            "404", "页面不存在", "page not found",
            "已迁移", "已改版",
        ]
        is_defunct = any(m in text for m in defunct_markers)

        if is_defunct:
            logger.info("过滤失效内容: %s", title[:60])
            continue

        kept.append(rec)

    removed = len(records) - len(kept)
    if removed:
        logger.info("失效过滤: %d → %d 条 (移除%d条明确失效)", len(records), len(kept), removed)
    return kept


# ── 网页正文抓取 ─────────────────────────────────────────

def fetch_page_content(url: str, timeout: int = 10) -> str:
    """抓取网页正文——提取纯文本内容"""
    try:
        request = urllib.request.Request(
            url=url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read(200 * 1024)
            html = raw.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.info("抓取页面失败 %s: %s", url[:60], exc)
        return ""

    # 移除 script/style/nav/header/footer
    html = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r'<style[^>]*>.*?</style>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r'<header[^>]*>.*?</header>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r'<[^>]+>', ' ', html)
    text = _re.sub(r'&nbsp;', ' ', text)
    text = _re.sub(r'&amp;', '&', text)
    text = _re.sub(r'&lt;', '<', text)
    text = _re.sub(r'&gt;', '>', text)
    text = _re.sub(r'&quot;', '"', text)
    text = _re.sub(r'[ \t]+', ' ', text)
    text = _re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    if len(text) > 8000:
        text = text[:8000] + "\n...(内容截断)"

    return text


def enrich_records_with_content(records: list[dict], max_fetch: int = 10) -> list[dict]:
    """对前 N 条记录抓取完整页面内容"""
    for i, rec in enumerate(records):
        if i >= max_fetch:
            break
        url = rec.get("url", "")
        if not url:
            continue
        logger.info("抓取页面 (%d/%d): %s", i + 1, min(len(records), max_fetch), url[:80])
        content = fetch_page_content(url)
        if content:
            rec["full_content"] = content
            if len(rec.get("snippet", "")) < 100:
                rec["snippet"] = content[:500]
        time.sleep(0.5)
    return records


# ── LangChain 工具（供 Agent tool-calling 使用）────────────

from langchain_core.tools import tool as lc_tool


@lc_tool
def fetch_page_tool(url: str) -> str:
    """获取指定网页的完整正文内容。当你看到搜索摘要但需要更多细节（具体数据、价格、日期）时使用此工具。
    优先选择官网(.com/.org 主域名)、权威媒体、以及摘要中提到了具体数字的页面。每次只抓取一个 URL。

    Args:
        url: 要抓取的网页 URL（必须是搜索结果中出现的完整 URL）
    """
    content = fetch_page_content(url)
    if not content:
        return f"⚠️ 无法抓取 {url}，页面可能已被删除、屏蔽或无法访问。请尝试其他 URL。"
    return f"【页面抓取成功】{url}\n\n{content}"


@lc_tool
def search_supplement_tool(query: str) -> str:
    """当你分析证据后发现某个具体维度缺少关键数据时，用此工具做定向补充搜索。
    每次只搜一个具体问题，搜索词应包含关键实体 + 时间限定（如"2026"或"2026年7月"）+ 所需数据维度。

    注意：此工具只返回搜索摘要（标题/URL/摘要/日期），不会抓取完整页面内容。从摘要中提取关键数据即可。

    Args:
        query: 具体的补充搜索词，如"DeepSeek V3 API 输出价格 2026年7月"
    """
    results = search_all([query], count_per=3)
    if not results:
        return f"未找到与 '{query}' 相关的搜索结果。"
    lines = [f"## 补充搜索结果：「{query}」\n"]
    for r in results:
        date_str = f"  |  日期: {r['date']}" if r.get('date') else ""
        lines.append(
            f"- **{r['title']}**{date_str}\n"
            f"  URL: {r['url']}\n"
            f"  摘要: {r['snippet'][:250]}\n"
        )
    return "\n".join(lines)


@lc_tool
def search_tool(query: str) -> str:
    """执行网络搜索。输入一个搜索词，返回去重后的搜索结果（标题、URL、摘要、发布日期、来源编号）。
    你应该先为每个搜索方向调用此工具获取结果摘要，再对最相关的页面调用 fetch_page_tool 获取全文。

    Args:
        query: 单个搜索查询词，如 "DeepSeek V4 Pro API 定价 2026年7月"
    """
    results = search_all([query], count_per=4)
    if not results:
        return f"未找到与 '{query}' 相关的搜索结果。"
    lines = [f"## 搜索结果：「{query}」({len(results)} 条)\n"]
    for r in results:
        date_str = f"  |  日期: {r['date']}" if r.get('date') else ""
        lines.append(
            f"[{r['source_id']}] **{r['title']}**{date_str}\n"
            f"  URL: {r['url']}\n"
            f"  摘要: {r['snippet'][:300]}\n"
        )
    return "\n".join(lines)
