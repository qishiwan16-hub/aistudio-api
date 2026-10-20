const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = 18082;
const BASE = path.join(process.env.HOME || '/data/data/com.termux/files/home', 'aistudio-api');

const LOG_FILES = {
  main:   path.join(BASE, 'aistudio.log'),
  bridge: path.join(BASE, 'bridge.log'),
  panel:  path.join(BASE, 'panel.log')
};

const PAGES = {
  requests: path.join(BASE, 'requests.html'),
  logs:     path.join(BASE, 'logviewer.html')
};

const ERR_RE = /error|exception|traceback|fail|critical|blocked|block.?reason|拦截|refused|denied|reject|429|500|502|503|timeout|超时|过载|throttl|limit|rate.?limit|quota|truncat|cut.?off|压缩|截断|max.?token|finish.?reason|content.?filter|safety|safety.?rating|recitation|overload|HARM_CATEGORY|PROHIBITED|RESOURCE_EXHAUSTED|UNAVAILABLE|candidates.*\[\]|candidates.*null|BLOCKED|SAFETY|MAX_TOKENS|prompt.?blocked|finish_reason/i;

function tailFile(fp, n) {
  try { return fs.readFileSync(fp, 'utf8').split('\n').slice(-n); }
  catch(e) { return ['(日志未找到: ' + fp + ')']; }
}

function getErrors(n) {
  let all = [];
  Object.entries(LOG_FILES).forEach(([name, fp]) => {
    try {
      const lines = fs.readFileSync(fp, 'utf8').split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (ERR_RE.test(lines[i])) {
          const s = Math.max(0, i-1), e = Math.min(lines.length-1, i+2);
          for (let j = s; j <= e; j++) all.push('[' + name.toUpperCase() + '] ' + lines[j]);
          all.push('');
          i = e;
        }
      }
    } catch(e) {}
  });
  return all.slice(-n);
}

/* ===== 解析请求记录 ===== */
function parseRequests() {
  let lines;
  try { lines = fs.readFileSync(LOG_FILES.main, 'utf8').split('\n'); }
  catch(e) { return []; }

  const reqs = [];
  const reqRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*收到.*请求.*model=(.+)/;
  const infoRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:Gemini 流式请求|Gemini 生成请求|流式请求|假流式请求|抗截断请求|请求).*model=([^,]+)(?:.*对话轮数=(\d+))?(?:.*尝试第\s*(\d+)\s*次)?/;
  const postRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*HTTP POST.*models\/([^:/ ]+):.*->\s*(\d+)\s*\(([\d.]+)ms\)/;
  const hookRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*Hook 拦截成功.*model=([^,]+)(?:.*snapshot=(\d+)\s*chars)?(?:.*body=(\d+)\s*chars)?/;
  const doneRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:假流式生成完成|Gemini 流式完成|Gemini 生成完成|抗截断生成完成|流式完成).*model=([^,]+)(?:.*输入Tokens=(\d+))?(?:.*输出Tokens=(\d+))?(?:.*总内容=(\d+))?/;
  const geminiErrRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*\[aistudio\.api\.gemini\].*(?:未知严重异常|严重错误|Gemini错误|流式错误)[：:]\s*(.+)/;
  const httpErrRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*HTTP (?:GET|POST)\s+(\S+)\s*->\s*(\d+)\s*\(([\d.]+)ms(?:,\s*(.+))?\)/;
  const errRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:ERROR|error|SAFETY|BLOCKED|HARM|RECITATION|RESOURCE_EXHAUSTED|finish_reason.*(?:SAFETY|RECITATION|OTHER))(.+)/i;
  const idleRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*浏览器已空闲.*?(\d+\.?\d*)\s*秒/;

  /* 用一个 map 按时间+模型聚合 */
  const map = new Map();
  /* 标准化模型名: models/gemini-4-argon → gemini-4-argon */
  function normModel(m) { return m.replace(/^models\//, '').trim(); }
  function getKey(time, model) { return time + '|' + normModel(model); }
  function ensure(time, model) {
    const k = getKey(time, model);
    if (!map.has(k)) {
      map.set(k, { time, model: normModel(model), status: 200, latency: '', turns: '', hookSnap: '', hookBody: '', type: 'ok', detail: '', inTokens: '', outTokens: '', resultText: '', retry: '', mode: '', _ts: Date.parse(time.replace(' ','T')+'Z') || 0 });
    }
    return map.get(k);
  }

  for (let i = 0; i < lines.length; i++) {
    const L = lines[i];
    let m;

    /* 收到请求 */
    m = L.match(reqRe);
    if (m) { ensure(m[1], m[2].trim()); continue; }

    /* 请求详情（对话轮数、重试次数、模式） */
    m = L.match(infoRe);
    if (m) {
      const r = ensure(m[1], m[2].trim());
      if (m[3]) r.turns = m[3];
      if (m[4]) r.retry = '第' + m[4] + '次';
      if (L.includes('假流式')) r.mode = '假流式';
      else if (L.includes('抗截断')) r.mode = '抗截断';
      else if (L.includes('Gemini 流式')) r.mode = '流式';
      else if (L.includes('Gemini 生成')) r.mode = '非流式';
      else if (L.includes('流式')) r.mode = '流式';
      continue;
    }

    /* HTTP POST 状态码+耗时 */
    m = L.match(postRe);
    if (m) {
      const r = ensure(m[1], m[2].trim());
      r.status = parseInt(m[3]);
      r.latency = m[4] + 'ms';
      if (r.status >= 400) r.type = 'err';
      continue;
    }

    /* Hook 拦截 - 时间可能差几秒，找最近的同模型请求 */
    m = L.match(hookRe);
    if (m) {
      const hTime = m[1], hModel = normModel(m[2].trim());
      /* 先精确匹配，再找10秒内同模型的最近请求 */
      let r = map.get(getKey(hTime, hModel));
      if (!r) {
        const hTs = Date.parse(hTime.replace(' ','T')+'Z') || 0;
        let best = null, bestDiff = Infinity;
        for (const v of map.values()) {
          if (normModel(v.model) === hModel) {
            const diff = Math.abs(v._ts - hTs);
            if (diff < 10000 && diff < bestDiff) { best = v; bestDiff = diff; }
          }
        }
        r = best || ensure(hTime, hModel);
      }
      r.type = 'hook';
      if (m[3]) r.hookSnap = m[3] + ' chars';
      if (m[4]) r.hookBody = m[4] + ' chars';
      continue;
    }

    /* 完成行（token 数据） */
    m = L.match(doneRe);
    if (m) {
      const dTime = m[1], dModel = normModel(m[2].trim());
      const dTs = Date.parse(dTime.replace(' ','T')+'Z') || 0;
      /* 找最近10分钟内同模型请求 */
      let best = null, bestDiff = Infinity;
      for (const v of map.values()) {
        if (normModel(v.model) === dModel) {
          const diff = dTs - v._ts;
          if (diff >= 0 && diff < 600000 && diff < bestDiff) { best = v; bestDiff = diff; }
        }
      }
      const r = best || ensure(dTime, dModel);
      if (m[3]) r.inTokens = m[3];
      if (m[4]) r.outTokens = m[4];
      /* 抗截断完成行用 总内容=N字符 而非 Tokens */
      if (m[5]) r.outTokens = m[5] + '字符';
      r.resultText = '✅ 生成完成';
      /* 计算实际生成耗时（从请求到完成） */
      if (best && bestDiff > 0) r.latency = (bestDiff / 1000).toFixed(1) + 's';
      continue;
    }

    /* Gemini 错误行（网络断开等） */
    m = L.match(geminiErrRe);
    if (m) {
      const eTs = Date.parse(m[1].replace(' ','T')+'Z') || 0;
      let best = null, bestDiff = Infinity;
      for (const v of map.values()) {
        const diff = eTs - v._ts;
        if (diff >= 0 && diff < 600000 && diff < bestDiff) { best = v; bestDiff = diff; }
      }
      if (best) {
        best.type = 'err';
        best.resultText = '❌ 失败';
        best.detail = m[2].trim();
      }
      continue;
    }

    /* HTTP 错误行（如 /v1/models 500） */
    m = L.match(httpErrRe);
    if (m) {
      const status = parseInt(m[3]);
      if (status >= 400) {
        const ep = m[2] || 'unknown';
        const r = ensure(m[1], ep);
        r.status = status;
        r.latency = m[4] + 'ms';
        r.type = 'err';
        r.resultText = '❌ ' + status + ' 错误';
        if (m[5]) r.detail = m[5].trim();
      }
      continue;
    }

    /* 浏览器空闲回收 */
    m = L.match(idleRe);
    if (m) {
      const r = ensure(m[1], '系统');
      r.type = 'ok';
      r.resultText = '💤 浏览器空闲回收';
      r.detail = '闲置 ' + Math.round(parseFloat(m[2])) + ' 秒后自动释放内存';
      continue;
    }

    /* 通用异常行 */
    m = L.match(errRe);
    if (m) {
      const last = [...map.values()].pop();
      if (last && !last.detail) {
        last.type = 'err';
        if (!last.resultText) last.resultText = '❌ 异常';
        last.detail = m[2].trim();
      }
    }
  }

  /* 转数组并按时间排序 */
  /* 过滤掉没有实质信息的空条目（如 HTTP POST /v1/chat/completions 被误匹配的） */
  return [...map.values()]
    .filter(r => r.model !== 's' && r.model !== 'completions' && r.model.length > 1)
    .sort((a, b) => a._ts - b._ts)
    .slice(-200);
}

const server = http.createServer((req, res) => {
  const parsed = url.parse(req.url, true);
  const pn = parsed.pathname;
  res.setHeader('Access-Control-Allow-Origin', '*');

  /* 首页 → 请求监控 */
  if (pn === '/' || pn === '/index.html') {
    try { res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); res.end(fs.readFileSync(PAGES.requests, 'utf8')); }
    catch(e) { res.writeHead(500); res.end('Page not found'); }
    return;
  }

  /* 日志页 */
  if (pn === '/logs') {
    try { res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); res.end(fs.readFileSync(PAGES.logs, 'utf8')); }
    catch(e) { res.writeHead(500); res.end('Page not found'); }
    return;
  }

  /* 请求 API */
  if (pn === '/api/requests') {
    const data = parseRequests();
    res.writeHead(200, {'Content-Type':'application/json; charset=utf-8'});
    res.end(JSON.stringify(data));
    return;
  }

  /* 日志 API */
  const logMatch = pn.match(/^\/api\/log\/(main|bridge|panel)$/);
  if (logMatch) {
    const n = parseInt(parsed.query.lines) || 200;
    res.writeHead(200, {'Content-Type':'text/plain; charset=utf-8'});
    res.end(tailFile(LOG_FILES[logMatch[1]], n).join('\n'));
    return;
  }

  if (pn === '/api/log/errors') {
    const n = parseInt(parsed.query.lines) || 300;
    res.writeHead(200, {'Content-Type':'text/plain; charset=utf-8'});
    res.end(getErrors(n).join('\n'));
    return;
  }

  res.writeHead(404); res.end('Not Found');
});

server.listen(PORT, '0.0.0.0', () => {
  console.log('Log Viewer running at http://localhost:' + PORT);
});
