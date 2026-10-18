const http = require('http');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');

const PROJECT_DIR = path.join(process.env.HOME, 'aistudio-api');
const CFG_PATH = path.join(PROJECT_DIR, 'config.yaml');
const PANEL_DIR = path.join(PROJECT_DIR, 'panel');
const PORT = 18081;

// 简易 YAML 解析 config.yaml (结构较简单)
function parseYaml(text) {
  const lines = text.split('\n');
  const root = {};
  const stack = [{ obj: root, indent: -2 }];
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const trimmed = raw.replace(/^\s+/, '');
    if (!trimmed || trimmed.startsWith('#')) continue;
    const indent = raw.length - trimmed.length;
    let line = trimmed;
    const cIdx = line.indexOf(' #');
    if (cIdx > 0) line = line.substring(0, cIdx).trimEnd();
    if (!line) continue;
    while (stack.length > 1 && stack[stack.length - 1].indent >= indent) stack.pop();
    const parent = stack[stack.length - 1].obj;
    if (line.startsWith('- ')) {
      const val = line.substring(2).trim();
      const top = stack[stack.length - 1];
      if (top.lastKey && !Array.isArray(parent[top.lastKey])) parent[top.lastKey] = [];
      const arr = top.lastKey ? parent[top.lastKey] : parent;
      if (Array.isArray(arr)) arr.push(parseVal(val));
      continue;
    }
    const ci = line.indexOf(':');
    if (ci < 0) continue;
    const key = line.substring(0, ci).trim();
    const valPart = line.substring(ci + 1).trim();
    if (valPart === '' || valPart === '|' || valPart === '>') {
      let childIsArray = false;
      for (let j = i + 1; j < lines.length; j++) {
        const pt = lines[j].replace(/^\s+/, '');
        if (!pt || pt.startsWith('#')) continue;
        if (pt.startsWith('- ')) childIsArray = true;
        break;
      }
      parent[key] = childIsArray ? [] : {};
      stack.push({ obj: parent[key], indent, lastKey: null });
    } else if (valPart.startsWith('[')) {
      const inner = valPart.slice(1, -1).trim();
      parent[key] = inner ? inner.split(',').map(x => parseVal(x.trim())) : [];
    } else {
      parent[key] = parseVal(valPart);
    }
    stack[stack.length - 1].lastKey = key;
  }
  return root;
}

function parseVal(s) {
  s = s.trim();
  if (s === '' || s === '~' || s === 'null') return null;
  if (s === 'true') return true;
  if (s === 'false') return false;
  if (/^-?\d+$/.test(s)) return parseInt(s);
  if (/^-?\d+\.\d+$/.test(s)) return parseFloat(s);
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) return s.slice(1, -1);
  return s;
}

// 写回 config.yaml - 只更新我们管理的顶层字段，保留其余原文
function writeConfig(obj) {
  const bak = CFG_PATH + '.bak.' + Date.now();
  if (fs.existsSync(CFG_PATH)) fs.copyFileSync(CFG_PATH, bak);
  // 读原文重写管理字段
  let text = fs.existsSync(CFG_PATH) ? fs.readFileSync(CFG_PATH, 'utf8') : '';
  // api_keys
  const keys = obj.api_keys || [];
  if (keys.length === 0) {
    text = text.replace(/^api_keys:.*(?:\n(?:[ \t]+-.*)*)?/m, 'api_keys: []');
  } else {
    const keyLines = 'api_keys:\n' + keys.map(k => `  - ${JSON.stringify(k)}`).join('\n');
    text = text.replace(/^api_keys:.*(?:\n(?:[ \t]+-.*)*)?/m, keyLines);
  }
  // logging fields
  const log = obj.logging || {};
  text = text.replace(/(level:)\s*\S+/, `$1 ${log.level || 'INFO'}`);
  text = text.replace(/(dump_requests:)\s*\S+/, `$1 ${!!log.dump_requests}`);
  text = text.replace(/(dump_to_file:)\s*\S+/, `$1 ${!!log.dump_to_file}`);
  text = text.replace(/(dump_dir:)\s*\S+/, `$1 ${log.dump_dir || 'dumps'}`);
  // model_defaults.drop_unsupported_params (top level)
  const md = obj.model_defaults || {};
  text = text.replace(/(drop_unsupported_params:)\s*\S+/g, (match, p1, offset) => {
    // Only replace the first (top-level) occurrence
    return `${p1} ${!!md.drop_unsupported_params}`;
  });
  fs.writeFileSync(CFG_PATH, text, 'utf8');
  // cleanup backups
  const baks = fs.readdirSync(PROJECT_DIR).filter(f => f.startsWith('config.yaml.bak.')).sort();
  while (baks.length > 5) fs.unlinkSync(path.join(PROJECT_DIR, baks.shift()));
}

function sendJSON(res, data, code = 200) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(data));
}

function readBody(req) {
  return new Promise(r => { let b = ''; req.on('data', c => b += c); req.on('end', () => r(b)); });
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript', '.css': 'text/css' };

http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type' });
    return res.end();
  }
  const url = req.url.split('?')[0];

  if (url === '/api/config' && req.method === 'GET') {
    try { return sendJSON(res, parseYaml(fs.readFileSync(CFG_PATH, 'utf8'))); }
    catch (e) { return sendJSON(res, { error: e.message }, 500); }
  }
  if (url === '/api/config' && req.method === 'POST') {
    try { writeConfig(JSON.parse(await readBody(req))); return sendJSON(res, { ok: true }); }
    catch (e) { return sendJSON(res, { error: e.message }, 500); }
  }
  if (url === '/api/action' && req.method === 'POST') {
    try {
      const { action } = JSON.parse(await readBody(req));
      const script = path.join(process.env.HOME, 'aistudio.sh');
      if (action === 'restart') exec(`bash ${script} stop && sleep 1 && bash ${script} bg`);
      else if (action === 'stop') exec(`bash ${script} stop`);
      return sendJSON(res, { message: action === 'stop' ? '服务停止中...' : '服务重启中...' });
    } catch (e) { return sendJSON(res, { error: e.message }, 500); }
  }

  let fp = url === '/' ? '/index.html' : url;
  fp = path.join(PANEL_DIR, fp);
  if (!fp.startsWith(PANEL_DIR)) { res.writeHead(403); return res.end(); }
  try {
    const data = fs.readFileSync(fp);
    res.writeHead(200, { 'Content-Type': MIME[path.extname(fp)] || 'application/octet-stream' });
    res.end(data);
  } catch {
    try { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(fs.readFileSync(path.join(PANEL_DIR, 'index.html'))); }
    catch { res.writeHead(404); res.end('Not Found'); }
  }
}).listen(PORT, '0.0.0.0', () => console.log(`[aistudio-api Panel] http://0.0.0.0:${PORT}`));
