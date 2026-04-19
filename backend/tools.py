# =============================================================
# tools.py —— Agent 可调用的工具函数集合
#
# 职责：
#   1. search_news  : 通过 Google News RSS 搜索新闻
#   2. deduplicate_news : 根据 URL 和标题去除重复文章
#   3. sort_and_limit   : 按发布时间降序排列并截取指定数量
#   4. format_for_frontend : 将原始数据格式化为前端所需结构
# =============================================================

import html       # 用于解码 HTML 实体（如 &amp; → &）
import logging
import re         # 用于正则匹配和删除 HTML 标签
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import quote_plus  # 将搜索词编码为 URL 安全格式

import feedparser  # RSS/Atom 解析库

logger = logging.getLogger("tools")

# Google News RSS 搜索接口
# 参数说明：
#   q     : 搜索关键词（URL 编码后的）
#   hl    : 界面语言（en-US 英文，覆盖面最广，结果最多）
#   gl    : 地区（US）
#   ceid  : 国家+语言组合标识
_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def _strip_html(text: str) -> str:
    """
    去除字符串中的所有 HTML 标签，并解码 HTML 实体。

    Google News RSS 的 summary 字段通常包含 <a>、<b> 等标签，
    直接显示会导致前端看到原始 HTML 代码，需要先清理。

    示例：
        "<b>Apple</b> &amp; Google" → "Apple & Google"
    """
    text = re.sub(r"<[^>]+>", "", text)   # 删除所有 <标签>
    return html.unescape(text).strip()    # 解码 &amp; 等实体并去除首尾空白


def _struct_to_iso(t) -> str:
    """
    将 feedparser 解析出的 time.struct_time 转换为 ISO 8601 字符串。

    feedparser 将 RSS 中的日期字符串解析为 Python 的 time.struct_time，
    格式为 (年, 月, 日, 时, 分, 秒, ...)，需要转换为标准 ISO 格式
    供后续日期比较和显示使用。

    返回示例："2026-04-19T10:30:00+00:00"
    """
    if not t:
        return ""
    try:
        # t[:6] 取前 6 个元素：(年, 月, 日, 时, 分, 秒)
        dt = datetime(*t[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return ""


def search_news(query: str, max_results: int = 15) -> List[Dict[str, Any]]:
    """
    通过 Google News RSS 搜索指定关键词的最新新闻。

    使用 feedparser 解析 RSS XML，无需 API Key，无明显限流。
    每篇文章返回标题、链接、摘要、来源和发布时间。

    Args:
        query:       搜索关键词（中英文均可）
        max_results: 最多返回条数，默认 15

    Returns:
        文章列表，每条为包含 title/url/body/source/date/image 的字典。
        出错时返回空列表，不抛出异常（避免中断 Agent 流程）。
    """
    logger.debug("Google News RSS 搜索  query=%r  max_results=%d", query, max_results)
    t0 = time.perf_counter()

    # 将关键词编码为 URL 安全格式（如空格→%20，中文→%E4%B8%AD%E6%96%87）
    url = _RSS_URL.format(query=quote_plus(query))
    logger.debug("RSS URL: %s", url)

    try:
        # feedparser.parse() 发起 HTTP 请求并解析返回的 XML
        feed = feedparser.parse(url)

        # bozo=True 表示 RSS 格式有轻微问题，通常仍可正常使用，记录警告即可
        if feed.bozo:
            logger.warning("feedparser 解析警告: %s", feed.bozo_exception)

        # 截取前 max_results 条
        entries = feed.entries[:max_results]
        results = []

        for entry in entries:
            # 优先取文章级别的来源，否则取 Feed 的整体标题
            source = entry.get("source", {}).get("title", "") or \
                     feed.feed.get("title", "Google News")

            results.append({
                "title":  entry.get("title", ""),
                "url":    entry.get("link", ""),
                "body":   _strip_html(entry.get("summary", "")),  # 清理摘要中的 HTML
                "source": source,
                "date":   _struct_to_iso(entry.get("published_parsed")),  # 转为 ISO 格式
                "image":  "",  # Google News RSS 不提供图片，留空
            })

        elapsed = time.perf_counter() - t0
        logger.info("搜索完成  query=%r  返回=%d 条  耗时=%.2fs", query, len(results), elapsed)
        if results:
            logger.debug(
                "第一条样本: title=%r  source=%r  date=%r",
                results[0]["title"][:60], results[0]["source"], results[0]["date"],
            )
        return results

    except Exception as e:
        logger.error("搜索失败  query=%r  error=%s", query, e, exc_info=True)
        return []


def _parse_date(date_str: str) -> datetime:
    """
    将 ISO 8601 日期字符串解析为带时区的 datetime 对象。

    用于文章排序时的日期比较。解析失败时返回最小时间（排在最后）。

    支持格式示例：
        "2026-04-19T10:30:00+00:00"
        "2026-04-19T10:30:00Z"
    """
    if not date_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        # Python 3.10 以下不支持 'Z' 后缀，手动替换为 '+00:00'
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        logger.debug("日期解析失败  date_str=%r", date_str)
        return datetime.min.replace(tzinfo=timezone.utc)


def deduplicate_news(articles: List[Dict]) -> List[Dict]:
    """
    对文章列表进行去重，同时基于 URL 和标题（前 60 字符）两个维度判断。

    去重策略：
      - URL 完全相同 → 重复
      - 标题前 60 字符相同（忽略大小写）→ 视为同一新闻的不同转载

    保留每组重复文章中的第一条（通常是最早出现的原始来源）。

    Args:
        articles: 原始文章列表（可能包含多次搜索的重复结果）

    Returns:
        去重后的文章列表
    """
    before = len(articles)
    seen_urls:   set = set()
    seen_titles: set = set()
    unique = []

    for article in articles:
        url       = article.get("url", "")
        # 标题统一小写+去首尾空格后取前 60 字符作为去重 key
        title_key = article.get("title", "").lower().strip()[:60]

        if url in seen_urls or title_key in seen_titles:
            continue  # 已见过，跳过

        seen_urls.add(url)
        seen_titles.add(title_key)
        unique.append(article)

    logger.info("去重  原始=%d  去重后=%d  删除=%d", before, len(unique), before - len(unique))
    return unique


def sort_and_limit(articles: List[Dict], limit: int = 15) -> List[Dict]:
    """
    将文章按发布时间降序排列（最新的在前），并截取前 limit 条。

    Args:
        articles: 去重后的文章列表
        limit:    保留的最大条数，默认 15

    Returns:
        排序并截取后的文章列表
    """
    sorted_articles = sorted(
        articles,
        key=lambda x: _parse_date(x.get("date", "")),
        reverse=True,  # 降序：最新的排最前
    )
    result = sorted_articles[:limit]
    logger.info("排序+截断  输入=%d  输出=%d  limit=%d", len(articles), len(result), limit)
    if result:
        logger.debug("最新一条: title=%r  date=%r", result[0].get("title"), result[0].get("date"))
    return result


def format_for_frontend(articles: List[Dict]) -> List[Dict]:
    """
    将后端内部格式的文章数据转换为前端 UI 所需的展示格式。

    主要变换：
      - 为每条文章分配从 1 开始的序号 id
      - 将 ISO 日期格式化为 "YYYY-MM-DD HH:MM" 便于显示
      - body 字段重命名为 summary（与前端字段名对应）

    Args:
        articles: 排序后的文章列表

    Returns:
        前端可直接渲染的文章列表
    """
    result = []
    for i, a in enumerate(articles):
        date_str = a.get("date", "")
        try:
            dt           = _parse_date(date_str)
            display_date = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            display_date = date_str  # 解析失败则保留原始字符串

        result.append({
            "id":      i + 1,                      # 从 1 开始的序号
            "title":   a.get("title", ""),
            "summary": a.get("body", ""),           # body → summary
            "url":     a.get("url", ""),
            "source":  a.get("source", "Unknown"),
            "date":    display_date,
            "image":   a.get("image", ""),
        })

    logger.debug("格式化完成  共 %d 条", len(result))
    return result
