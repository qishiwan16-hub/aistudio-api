const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = Number(process.env.LOGVIEWER_PORT || 18082);
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
  const reqRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*收到.*请求.*model=([^,\s]+)/;
  const infoRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:Gemini 流式请求|Gemini 生成请求|流式请求|假流式请求|抗截断请求|请求).*model=([^,]+)(?:.*对话轮数=(\d+))?(?:.*(?:尝试第\s*(\d+)\s*次|尝试=(\d+)\/\d+))?/;
  const postRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*HTTP POST.*models\/([^:/ ]+):.*->\s*(\d+)\s*\(([\d.]+)ms\)/;
  const hookRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:Hook 拦截成功|捕获请求模板成功).*model=([^,]+)(?:.*(?:snapshot|快照长度)=(\d+))?(?:.*(?:body|请求体长度)=(\d+))?/;
  const doneRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:假流式生成完成|Gemini 流式完成|Gemini 生成完成|抗截断生成完成|防外审生成完成|流式完成|流式请求完成|非流式请求完成).*model=([^,]+)(?:.*耗时=([\d.]+)ms)?(?:.*输入[Tt]okens=(\d+))?(?:.*输出[Tt]okens=(\d+))?(?:.*(?:总内容|输出字符)=(\d+))?/;
  const geminiErrRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*\[aistudio\.api\.gemini\].*(?:未知严重异常|严重错误|Gemini错误|流式错误|请求处理发生未捕获异常|响应解析异常)[：:]\s*(.+)/;
  const httpErrRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*HTTP (?:GET|POST)\s+(\S+)\s*->\s*(\d+)\s*\(([\d.]+)ms(?:,\s*(.+))?\)/;
  const errRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:ERROR|error|SAFETY|BLOCKED|HARM|RECITATION|RESOURCE_EXHAUSTED|finish_reason.*(?:SAFETY|RECITATION|OTHER))(.+)/i;
  const signRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*WAA 签名(成功|失败).*?auth_user=(\d+)(?:.*poolState=(\w+))?(?:.*耗时=([\d.]+)ms)?(?:.*(?:错误|响应)=(.+))?/;
  const directRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*直连 Google (?:流式开始|完成: 非流式).*?auth_user=(\d+), HTTP (\d+)/;
  const engineRe = /\[engine=browserless\]|直连 Google|WAA 签名/;
  const idleRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*浏览器已空闲.*?(\d+\.?\d*)\s*秒/;

  /* 用一个 map 按时间+模型聚合 */
  const map = new Map();
  /* 标准化模型名: models/gemini-4-argon → gemini-4-argon */
  function normModel(m) { return m.replace(/^models\//, '').trim(); }
  function getKey(time, model) { return time + '|' + normModel(model); }
  /* 签名/直连日志不带 model，挂到最近 10 分钟内的最新请求 */
  function latestOpen(ts) {
    let best = null;
    for (const v of map.values()) {
      const diff = ts - v._ts;
      if (diff >= 0 && diff < 600000 && (!best || v._ts >= best._ts)) best = v;
    }
    return best;
  }
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

    /* 无浏览器：WAA 签名 */
    m = L.match(signRe);
    if (m) {
      const r = latestOpen(Date.parse(m[1].replace(' ','T')+'Z') || 0);
      if (r) {
        r.engine = '无浏览器';
        r.account = 'u/' + m[3];
        r.sign = (m[2] === '成功' ? '✓' : '✗') + (m[4] ? ' ' + m[4] : '') + (m[5] ? ' ' + Math.round(parseFloat(m[5])) + 'ms' : '');
        if (m[2] === '失败') { r.type = 'err'; r.resultText = '❌ 签名失败'; if (m[6]) r.detail = m[6].trim(); }
      }
      continue;
    }
    /* 无浏览器：直连 Google 的 HTTP 状态 */
    m = L.match(directRe);
    if (m) {
      const r = latestOpen(Date.parse(m[1].replace(' ','T')+'Z') || 0);
      if (r) {
        r.engine = '无浏览器';
        r.account = 'u/' + m[2];
        r.status = parseInt(m[3]);
        if (r.status >= 400) r.type = 'err';
      }
      continue;
    }
    if (engineRe.test(L)) continue;

    /* 收到请求 */
    m = L.match(reqRe);
    if (m) { ensure(m[1], m[2].trim()); continue; }

    /* 请求详情（对话轮数、重试次数、模式） */
    m = (doneRe.test(L) || hookRe.test(L)) ? null : L.match(infoRe);
    if (m) {
      const r = ensure(m[1], m[2].trim());
      if (m[3]) r.turns = m[3];
      if (m[4] || m[5]) r.retry = '第' + (m[4] || m[5]) + '次';
      if (L.includes('假流式')) r.mode = '假流式';
      else if (L.includes('防外审')) r.mode = '防外审截断';
      else if (L.includes('抗截断')) r.mode = '抗截断';
      else if (L.includes('Gemini 流式')) r.mode = '流式';
      else if (L.includes('Gemini 生成') || L.includes('非流式')) r.mode = '非流式';
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

    /* Hook 拦截 - 合并回最近的同模型请求（生成可能需要几十秒到几分钟） */
    m = L.match(hookRe);
    if (m) {
      const hTime = m[1], hModel = normModel(m[2].trim());
      const hTs = Date.parse(hTime.replace(' ','T')+'Z') || 0;
      /* 找10分钟内同模型、尚未拦截的最近请求 */
      let best = null, bestDiff = Infinity;
      for (const v of map.values()) {
        if (normModel(v.model) === hModel && v.type !== 'hook') {
          const diff = hTs - v._ts;
          if (diff >= 0 && diff < 600000 && diff < bestDiff) { best = v; bestDiff = diff; }
        }
      }
      /* 找不到未拦截的，放宽到已拦截的（续传场景） */
      if (!best) {
        for (const v of map.values()) {
          if (normModel(v.model) === hModel) {
            const diff = hTs - v._ts;
            if (diff >= 0 && diff < 600000 && diff < bestDiff) { best = v; bestDiff = diff; }
          }
        }
      }
      const r = best || ensure(hTime, hModel);
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
      if (m[4] && m[4] !== 'None') r.inTokens = m[4];
      if (m[5] && m[5] !== 'None') r.outTokens = m[5];
      /* 抗截断完成行用 总内容=N字符 而非 Tokens */
      if (m[6]) { r.outTokens = m[6]; r._outUnit = '字符'; }
      r.resultText = '✅ 生成完成';
      /* 计算实际生成耗时（从请求到完成） */
      if (m[3]) r.latency = (parseFloat(m[3]) / 1000).toFixed(1) + 's';
      else if (best && bestDiff > 0) r.latency = (bestDiff / 1000).toFixed(1) + 's';
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

/* 引擎与 WAA 签名汇总（从最近一次启动开始统计） */
function parseEngine() {
  let lines;
  try { lines = fs.readFileSync(LOG_FILES.main, 'utf8').split('\n'); }
  catch(e) { return { engine: 'unknown' }; }
  const startRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*服务初始化完成: 引擎=(\w+)(?:, 签名服务=(\S+?),)?/;
  const okRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*WAA 签名成功: 服务=(\S+?), auth_user=(\d+).*?poolState=(\S+?),.*耗时=([\d.]+)ms/;
  const failRe = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*(?:WAA 签名失败|WAA 签名服务连接失败|WAA 签名服务异常)[:：]\s*(.+)/;
  let st = null;
  for (const L of lines) {
    let m = L.match(startRe);
    if (m) {
      st = { engine: m[2], since: m[1], server: m[3] || '', ok: 0, fail: 0, totalMs: 0, maxMs: 0,
             last: null, lastFail: null, pool: {} };
      continue;
    }
    if (!st) continue;
    m = L.match(okRe);
    if (m) {
      const ms = parseFloat(m[5]);
      st.ok++; st.totalMs += ms; if (ms > st.maxMs) st.maxMs = ms;
      st.server = st.server || m[2];
      st.pool[m[4]] = (st.pool[m[4]] || 0) + 1;
      st.last = { time: m[1], account: 'u/' + m[3], pool: m[4], ms: Math.round(ms), ok: true };
      continue;
    }
    m = L.match(failRe);
    if (m) {
      st.fail++;
      st.lastFail = { time: m[1], reason: m[2].trim().slice(0, 300) };
      st.last = { time: m[1], ok: false, reason: st.lastFail.reason };
    }
  }
  if (!st) return { engine: 'unknown' };
  st.avgMs = st.ok ? Math.round(st.totalMs / st.ok) : 0;
  st.maxMs = Math.round(st.maxMs);
  delete st.totalMs;
  return st;
}

/* ── pg-bridge 面板（只读预览）：数据来自 data/accounts + 日志里的签名记录 ── */
const TIER_HDR = { Pro: 'TIER1', Ultra: 'TIER2', Ultra_20X: 'TIER2', Plus: 'TIER0', Free: 'TIER0' };
function waaStatus() {
  const accDir = path.join(BASE, 'data', 'accounts');
  let reg = {};
  try { reg = JSON.parse(fs.readFileSync(path.join(accDir, 'registry.json'), 'utf8')).accounts || {}; } catch(e) {}
  /* 日志：按 auth_user 统计签名次数和最近一次 poolState */
  const sign = {};
  try {
    const re = /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}).*WAA 签名成功:.*?auth_user=(\d+).*?poolState=(\w+)/;
    for (const L of fs.readFileSync(LOG_FILES.main, 'utf8').split('\n')) {
      const m = L.match(re);
      if (!m) continue;
      const s = sign[m[2]] || (sign[m[2]] = { mints: 0 });
      s.mints++; s.state = m[3]; s.ts = Date.parse(m[1].replace(' ', 'T') + '+08:00') || 0;
    }
  } catch(e) {}
  const accounts = Object.values(reg).map(a => {
    let names = [];
    try {
      const c = JSON.parse(fs.readFileSync(path.join(accDir, a.id, 'auth.json'), 'utf8')).cookies || [];
      names = Array.isArray(c) ? c.map(x => x.name) : Object.keys(c);
    } catch(e) {}
    const s = sign[String(a.auth_user || '0')];
    const pool = s && s.ts && Date.now() - s.ts < 3600000
      ? { state: s.state === 'cold' ? 'hot' : s.state, mints: s.mints, expiresAt: s.ts + 3600000 } : null;
    return {
      accountId: a.id + (a.enabled === false ? ' (停用)' : ''),
      g1Tier: TIER_HDR[a.tier] || 'TIER0',
      authUser: Number(a.auth_user || 0),
      cookieCount: names.length,
      cookieNames: names,
      requestsCount: s ? s.mints : 0,
      topModels: [],
      pool,
    };
  });
  return { ok: true, accountCount: accounts.length, accounts,
           cookiesDir: accDir, cookieFiles: accounts.length };
}
function waaPage() {
  delete require.cache[require.resolve('./waa-ui/dashboard.js')];
  const { renderDashboardHtml } = require('./waa-ui/dashboard.js');
  let html = renderDashboardHtml({ apiKey: '（见 aistudio-api 配置）', defaultModel: 'gemini-3.8-flash' });
  const shim = '<style>.import-bar,#manual-cookie,button.danger,button[onclick^="testAcc"]{display:none!important}</style>'
    + '<script>(function(){var f=window.fetch;window.fetch=function(u,o){'
    + 'if(u==="/api/status")u="/api/waa-status";'
    + 'if(/^\\/api\\/(accounts|login|logout)/.test(u))return Promise.resolve(new Response(JSON.stringify({ok:false,error:"只读预览"}),{status:200,headers:{"Content-Type":"application/json"}}));'
    + 'return f(u,o)}})();</script>';
  html = html.replace('<script>', shim + '<script>');
  html = html.replace(/(<div class="brand-title">)WAA \/\/ RELAY GATEWAY/, '$1WAA // 无浏览器模式（只读预览）');
  html = html.replace(
    '<div class="brand-title">WAA // 无浏览器模式（只读预览）</div>',
    '<div class="brand-title">WAA // 无浏览器模式（只读预览）</div><a href="/waa/settings" style="display:inline-block;margin-top:10px;padding:8px 12px;border:1px solid #64748b;border-radius:6px;color:#e2e8f0;text-decoration:none">签名服务设置</a>'
  );
  return html;
}

function waaSettingsPage() {
  return `<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WAA 签名服务</title><style>
  *{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#18212f;font:14px system-ui,-apple-system,"Segoe UI",sans-serif}header{background:white;border-bottom:1px solid #dce2e9;padding:16px 20px;display:flex;align-items:center;justify-content:space-between}h1{font-size:18px;margin:0}main{max-width:1050px;margin:0 auto;padding:22px 18px}.bar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}button{border:1px solid #cbd3dc;background:#fff;color:#18212f;border-radius:5px;padding:8px 11px;cursor:pointer}button.primary{background:#2457c5;color:white;border-color:#2457c5}button.danger{color:#b42318}.list{border-top:1px solid #dce2e9}.row{padding:15px 2px;border-bottom:1px solid #dce2e9;display:flex;justify-content:space-between;gap:15px;align-items:center}.info{min-width:0}.name{font-weight:650}.url,.meta{margin-top:5px;color:#5d6978;font-size:12px;overflow-wrap:anywhere}.actions{display:flex;gap:7px;flex-wrap:wrap}.tag{font-size:11px;background:#eaf5ef;color:#18643d;padding:3px 7px;border-radius:4px;margin-left:8px}.modal{position:fixed;inset:0;background:#0007;display:grid;place-items:center;padding:18px}.box{background:#fff;border-radius:7px;padding:18px;width:min(620px,100%);max-height:92vh;overflow:auto}.fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.fields label{font-size:12px;color:#55606f}.fields input,.fields textarea{display:block;width:100%;padding:9px;margin-top:5px;border:1px solid #cbd3dc;border-radius:4px;font:13px system-ui}.wide{grid-column:1/-1}.footer{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}.hint{font-size:11px;color:#778293;margin-top:5px}#msg{padding:8px 0;color:#a33}a{color:#315dae}@media(max-width:650px){.row{align-items:flex-start;flex-direction:column}.fields{grid-template-columns:1fr}.wide{grid-column:auto}}
  </style></head><body><header><h1>WAA 签名服务</h1><a href="/waa">返回 WAA 状态面板</a></header><main><div class="bar"><div>选择签名节点并设置节点代理。更改立即写入 API 服务配置。</div><button class="primary" onclick="openForm()">添加服务</button></div><div id="msg"></div><div id="list" class="list">加载中…</div></main><div id="modal" class="modal" style="display:none" onclick="if(event.target===this)closeForm()"><form class="box" onsubmit="save(event)"><h2 id="formTitle" style="font-size:16px;margin:0 0 15px">添加签名服务</h2><div class="fields"><label>名称<input id="name" required></label><label>服务 URL<input id="url" required placeholder="https://signer.example"></label><label class="wide">节点代理<input id="proxy" placeholder="http://user:pass@host:port；留空继承全局，填 direct 表示直连"><div class="hint">本地 signer 会忽略代理；远程节点使用此代理访问 signer。</div></label><label class="wide">User-Agent<input id="ua"></label><label class="wide">自定义请求头 JSON<textarea id="headers" rows="4">{}</textarea></label><label class="wide">描述<input id="description"></label></div><div class="footer"><button type="button" onclick="closeForm()">取消</button><button class="primary" type="submit">保存</button></div></form></div><script>
  let data={servers:[],active_server:''}, editing=null;const api='/api/waa-config';const msg=(s)=>document.getElementById('msg').textContent=s||'';
  async function call(path,method='GET',body){const r=await fetch(api+path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,credentials:'same-origin'});const d=await r.json();if(!r.ok)throw Error(d.detail||d.error||('HTTP '+r.status));return d}
  async function load(){try{data=await call('/signer-servers');render();msg('')}catch(e){msg('加载失败：'+e.message)}}
  function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  function render(){const root=document.getElementById('list');root.innerHTML=data.servers.map(s=>'<article class="row"><div class="info"><div class="name">'+esc(s.name)+(s.id===data.active_server?'<span class="tag">正在使用</span>':'')+'</div><div class="url">'+esc(s.url)+'</div><div class="meta">节点代理：'+(s.proxy_url?'已配置':'继承全局')+(s.description?' · '+esc(s.description):'')+'</div></div><div class="actions"><button onclick="test(\''+esc(s.id)+'\')">测试</button>'+(s.id!==data.active_server?'<button onclick="switchTo(\''+esc(s.id)+'\')">切换</button>':'')+'<button onclick="edit(\''+esc(s.id)+'\')">编辑</button>'+(s.id!==data.active_server?'<button class="danger" onclick="removeServer(\''+esc(s.id)+'\')">删除</button>':'')+'</div></article>').join('')||'<div style="padding:18px 0;color:#718096">没有配置 signer。</div>'}
  function showForm(s={}){editing=s.id||null;document.getElementById('formTitle').textContent=editing?'编辑签名服务':'添加签名服务';for(const [id,key] of [['name','name'],['url','url'],['proxy','proxy_url'],['ua','user_agent'],['description','description']])document.getElementById(id).value=s[key]||'';document.getElementById('headers').value=JSON.stringify(s.headers||{},null,2);document.getElementById('modal').style.display='grid'}function closeForm(){document.getElementById('modal').style.display='none'}function edit(id){showForm(data.servers.find(s=>s.id===id)||{})}
  async function save(e){e.preventDefault();try{const headers=JSON.parse(document.getElementById('headers').value||'{}');if(!headers||Array.isArray(headers)||typeof headers!=='object')throw Error('请求头必须是 JSON 对象');const s=data.servers.find(x=>x.id===editing)||{};await call('/signer-servers','PUT',{id:editing||undefined,name:document.getElementById('name').value,url:document.getElementById('url').value,proxy_url:document.getElementById('proxy').value,user_agent:document.getElementById('ua').value,description:document.getElementById('description').value,headers});closeForm();await load()}catch(e){msg('保存失败：'+e.message)}}
  async function switchTo(id){try{await call('/signer-servers/switch','POST',{server_id:id});await load()}catch(e){msg('切换失败：'+e.message)}}async function removeServer(id){if(!confirm('删除此签名服务？'))return;try{await call('/signer-servers/'+encodeURIComponent(id),'DELETE');await load()}catch(e){msg('删除失败：'+e.message)}}async function test(id){const s=data.servers.find(x=>x.id===id);try{const r=await call('/signer-servers/test','POST',{url:s.url,proxy_url:s.proxy_url||''});msg(r.ok?'连接成功：'+r.elapsed_ms+' ms':'连接失败：'+(r.error||r.status_code))}catch(e){msg('测试失败：'+e.message)}}
  function openForm(){showForm({url:'https://waa.spinsnow.fun',headers:{'X-User-Agent':'grpc-web-javascript/0.1'}})}load();</script></body></html>`;
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

  /* WAA signer 管理与只读 pg-bridge 预览 */
  if (pn === '/waa/settings') {
    res.writeHead(200, {'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'no-store'});
    res.end(waaSettingsPage());
    return;
  }
  if (pn === '/api/waa-config' || pn.startsWith('/api/waa-config/')) {
    const apiBase = process.env.AISTUDIO_API_URL || 'http://127.0.0.1:8080';
    const suffix = pn.slice('/api/waa-config'.length);
    const target = new URL('/config/signer-servers' + suffix + (parsed.search || ''), apiBase);
    const headers = {};
    for (const name of ['cookie', 'authorization', 'content-type', 'content-length']) {
      if (req.headers[name]) headers[name] = req.headers[name];
    }
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      const proxyReq = http.request(target, { method: req.method, headers }, proxyRes => {
        res.writeHead(proxyRes.statusCode || 502, {
          'Content-Type': proxyRes.headers['content-type'] || 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        });
        proxyRes.pipe(res);
      });
      proxyReq.on('error', error => {
        if (!res.headersSent) res.writeHead(502, {'Content-Type':'application/json; charset=utf-8'});
        res.end(JSON.stringify({detail:'API proxy failed: '+error.message}));
      });
      if (chunks.length) proxyReq.write(Buffer.concat(chunks));
      proxyReq.end();
    });
    return;
  }
  if (pn === '/waa') {
    try { res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); res.end(waaPage()); }
    catch(e) { res.writeHead(500, {'Content-Type':'text/plain; charset=utf-8'}); res.end('面板加载失败: ' + e.message); }
    return;
  }
  if (pn === '/api/waa-status') {
    res.writeHead(200, {'Content-Type':'application/json; charset=utf-8'});
    res.end(JSON.stringify(waaStatus()));
    return;
  }

  /* 引擎 / 签名汇总 API */
  if (pn === '/api/engine') {
    res.writeHead(200, {'Content-Type':'application/json; charset=utf-8'});
    res.end(JSON.stringify(parseEngine()));
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
