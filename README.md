# Agent News — 智能新闻搜索

基于 **Ollama + Gemma 4（本地大模型）** 驱动的 AI 新闻搜索应用，前后端分离架构。

用户输入感兴趣的主题，Agent 自主规划搜索策略、调用 Google News RSS 获取最新新闻，经去重和时间排序后以流式方式实时返回 Top 15 新闻，每条包含标题、摘要、来源和发布时间。

---

## 功能特点

- **Agent 驱动**：由本地 LLM 自主决定搜索关键词变体，多轮搜索覆盖更全面
- **流式返回**：基于 SSE（Server-Sent Events），新闻边搜索边显示，无需等待全部完成
- **无限流限制**：使用 Google News RSS，无需 API Key，无速率限制
- **自动去重**：基于 URL 和标题双维度去重，过滤转载重复新闻
- **时间排序**：所有结果按发布时间降序排列，最新新闻优先
- **双端日志**：前端/后端日志分别写入独立文件，方便调试
- **完全本地运行**：模型运行在本地 Ollama，无数据外传

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 前端 | 原生 HTML / CSS / JavaScript | 无需构建工具，直接浏览器打开 |
| 后端 | Python + FastAPI | 提供 REST API 和 SSE 流式接口 |
| AI 模型 | Ollama + Gemma 4:31b | 本地运行，负责 Tool Calling 决策 |
| 新闻数据源 | Google News RSS + feedparser | 免费，无需注册，无限流 |

---

## 项目结构

```
agent-news/
├── frontend/
│   ├── index.html      # 页面结构：搜索框、进度卡片、新闻卡片网格
│   ├── style.css       # 样式：响应式布局、卡片动画、进度步骤
│   └── app.js          # 前端逻辑：SSE 解析、卡片渲染、日志上报
├── backend/
│   ├── main.py         # FastAPI 入口：路由定义、日志配置
│   ├── agent.py        # Agent 主循环：LLM 工具调用、SSE 事件生成
│   ├── tools.py        # 工具函数：搜索、去重、排序、格式化
│   └── requirements.txt
├── logs/               # 运行时自动创建
│   ├── backend.log     # 后端日志（含 Agent 执行过程）
│   └── frontend.log    # 前端日志（用户操作和请求过程）
└── README.md
```

---

## 快速开始

### 前置条件

- Python 3.9+
- [Ollama](https://ollama.com) 已安装并运行
- 已拉取 Gemma 4 模型

```bash
# 确认 Ollama 正在运行
ollama serve

# 拉取模型（若尚未拉取）
ollama pull gemma4:31b

# 确认模型已就绪
ollama list
```

### 安装依赖

```bash
cd agent-news/backend
pip install -r requirements.txt
```

### 启动后端

```bash
python main.py
# 服务启动在 http://localhost:8000
```

### 打开前端

直接用浏览器打开 `frontend/index.html`，无需任何构建步骤。

---

## 使用方法

1. 在搜索框中输入感兴趣的主题（中英文均可）
2. 点击「搜索」或按 Enter 键
3. 观察进度卡片，Agent 会经历以下阶段：
   - **启动 Agent** → LLM 开始规划搜索策略
   - **搜索新闻** → 调用 Google News RSS（可能多轮）
   - **去重排序** → 过滤重复，按时间排列
   - **完成** → 新闻卡片全部渲染完毕
4. 点击新闻标题或「阅读原文」跳转到原始文章

---

## Agent 工作流程

```
用户输入主题
    │
    ▼
[LLM 推理] Gemma 4 收到主题，决定搜索策略
    │
    ├──► 调用 search_news("主题")
    ├──► 调用 search_news("主题 latest 2025")
    └──► 调用 search_news("主题 news")
              │
              ▼ （每次搜索结果追加到消息历史）
    [LLM 推理] 判断结果是否足够 → 调用 finish_search
              │
              ▼
       后处理：去重 → 时间排序 → 取前 15 条
              │
              ▼
       SSE 流式推送每条新闻给前端（实时渲染）
```

---

## 日志说明

后端运行后，`logs/` 目录自动创建：

| 文件 | 内容 |
|------|------|
| `backend.log` | 请求接收、LLM 调用耗时、工具执行结果、去重排序详情 |
| `frontend.log` | 用户操作、SSE chunk 统计、每条新闻的渲染记录 |

日志单文件最大 5MB，自动轮转，最多保留 3 个历史文件。

**查看实时日志：**
```bash
# 实时追踪后端日志
tail -f logs/backend.log

# 实时追踪前端日志
tail -f logs/frontend.log
```

---

## 接口文档

启动后访问 [http://localhost:8000/docs](http://localhost:8000/docs) 查看 FastAPI 自动生成的接口文档。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/search` | 搜索新闻，SSE 流式返回 |
| POST | `/api/log` | 接收前端日志写入文件 |
| GET  | `/health` | 健康检查 |

---

## 常见问题

**Q：搜索后长时间无响应？**

A：Gemma 4:31b 模型较大，首次 LLM 调用可能需要 10～30 秒，属于正常现象。可通过 `tail -f logs/backend.log` 查看实际进度。

**Q：返回结果为 0 条？**

A：检查网络是否可以访问 `news.google.com`，或尝试换用英文关键词。

**Q：如何更换模型？**

A：修改 `backend/agent.py` 第一行的 `MODEL` 变量为已拉取的其他模型名称即可。

---

## 开发说明

后端启用了 `reload=True`，修改 Python 文件后自动重启，无需手动操作。

前端为纯静态文件，修改后刷新浏览器即可生效。
