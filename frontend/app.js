// =============================================================
// app.js —— 前端核心逻辑
//
// 职责：
//   1. 接收用户输入的搜索主题
//   2. 向后端发起 POST 请求，通过 Fetch API 读取 SSE 流
//   3. 实时解析 SSE 事件，更新进度状态和渲染新闻卡片
//   4. 将调试日志同步发送到后端，写入 logs/frontend.log
// =============================================================

// 后端接口地址
const API_URL = "http://localhost:8000/api/search";  // 新闻搜索接口
const LOG_URL = "http://localhost:8000/api/log";      // 前端日志上报接口

// 页面 DOM 元素引用（页面加载后立即获取，避免重复查询）
const input       = document.getElementById("topic-input");   // 搜索输入框
const btn         = document.getElementById("search-btn");    // 搜索按钮
const statusSec   = document.getElementById("status-section"); // 进度状态卡片区域
const statusMsg   = document.getElementById("status-msg");    // 进度文字描述
const newsGrid    = document.getElementById("news-grid");     // 新闻卡片网格容器
const resultsHdr  = document.getElementById("results-header"); // 结果列表标题行
const resultCount = document.getElementById("result-count");  // 当前新闻数量显示
const steps       = document.querySelectorAll(".step");       // 所有进度步骤节点

// =============================================================
// 前端日志模块（IIFE 立即执行函数，封装成模块）
//
// 功能：
//   - 输出到浏览器控制台（console.debug/info/warn/error）
//   - 同时 POST 到后端 /api/log 接口，写入 logs/frontend.log
//   - 使用 fire-and-forget（不 await），日志失败不影响主流程
// =============================================================
const logger = (() => {
  /**
   * 内部发送函数
   * @param {string} level - 日志级别：DEBUG / INFO / WARN / ERROR
   * @param {string} msg   - 格式化后的日志内容
   */
  function send(level, msg) {
    // 1. 输出到浏览器控制台，方便开发者实时查看
    const fn = { DEBUG: "debug", INFO: "info", WARN: "warn", ERROR: "error" }[level];
    console[fn](`[Frontend][${level}] ${msg}`);

    // 2. 异步 POST 到后端写入文件（fire-and-forget，.catch 吞掉错误避免控制台噪音）
    fetch(LOG_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, message: msg }),
    }).catch(() => {});
  }

  /**
   * 格式化多个参数为单个字符串
   * 对象类型自动 JSON.stringify，其余转为字符串，以空格连接
   */
  function fmt(msg, args) {
    return [msg, ...args.map(a => (typeof a === "object" ? JSON.stringify(a) : String(a)))].join(" ");
  }

  // 暴露四个级别的日志方法
  return {
    debug: (msg, ...args) => send("DEBUG", fmt(msg, args)),
    info:  (msg, ...args) => send("INFO",  fmt(msg, args)),
    warn:  (msg, ...args) => send("WARN",  fmt(msg, args)),
    error: (msg, ...args) => send("ERROR", fmt(msg, args)),
  };
})();

// =============================================================
// SSE 流解析器
//
// 背景：使用 Fetch API 读取流时，每次 reader.read() 返回的 chunk
// 不一定按照 SSE 事件边界对齐，可能一个 chunk 包含多个事件，
// 也可能一个事件被拆分在两个 chunk 中。
//
// 解决方案：维护一个字符串缓冲区，每次 feed() 新数据后，
// 按双换行（\n\n）分割出完整的事件块进行解析。
// =============================================================
class SSEParser {
  constructor() {
    this.buf = ""; // 未完成事件的缓冲区
  }

  /**
   * 向解析器喂入新收到的文本数据
   * @param {string} chunk - 从流中读取并解码的文本片段
   * @returns {Array} 解析出的完整事件数组，每项为 { type, data }
   */
  feed(chunk) {
    this.buf += chunk;

    // 按双换行分割，每个完整的 SSE 事件以空行结尾
    const blocks = this.buf.split("\n\n");

    // 最后一个元素可能是不完整的事件，保留在缓冲区等待后续数据
    this.buf = blocks.pop();

    const events = blocks.map(b => this._parse(b)).filter(Boolean);
    if (events.length) logger.debug(`SSEParser: 解析出 ${events.length} 个事件`);
    return events;
  }

  /**
   * 解析单个 SSE 事件块
   * SSE 格式：
   *   event: <事件名>\n
   *   data: <JSON字符串>\n
   *
   * @param {string} block - 一个完整的 SSE 事件文本块
   * @returns {{ type: string, data: any } | null}
   */
  _parse(block) {
    let type = "message"; // 默认事件类型
    let data = "";

    for (const line of block.split("\n")) {
      if (line.startsWith("event: "))      type = line.slice(7).trim();
      else if (line.startsWith("data: "))  data = line.slice(6).trim();
    }

    if (!data) return null; // 空数据块，忽略

    try {
      return { type, data: JSON.parse(data) }; // 正常 JSON
    } catch {
      return { type, data }; // 解析失败则保留原始字符串
    }
  }
}

// =============================================================
// UI 辅助函数
// =============================================================

/**
 * 更新进度步骤的视觉状态
 * - 编号小于 n 的步骤标记为"已完成"（绿色）
 * - 编号等于 n 的步骤标记为"进行中"（蓝色脉冲动画）
 * - 编号大于 n 的步骤保持默认（灰色）
 *
 * @param {number} n - 当前活跃的步骤编号（从 1 开始）
 */
function setStep(n) {
  steps.forEach((el, i) => {
    el.classList.remove("active", "done");
    if (i + 1 < n)      el.classList.add("done");
    else if (i + 1 === n) el.classList.add("active");
  });
}

/**
 * 更新状态卡片中的文字描述
 * @param {string} msg - 要显示的状态文字
 */
function setStatus(msg) {
  statusMsg.textContent = msg;
}

// =============================================================
// 新闻卡片构建
// =============================================================

/**
 * 根据新闻数据创建一个卡片 DOM 元素
 *
 * 卡片结构：
 *   [图片 / 占位符]
 *   来源徽章 + 发布时间
 *   标题（可点击跳转）
 *   摘要（最多显示 3 行）
 *   "阅读原文" 链接
 *
 * @param {Object} item - 后端返回的单条新闻数据
 * @returns {HTMLElement} 构建好的卡片元素
 */
function buildCard(item) {
  const card = document.createElement("div");
  card.className = "news-card";

  // Google News RSS 不提供图片，当 image 为空时显示占位符
  const imgHTML = item.image
    ? `<img class="card-image" src="${item.image}" alt="" loading="lazy"
           onerror="this.replaceWith(placeholder())">`  // 图片加载失败时替换为占位符
    : `<div class="card-image-placeholder">📰</div>`;

  // 使用 innerHTML 批量设置内容（注意所有用户数据都经过 escHtml 转义）
  card.innerHTML = `
    ${imgHTML}
    <div class="card-body">
      <div class="card-meta">
        <span class="source-badge">${escHtml(item.source)}</span>
        <span class="card-date">${escHtml(item.date)}</span>
      </div>
      <a class="card-title" href="${item.url}" target="_blank" rel="noopener">
        ${escHtml(item.title)}
      </a>
      <p class="card-summary">${escHtml(item.summary)}</p>
    </div>
    <div class="card-footer">
      <a class="read-more" href="${item.url}" target="_blank" rel="noopener">
        阅读原文 ↗
      </a>
    </div>`;
  return card;
}

/**
 * 创建图片加载失败时的占位符元素
 * （被 img 标签的 onerror 属性调用）
 */
function placeholder() {
  const d = document.createElement("div");
  d.className = "card-image-placeholder";
  d.textContent = "📰";
  return d;
}

/**
 * 对字符串进行 HTML 转义，防止 XSS 注入
 * 将 & < > " 等特殊字符替换为对应的 HTML 实体
 *
 * @param {string} str - 待转义的字符串
 * @returns {string} 安全的 HTML 字符串
 */
function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// =============================================================
// 主搜索函数
// =============================================================

/**
 * 执行新闻搜索的主函数，由搜索按钮点击或回车键触发。
 *
 * 流程：
 *   1. 读取用户输入，重置 UI 状态
 *   2. POST 到 /api/search，获取 SSE 流式响应
 *   3. 循环读取流数据，解析 SSE 事件
 *   4. 根据事件类型分别处理：更新进度 / 渲染卡片 / 完成 / 错误
 */
async function doSearch() {
  const topic = input.value.trim();
  if (!topic) { input.focus(); return; } // 输入为空时聚焦输入框并返回

  logger.info(`========== 新搜索开始  topic="${topic}" ==========`);

  // 重置 UI：清空旧结果，显示进度区域，禁用按钮防止重复提交
  newsGrid.innerHTML        = "";
  resultsHdr.style.display  = "none";
  statusSec.style.display   = "block";
  btn.disabled              = true;
  btn.textContent           = "搜索中...";
  setStep(1);
  setStatus(`正在启动 Agent，搜索「${topic}」...`);

  // 统计变量，用于日志和进度显示
  let count      = 0;   // 已渲染的新闻卡片数
  let byteCount  = 0;   // 累计接收字节数
  let chunkCount = 0;   // 累计接收 chunk 数

  const parser = new SSEParser();       // 创建 SSE 解析器实例
  const t0     = performance.now();     // 记录开始时间，用于计算总耗时

  try {
    logger.info(`发起 POST 请求  url=${API_URL}`);

    // 向后端发起搜索请求
    const res = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic }),
    });

    logger.info(`HTTP 响应  status=${res.status}  ok=${res.ok}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    // 获取可读流和解码器，用于逐块读取 SSE 数据
    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    logger.debug("开始读取 SSE 流...");

    // 循环读取流数据，直到流结束（done=true）
    while (true) {
      const { done, value } = await reader.read();

      if (done) {
        logger.info(`SSE 流读取完毕  总 chunks=${chunkCount}  总字节=${byteCount}`);
        break;
      }

      // 统计本次 chunk 信息
      chunkCount++;
      byteCount += value.byteLength;
      logger.debug(`收到 chunk #${chunkCount}  字节=${value.byteLength}  累计=${byteCount}B`);

      // 解码二进制数据为字符串，stream:true 表示数据可能不完整（多字节字符跨 chunk）
      const events = parser.feed(decoder.decode(value, { stream: true }));

      // 处理本次解析出的所有完整事件
      for (const ev of events) {
        logger.debug(`处理事件  type=${ev.type}  data=${JSON.stringify(ev.data).slice(0, 120)}`);

        if (ev.type === "status") {
          // 进度更新：同步步骤高亮和状态文字
          setStep(ev.data.step);
          setStatus(ev.data.message);
          logger.info(`[status] step=${ev.data.step}  msg="${ev.data.message}"`);

        } else if (ev.type === "news_item") {
          // 收到新闻条目：第一条时显示结果标题行，然后追加卡片
          if (count === 0) resultsHdr.style.display = "flex";
          newsGrid.appendChild(buildCard(ev.data));
          count++;
          resultCount.textContent = `共 ${count} 条`;
          logger.debug(
            `[news_item #${count}] title="${ev.data.title?.slice(0, 60)}"  source="${ev.data.source}"  date="${ev.data.date}"`
          );

        } else if (ev.type === "done") {
          // 搜索完成：更新状态文字，隐藏 loading 动画
          const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
          setStep(5); // 超出步骤数，所有步骤都变为已完成状态
          setStatus(`✅ 搜索完成，共找到 ${ev.data.total} 条新闻`);
          document.querySelector(".spinner").style.display = "none";
          logger.info(`[done] total=${ev.data.total}  前端总耗时=${elapsed}s`);

        } else if (ev.type === "error") {
          // 错误事件：显示错误信息，若没有任何结果则显示空状态
          setStatus(`❌ ${ev.data.message}`);
          if (count === 0) showEmpty(ev.data.message);
          logger.error(`[error] ${ev.data.message}`);
        }
      }
    }

  } catch (err) {
    // 网络错误或代码异常
    logger.error(`请求异常  ${err.message}`, err.stack ?? "");
    setStatus(`❌ 请求失败：${err.message}`);
    if (count === 0) showEmpty("无法连接到后端服务，请确认已启动。");

  } finally {
    // 无论成功还是失败，都恢复按钮状态
    btn.disabled    = false;
    btn.textContent = "搜索";
    logger.info(`========== 搜索结束  渲染卡片=${count} 张 ==========`);
  }
}

/**
 * 显示无结果的空状态提示
 * @param {string} msg - 提示信息
 */
function showEmpty(msg) {
  newsGrid.innerHTML = `
    <div class="empty-state" style="grid-column:1/-1">
      <div class="icon">🔍</div>
      <h3>未找到结果</h3>
      <p>${escHtml(msg)}</p>
    </div>`;
}

// =============================================================
// 事件绑定
// =============================================================

// 点击搜索按钮触发搜索
btn.addEventListener("click", doSearch);

// 在输入框中按下 Enter 键触发搜索
input.addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });

// 页面脚本加载完成，记录日志（用于确认 JS 是否正常加载）
logger.info("app.js 加载完成，等待用户输入");
