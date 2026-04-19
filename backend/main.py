# =============================================================
# main.py —— FastAPI 后端入口
#
# 职责：
#   1. 提供 /api/search 接口，接收搜索主题并以 SSE 流式返回新闻
#   2. 提供 /api/log 接口，接收前端日志并写入 frontend.log
#   3. 在应用启动后统一配置日志系统，避免被 uvicorn 覆盖
# =============================================================

import logging
import logging.handlers
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 导入 Agent 主循环（负责调用 LLM 并搜索新闻）
from agent import run_agent

# =============================================================
# 日志相关常量
# =============================================================

# 日志目录：backend/ 的上一级，即项目根目录下的 logs/
LOG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")

# 日志格式：时间 [级别] 模块名: 消息
LOG_FMT  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE = "%Y-%m-%d %H:%M:%S"


def _file_handler(path: str) -> logging.handlers.RotatingFileHandler:
    """
    创建一个滚动文件日志 handler。
    - 单个文件最大 5MB，超过后自动轮转
    - 最多保留 3 个历史文件（backend.log.1 / .2 / .3）
    """
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    h.setFormatter(logging.Formatter(LOG_FMT, datefmt=LOG_DATE))
    return h


def _setup_logging() -> None:
    """
    统一配置日志系统。

    必须在 FastAPI startup 事件中调用，而不能放在模块顶层。
    原因：uvicorn 使用 reload=True 时会在子进程中重新初始化自己的日志配置，
    若在模块顶层设置，会被 uvicorn 启动流程覆盖掉，导致日志文件无法正常写入。
    在 startup 事件中设置则保证在 uvicorn 完成初始化之后再执行，不会被覆盖。

    日志分流策略：
      - 根 logger (root)  → backend.log + 控制台（终端实时可见）
      - frontend logger   → frontend.log（只记录前端发来的日志，不混入后端）
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    # 控制台 handler：输出到终端，时间格式精简为 HH:MM:SS
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FMT, datefmt="%H:%M:%S"))

    # 配置根 logger：清除 uvicorn 已添加的默认 handler，换成我们的
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(_file_handler(os.path.join(LOG_DIR, "backend.log")))
    root.addHandler(console)

    # 配置前端专用 logger：
    #   propagate=False 表示日志不向上传递给 root logger，
    #   从而只写入 frontend.log，不会同时出现在 backend.log 和终端
    fe = logging.getLogger("frontend")
    fe.setLevel(logging.DEBUG)
    fe.handlers.clear()
    fe.addHandler(_file_handler(os.path.join(LOG_DIR, "frontend.log")))
    fe.propagate = False


# =============================================================
# FastAPI 应用初始化
# =============================================================

app = FastAPI(title="Agent News API")

# 允许跨域请求（前端以 file:// 协议打开时 origin 为 null，需要放行）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================
# 应用生命周期事件
# =============================================================

@app.on_event("startup")
async def startup():
    """
    FastAPI 应用启动回调。
    uvicorn 完成自身初始化后触发，此时配置日志才不会被覆盖。
    """
    _setup_logging()
    logging.getLogger("main").info("日志系统初始化完成  backend.log / frontend.log")


# 获取本模块专属的 logger（名称为 "main"，日志中可区分来源）
logger = logging.getLogger("main")


# =============================================================
# 请求/响应数据模型
# =============================================================

class SearchRequest(BaseModel):
    topic: str  # 用户输入的搜索主题


class LogRequest(BaseModel):
    level: str    # 日志级别：DEBUG / INFO / WARN / ERROR
    message: str  # 日志内容


# =============================================================
# API 路由
# =============================================================

@app.post("/api/search")
async def search(req: SearchRequest, request: Request):
    """
    新闻搜索接口。

    接收用户输入的主题，启动 Agent，以 SSE（Server-Sent Events）
    流式返回进度状态和新闻列表。前端通过 Fetch API 读取流。

    SSE 事件类型：
      - status   : 进度更新 {"step": 1-4, "message": "..."}
      - news_item: 单条新闻 {"id", "title", "summary", "url", "source", "date"}
      - done     : 完成信号 {"total": N}
      - error    : 错误信息 {"message": "..."}
    """
    topic = req.topic.strip()
    logger.info(">>> 收到搜索请求  topic=%r  client=%s", topic, request.client.host)

    if not topic:
        logger.warning("请求主题为空，拒绝")
        return {"error": "请输入搜索主题"}

    start = time.perf_counter()

    async def stream_with_log():
        # 遍历 Agent 生成器，将每个 SSE 数据块直接透传给前端
        async for chunk in run_agent(topic):
            yield chunk
        elapsed = time.perf_counter() - start
        logger.info("<<< 请求完成  topic=%r  耗时=%.2fs", topic, elapsed)

    return StreamingResponse(
        stream_with_log(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",       # 禁止代理/浏览器缓存 SSE 流
            "X-Accel-Buffering": "no",          # 禁止 Nginx 缓冲，确保实时推送
        },
    )


@app.post("/api/log")
async def frontend_log(req: LogRequest):
    """
    前端日志接收接口。

    前端 app.js 中的 logger 会将每条日志 POST 到此接口，
    由后端写入 logs/frontend.log，方便调试时集中查看。
    """
    level_map = {
        "DEBUG":   logging.DEBUG,
        "INFO":    logging.INFO,
        "WARN":    logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR":   logging.ERROR,
    }
    logging.getLogger("frontend").log(
        level_map.get(req.level.upper(), logging.INFO),
        req.message,
    )
    return {"ok": True}


@app.get("/health")
async def health():
    """健康检查接口，可用于监控或 Docker healthcheck。"""
    logger.debug("health check")
    return {"status": "ok"}


# =============================================================
# 直接运行入口
# =============================================================

if __name__ == "__main__":
    import uvicorn
    # reload=True：开发模式，修改代码后自动重启
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
