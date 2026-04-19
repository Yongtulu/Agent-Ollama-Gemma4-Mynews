# =============================================================
# agent.py —— 新闻搜索 Agent 核心逻辑
#
# 职责：
#   以"工具调用（Tool Calling）"的方式驱动本地 LLM（Ollama + Gemma 4），
#   让模型自主决定搜索策略，调用 search_news 工具获取新闻，
#   最终将结果去重、排序后以 SSE 事件流的形式推送给前端。
#
# Agent 工作流程：
#   1. 将用户主题作为 user 消息传入 LLM
#   2. LLM 决定调用 search_news（可多次，使用不同关键词）
#   3. 每次工具调用结果追加到消息历史，驱动下一轮 LLM 推理
#   4. LLM 调用 finish_search 或停止工具调用时，进入后处理阶段
#   5. 后处理：去重 → 按时间排序 → 取前 15 条 → 格式化推送
# =============================================================

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator

import ollama  # Ollama Python SDK，用于与本地 LLM 通信

from tools import search_news, deduplicate_news, sort_and_limit, format_for_frontend

logger = logging.getLogger("agent")

# 使用的本地模型名称（需在 Ollama 中已拉取）
MODEL = "gemma4:31b"

# =============================================================
# LLM 系统提示词
# 用英文以获得更稳定的工具调用行为
# =============================================================
SYSTEM_PROMPT = """You are a professional news search agent. Your task is to find the latest news on the given topic.

Rules:
1. Call search_news 2-3 times using different query variations (e.g. the topic itself, topic + "latest", topic + "news 2025").
2. After gathering enough results, call finish_search to complete the task.
3. Do not explain or chat — only call tools."""

# =============================================================
# 工具定义（OpenAI Function Calling 格式，Ollama 兼容）
#
# LLM 在推理时会从这里选择要调用的工具，并填充参数。
# 工具的实际执行由我们的 Python 代码完成，结果再回传给 LLM。
# =============================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "Search for the latest news articles on a topic via web search.",
            "parameters": {
                "type": "object",
                "properties": {
                    # LLM 填写搜索关键词，可以是主题本身或变体
                    "query": {"type": "string", "description": "Search query string"},
                    # 控制每次搜索返回的最大条数
                    "max_results": {"type": "integer", "description": "Max results (default 15)", "default": 15},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_search",
            "description": "Call this when you have completed all searches.",
            "parameters": {
                "type": "object",
                "properties": {
                    # LLM 说明结束搜索的原因，便于调试
                    "reason": {"type": "string", "description": "Why search is complete"}
                },
                "required": ["reason"],
            },
        },
    },
]


def _sse(event: str, data: Any) -> str:
    """
    将事件名称和数据序列化为标准 SSE 格式字符串。

    SSE 格式规范：
        event: <事件名>\n
        data: <JSON 字符串>\n
        \n  （空行表示一个事件结束）
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def run_agent(topic: str) -> AsyncGenerator[str, None]:
    """
    Agent 主循环，以异步生成器形式运行，边执行边 yield SSE 事件。

    Args:
        topic: 用户输入的搜索主题

    Yields:
        SSE 格式字符串，类型包括：
          status    - 进度更新
          news_item - 单条新闻数据
          done      - 全部完成信号
          error     - 错误信息
    """
    logger.info("=== Agent 启动  topic=%r  model=%s ===", topic, MODEL)
    agent_start = time.perf_counter()

    # 收集所有搜索到的原始文章（可能来自多次工具调用）
    all_articles = []

    # 初始化对话消息历史
    # system 消息定义 Agent 行为规则；user 消息触发搜索任务
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Find the latest top 15 news about: {topic}"},
    ]

    # 推送第一个状态事件，告知前端 Agent 已启动
    yield _sse("status", {"step": 1, "message": f'正在启动 Agent，搜索主题「{topic}」...'})

    # =============================================================
    # Agent 主循环：最多进行 6 轮 LLM 推理
    # 每轮：LLM 决策 → 执行工具 → 将结果追加到消息历史 → 下一轮
    # =============================================================
    for iteration in range(6):
        logger.info("--- 第 %d 轮 LLM 调用 ---", iteration + 1)
        t0 = time.perf_counter()

        # 调用 Ollama（同步阻塞），放入线程池避免阻塞事件循环
        try:
            response = await asyncio.to_thread(
                ollama.chat,
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                options={"temperature": 0.2},  # 低温度使工具调用更确定性
            )
        except Exception as e:
            logger.error("Ollama 调用失败: %s", e, exc_info=True)
            yield _sse("error", {"message": f"模型调用失败: {e}"})
            return

        elapsed = time.perf_counter() - t0
        msg = response.message
        logger.info(
            "LLM 响应  耗时=%.2fs  content=%r  tool_calls=%s",
            elapsed,
            (msg.content or "")[:80],
            [tc.function.name for tc in msg.tool_calls] if msg.tool_calls else "无",
        )

        # 将 assistant 的响应追加到消息历史（含工具调用指令）
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": msg.tool_calls,
        })

        # 若 LLM 未发出任何工具调用，说明它认为任务已完成
        if not msg.tool_calls:
            logger.info("模型未调用工具，Agent 主动结束")
            break

        # 依次执行本轮所有工具调用
        done = False
        for tool_call in msg.tool_calls:
            fn   = tool_call.function.name
            args = tool_call.function.arguments or {}
            logger.debug("工具调用  fn=%s  args=%s", fn, args)

            if fn == "search_news":
                query       = args.get("query", topic)
                max_results = int(args.get("max_results", 15))

                # 推送搜索进度给前端
                yield _sse("status", {"step": 2, "message": f'搜索中：{query}'})

                # 在线程池中执行同步的 HTTP 请求（Google News RSS）
                results = await asyncio.to_thread(search_news, query, max_results)
                all_articles.extend(results)
                logger.info("工具返回  query=%r  本次=%d  累计=%d", query, len(results), len(all_articles))

                yield _sse("status", {
                    "step": 2,
                    "message": f'找到 {len(results)} 条（累计 {len(all_articles)} 条）',
                })

                # 将工具执行结果追加到消息历史，供下一轮 LLM 参考
                # 只传标题/来源/日期，不传正文，减少 token 消耗
                messages.append({
                    "role": "tool",
                    "name": fn,
                    "content": json.dumps(
                        [
                            {
                                "title":  r.get("title"),
                                "source": r.get("source"),
                                "date":   r.get("date"),
                            }
                            for r in results
                        ],
                        ensure_ascii=False,
                    ),
                })

            elif fn == "finish_search":
                # LLM 主动宣告搜索完成
                reason = args.get("reason", "")
                logger.info("Agent 调用 finish_search  reason=%r", reason)
                messages.append({"role": "tool", "name": fn, "content": "Search completed."})
                done = True

        # 收到 finish_search 信号，退出主循环
        if done:
            break

    # =============================================================
    # 后处理阶段
    # =============================================================

    yield _sse("status", {"step": 3, "message": "正在去重、排序..."})

    # 兜底：若 Agent 全程未收集到任何文章（如模型拒绝调用工具），
    # 直接用原始主题执行一次搜索
    if not all_articles:
        logger.warning("Agent 未收集到任何文章，启动兜底搜索  topic=%r", topic)
        yield _sse("status", {"step": 2, "message": f"兜底搜索：{topic}"})
        all_articles = await asyncio.to_thread(search_news, topic, 15)

    # 去重（基于 URL 和标题前 60 字符）→ 按发布时间降序 → 截取前 15 条
    unique    = deduplicate_news(all_articles)
    top15     = sort_and_limit(unique, limit=15)
    formatted = format_for_frontend(top15)

    total_elapsed = time.perf_counter() - agent_start
    logger.info(
        "=== Agent 完成  topic=%r  结果=%d 条  总耗时=%.2fs ===",
        topic, len(formatted), total_elapsed,
    )

    # 推送完成状态
    yield _sse("status", {"step": 4, "message": f"完成，共 {len(formatted)} 条新闻"})

    # 逐条推送新闻给前端（前端收到后立即渲染，无需等待全部完成）
    for article in formatted:
        yield _sse("news_item", article)

    # 推送结束信号，前端据此关闭加载状态
    yield _sse("done", {"total": len(formatted)})
