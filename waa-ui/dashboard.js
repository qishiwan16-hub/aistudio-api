'use strict';

/**
 * Account Store & Local Web Dashboard for Relay Client Gateway
 *
 * Persistence:
 *   - When a storage directory is configured, every imported account is written
 *     as one JSON credential file (`cookies/acc_<hash>.json`, mode 0600) and
 *     automatically reloaded on startup.
 *   - Without a storage directory the store stays strictly in RAM.
 *
 * Security:
 *   - Dashboard API outputs only whitelisted, anonymized fields (`accountId`,
 *     `authUser`, `g1Tier`, `cookieCount`, `cookieNames`, `status`, `addedAt`,
 *     `requestsCount`), never exposing raw Cookie or Authorization strings.
 */

const crypto = require('node:crypto');
const { CookieStore } = require('./cookie-store');

const MINIMAL_COOKIE_WHITELIST = new Set([
  'SID',
  'HSID',
  'SSID',
  'APISID',
  'SAPISID',
  '__Secure-1PSID',
  '__Secure-3PSID',
  '__Secure-1PAPISID',
  '__Secure-3PAPISID',
  '__Secure-1PSIDTS',
  '__Secure-3PSIDTS',
  'SIDCC',
  '__Secure-1PSIDCC',
  '__Secure-3PSIDCC',
  'NID',
  'LSID',
  '__Host-1PLSID',
  '__Host-3PLSID',
  '__Host-GAPS',
  'ACCOUNT_CHOOSER',
]);

function parseCookieString(rawInput) {
  const map = new Map();
  if (!rawInput) return map;
  if (typeof rawInput === 'object' && !(rawInput instanceof Map)) {
    for (const [k, v] of Object.entries(rawInput)) {
      if (k && v) map.set(String(k).trim(), String(v).trim());
    }
    return map;
  }
  const str = String(rawInput)
    .replace(/^\s*cookie\s*:\s*/i, '')
    .trim();
  for (const part of str.split(/;\s*/)) {
    if (!part) continue;
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    const k = part.slice(0, eq).trim();
    const v = part.slice(eq + 1).trim();
    if (k && v) map.set(k, v);
  }
  return map;
}

function extractMinimalCookies(rawInput) {
  const fullMap = parseCookieString(rawInput);
  const minMap = new Map();
  for (const [k, v] of fullMap.entries()) {
    if (MINIMAL_COOKIE_WHITELIST.has(k) && v) {
      minMap.set(k, v);
    }
  }
  const header = Array.from(minMap.entries())
    .map(([k, v]) => `${k}=${v}`)
    .join('; ');
  return { map: minMap, header };
}

function currentQuotaBucket(now = Date.now()) {
  // Daily rollover at 15:00 UTC+8 (= 07:00 UTC)
  return new Date(now - 7 * 3600 * 1000).toISOString().slice(0, 10);
}

function computeAccountId(cookieMap, authUser = 0) {
  const anchor =
    cookieMap.get('SAPISID') ||
    cookieMap.get('__Secure-3PAPISID') ||
    cookieMap.get('__Secure-1PAPISID') ||
    cookieMap.get('SID') ||
    cookieMap.get('__Secure-3PSID') ||
    Array.from(cookieMap.values()).join(';');
  const digest = crypto
    .createHash('sha256')
    .update(`${anchor}|u=${Number(authUser || 0)}`, 'utf8')
    .digest('hex')
    .slice(0, 16);
  return `acc_${digest}`;
}

function resolveModelAlias(modelInput, fallback = 'models/gemini-3.8-flash') {
  const raw = String(modelInput || '')
    .trim()
    .replace(/-(high|medium|low|minimal)$/i, '');
  if (!raw) return fallback;
  const lower = raw.toLowerCase();
  if (
    lower === 'gemini-3.8-flash' ||
    lower === 'models/gemini-3.8-flash'
  ) {
    return 'models/gemini-3.8-flash';
  }
  if (
    lower === 'argon' ||
    lower === '4-argon' ||
    lower === 'gemini-4-argon' ||
    lower === 'models/gemini-4-argon'
  ) {
    return 'models/gemini-4-argon';
  }
  return raw.startsWith('models/') ? raw : `models/${raw}`;
}

class RelayAccountStore {
  /**
   * @param {object} [options]
   * @param {string} [options.storageDir] Directory for per-account JSON credentials.
   *        When set, the directory is auto-created and all `*.json` files are
   *        loaded on startup; every add/remove is persisted back to disk.
   */
  constructor(options = {}) {
    this.accounts = new Map();
    this.rrIndex = 0;
    this.storageDir = null;
    this.cookieStore = null;
    this.loadedFiles = 0;
    if (options.storageDir) {
      this.cookieStore = new CookieStore(options.storageDir);
      this.storageDir = this.cookieStore.dir;
      this.loadedFiles = this.loadFromDisk();
    }
  }

  /** Restore every persisted credential file into the in-memory store. */
  loadFromDisk() {
    if (!this.cookieStore) return 0;
    const files = this.cookieStore.list();
    let loaded = 0;
    this._loading = true;
    try {
      for (const { file, data } of files) {
        try {
          const pub = this.addAccount(
            {
              cookies: data.cookies,
              authUser: data.authUser ?? 0,
              g1Tier: data.g1Tier || null,
            },
            data.authUser ?? 0,
            data.g1Tier || null,
            data.source || 'disk',
          );
          const restored = this.accounts.get(pub.accountId);
          if (restored) {
            const curBucket = currentQuotaBucket();
            if (data.dailyBucket === curBucket) {
              restored.dailyBucket = curBucket;
              restored.requestsCount = Number(data.requestsCount || 0);
              restored.dailyModelCounts =
                data.dailyModelCounts && typeof data.dailyModelCounts === 'object'
                  ? { ...data.dailyModelCounts }
                  : {};
            } else {
              restored.dailyBucket = curBucket;
              restored.requestsCount = 0;
              restored.dailyModelCounts = {};
            }
          }
          loaded += 1;
        } catch (err) {
          console.warn(`[relay] 跳过无效 Cookie 文件 ${file}: ${err.message}`);
        }
      }
    } finally {
      this._loading = false;
    }
    return loaded;
  }

  /** Persist a single account entry (no-op when persistence is disabled). */
  persist(entry) {
    if (!this.cookieStore || this._loading) return;
    try {
      this.cookieStore.save(entry);
    } catch (err) {
      console.warn(`[relay] Cookie 持久化失败 (${entry.accountId}): ${err.message}`);
    }
  }

  /**
   * Add or update an account in the Relay Client's in-memory store.
   * Accepts:
   *   - Tampermonkey JSON export (`{ cookies, authUser, g1Tier }`)
   *   - Raw Cookie string (`SID=...; SAPISID=...`)
   *   - Full cURL command snippet
   */
  addAccount(rawInput, authUser = 0, g1Tier = null, source = 'manual') {
    let text = '';
    let jsonAuthUser = null;
    let jsonTier = null;

    if (rawInput && typeof rawInput === 'object' && typeof rawInput.cookies === 'string') {
      text = rawInput.cookies.trim();
      if (rawInput.authUser !== undefined && rawInput.authUser !== null) {
        jsonAuthUser = Number(rawInput.authUser);
      }
      if (rawInput.g1Tier) jsonTier = String(rawInput.g1Tier).trim().toUpperCase();
    } else {
      text = String(rawInput || '').trim();
    }

    // Unwrap nested JSON string (e.g., when Dashboard submits `{ cookies: "<Tampermonkey JSON>", authUser: 0 }`)
    if (text.startsWith('{')) {
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed.cookies === 'string') {
          text = parsed.cookies.trim();
          if (parsed.authUser !== undefined && parsed.authUser !== null) {
            jsonAuthUser = Number(parsed.authUser);
          }
          if (parsed.g1Tier) {
            jsonTier = String(parsed.g1Tier).trim().toUpperCase();
          }
        }
      } catch {}
    }

    const rawTextForHeaders =
      typeof rawInput === 'string'
        ? rawInput
        : rawInput && typeof rawInput.cookies === 'string'
          ? rawInput.cookies
          : text;

    const curlCookieMatch =
      text.match(/(?:-H|--header)\s+['"]cookie:\s*([^'"]+)['"]/i) ||
      text.match(/(?:-b|--cookie)\s+['"]([^'"]+)['"]/i);
    if (curlCookieMatch) {
      text = curlCookieMatch[1];
    }

    const authUserMatch = String(rawTextForHeaders || '').match(
      /(?:-H|--header)\s+['"]x-goog-authuser:\s*(\d+)['"]/i,
    );
    const tierHeaderMatch = String(rawTextForHeaders || '').match(
      /(?:-H|--header)\s+['"]x-aistudio-g1-tier:\s*(TIER\d+)['"]/i,
    );

    const resolvedAuthUser =
      jsonAuthUser !== null && !Number.isNaN(jsonAuthUser)
        ? jsonAuthUser
        : authUserMatch
          ? Number(authUserMatch[1])
          : Number(authUser ?? 0);

    const min = extractMinimalCookies(text);
    if (
      min.map.size === 0 ||
      (!min.map.has('SAPISID') && !min.map.has('__Secure-3PAPISID') && !min.map.has('SID'))
    ) {
      throw new Error('未检测到有效的 Google 核心鉴权 Cookie (需包含 SID / SAPISID / __Secure-1PSID)');
    }
    if (
      !min.map.has('__Secure-1PSID') &&
      !min.map.has('__Secure-3PSID') &&
      !min.map.has('HSID')
    ) {
      throw new Error(
        `仅检测到 ${min.map.size} 项非 HttpOnly Cookie，缺少 __Secure-1PSID / HSID / SSID（请在油猴设置开启 Cookie All 权限后重新抓取，切勿使用 document.cookie）`,
      );
    }

    const accountId = computeAccountId(min.map, resolvedAuthUser);
    const now = Date.now();
    const existing = this.accounts.get(accountId);
    const resolvedTier =
      jsonTier ||
      (tierHeaderMatch ? tierHeaderMatch[1].toUpperCase() : null) ||
      (g1Tier ? String(g1Tier).trim().toUpperCase() : null) ||
      (existing && existing.g1Tier) ||
      'TIER0';
    const curBucket = currentQuotaBucket(now);
    const sameBucket = existing && existing.dailyBucket === curBucket;
    const entry = {
      accountId,
      authUser: resolvedAuthUser,
      g1Tier: resolvedTier,
      cookies: min.header,
      cookieCount: min.map.size,
      cookieNames: Array.from(min.map.keys()),
      source: existing ? existing.source : source,
      status: 'ready',
      addedAt: existing ? existing.addedAt : now,
      updatedAt: now,
      lastUsedAt: existing ? existing.lastUsedAt : 0,
      dailyBucket: curBucket,
      requestsCount: sameBucket ? existing.requestsCount || 0 : 0,
      dailyModelCounts: sameBucket && existing.dailyModelCounts ? { ...existing.dailyModelCounts } : {},
      lastError: null,
    };
    this.accounts.set(accountId, entry);
    this.persist(entry);
    return this.toPublicView(entry);
  }

  removeAccount(accountId) {
    const removed = this.accounts.delete(accountId);
    if (removed && this.cookieStore) {
      try {
        this.cookieStore.remove(accountId);
      } catch (err) {
        console.warn(`[relay] 删除 Cookie 文件失败 (${accountId}): ${err.message}`);
      }
    }
    return removed;
  }

  /**
   * Replace an account's cookies in place.
   * Used after a local `RotateCookies` refresh or a `Set-Cookie` update, so the
   * next request starts from the refreshed set instead of the stale one.
   */
  updateCookies(rawInput, authUser = 0, newCookies = '') {
    const oldMin = extractMinimalCookies(rawInput);
    if (oldMin.map.size === 0) return null;
    const accountId = computeAccountId(oldMin.map, Number(authUser || 0));
    const entry = this.accounts.get(accountId);
    if (!entry) return null;
    const min = extractMinimalCookies(newCookies);
    if (min.map.size === 0) return null;
    entry.cookies = min.header;
    entry.cookieCount = min.map.size;
    entry.cookieNames = Array.from(min.map.keys());
    entry.updatedAt = Date.now();
    this.persist(entry);
    return this.toPublicView(entry);
  }

  countReady() {
    let count = 0;
    for (const account of this.accounts.values()) {
      if (account.status === 'ready') count += 1;
    }
    return count;
  }

  pickAccount() {
    return this.pickAccountExcluding();
  }

  /**
   * Round-robin pick among ready accounts, skipping any id already tried by
   * the current request (used for automatic failover).
   */
  pickAccountExcluding(excludedIds = null) {
    const list = Array.from(this.accounts.values()).filter((a) => a.status === 'ready');
    if (list.length === 0) return null;
    const excluded = excludedIds instanceof Set ? excludedIds : new Set(excludedIds || []);
    for (let i = 0; i < list.length; i += 1) {
      const idx = (this.rrIndex + i) % list.length;
      const candidate = list[idx];
      if (excluded.has(candidate.accountId)) continue;
      this.rrIndex = (idx + 1) % list.length;
      candidate.lastUsedAt = Date.now();
      return candidate;
    }
    return null;
  }

  _ensureDailyBucket(entry) {
    const curBucket = currentQuotaBucket();
    if (entry.dailyBucket !== curBucket) {
      entry.dailyBucket = curBucket;
      entry.requestsCount = 0;
      entry.dailyModelCounts = {};
      this.persist(entry);
    } else if (!entry.dailyModelCounts || typeof entry.dailyModelCounts !== 'object') {
      entry.dailyModelCounts = {};
    }
  }

  recordModelUsage(accountId, model) {
    const entry = this.accounts.get(accountId);
    if (!entry) return;
    this._ensureDailyBucket(entry);
    const cleanModel = String(model || '')
      .trim()
      .replace(/^models\//i, '')
      .replace(/-(high|medium|low|minimal)$/i, '');
    entry.lastUsedAt = Date.now();
    entry.requestsCount = (entry.requestsCount || 0) + 1;
    if (cleanModel) {
      entry.dailyModelCounts[cleanModel] = (entry.dailyModelCounts[cleanModel] || 0) + 1;
    }
    this.persist(entry);
  }

  toPublicView(entry) {
    this._ensureDailyBucket(entry);
    const topModels = Object.entries(entry.dailyModelCounts || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([model, count]) => ({ model, count }));
    return {
      accountId: entry.accountId,
      authUser: entry.authUser,
      g1Tier: entry.g1Tier || 'TIER0',
      cookieCount: entry.cookieCount,
      cookieNames: entry.cookieNames,
      source: entry.source,
      status: entry.status,
      addedAt: entry.addedAt,
      updatedAt: entry.updatedAt,
      lastUsedAt: entry.lastUsedAt,
      dailyBucket: entry.dailyBucket,
      requestsCount: entry.requestsCount || 0,
      topModels,
      lastError: entry.lastError,
    };
  }

  listPublic() {
    return Array.from(this.accounts.values()).map((e) => this.toPublicView(e));
  }
}

function renderLoginHtml() {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>登录 // WAA RELAY GATEWAY</title>
<style>
  :root {
    --bg: #fafafa;
    --surface: #ffffff;
    --surface-sub: #f4f4f5;
    --fg: #09090b;
    --muted: #71717a;
    --border: #e4e4e7;
    --border-strong: #18181b;
    --grid-line: rgba(9, 9, 11, 0.04);
    --danger: #dc2626;
    --danger-bg: rgba(220, 38, 38, 0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #09090b;
      --surface: #111113;
      --surface-sub: #18181b;
      --fg: #f4f4f5;
      --muted: #a1a1aa;
      --border: #27272a;
      --border-strong: #e4e4e7;
      --grid-line: rgba(244, 244, 245, 0.035);
      --danger: #f87171;
      --danger-bg: rgba(248, 113, 113, 0.08);
    }
  }
  :root[data-theme="dark"] {
    --bg: #09090b;
    --surface: #111113;
    --surface-sub: #18181b;
    --fg: #f4f4f5;
    --muted: #a1a1aa;
    --border: #27272a;
    --border-strong: #e4e4e7;
    --grid-line: rgba(244, 244, 245, 0.035);
    --danger: #f87171;
    --danger-bg: rgba(248, 113, 113, 0.08);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background-color: var(--bg);
    background-image:
      linear-gradient(to right, var(--grid-line) 1px, transparent 1px),
      linear-gradient(to bottom, var(--grid-line) 1px, transparent 1px);
    background-size: 28px 28px;
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .login-card {
    position: relative;
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 36px 32px;
    max-width: 440px;
    width: 100%;
  }
  .login-card::before, .login-card::after {
    content: "";
    position: absolute;
    width: 10px;
    height: 10px;
    pointer-events: none;
  }
  .login-card::before { top: -1px; left: -1px; border-top: 2px solid var(--border-strong); border-left: 2px solid var(--border-strong); }
  .login-card::after { bottom: -1px; right: -1px; border-bottom: 2px solid var(--border-strong); border-right: 2px solid var(--border-strong); }
  .top-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
  .poly-line { width: 88px; height: 24px; stroke: var(--fg); fill: none; stroke-width: 1.5; }
  .theme-toggle {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 4px 10px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    cursor: pointer;
  }
  h1 { margin: 0 0 8px 0; font-size: 20px; font-weight: 700; letter-spacing: -0.03em; font-family: ui-monospace, monospace; }
  p { color: var(--muted); font-size: 13px; line-height: 1.6; margin: 0 0 22px 0; }
  input[type="password"] {
    width: 100%;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 11px 12px;
    font-size: 13px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    outline: none;
    margin-bottom: 12px;
  }
  input[type="password"]:focus { border-color: var(--border-strong); }
  .login-btn {
    width: 100%;
    padding: 11px 18px;
    background: var(--fg);
    color: var(--bg);
    border: 1px solid var(--border-strong);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
    transition: opacity 0.12s;
  }
  .login-btn:hover { opacity: 0.86; }
  .err-msg {
    display: none;
    margin-bottom: 12px;
    padding: 8px 10px;
    font-size: 12px;
    font-family: ui-monospace, monospace;
    color: var(--danger);
    border: 1px solid var(--danger);
    background: var(--danger-bg);
  }
</style>
</head>
<body>
  <div class="login-card">
    <div class="top-row">
      <svg class="poly-line" viewBox="0 0 88 24">
        <polyline points="2,18 18,18 26,5 38,20 50,9 62,15 72,4 86,4"/>
      </svg>
      <button class="theme-toggle" onclick="toggleTheme()">◐ 主题</button>
    </div>
    <h1>WAA // RELAY GATEWAY</h1>
    <p>请输入网关 API Key（登录密码）以访问 Cookie 凭证管理面板。<br>下游 OpenAI / Gemini 接口调用不受影响。</p>
    <div id="login-err" class="err-msg"></div>
    <form onsubmit="submitLogin(event)">
      <input type="password" id="login-key" placeholder="输入 RELAY_API_KEY..." autocomplete="current-password" autofocus>
      <button type="submit" class="login-btn" id="login-btn">验证并进入面板 →</button>
    </form>
  </div>
<script>
(function initTheme() {
  const saved = localStorage.getItem('aispg_theme');
  if (saved === 'light' || saved === 'dark') {
    document.documentElement.setAttribute('data-theme', saved);
  }
})();
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  const isDark = cur ? cur === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  const next = isDark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('aispg_theme', next);
}
async function submitLogin(e) {
  e.preventDefault();
  const key = document.getElementById('login-key').value.trim();
  const errEl = document.getElementById('login-err');
  const btn = document.getElementById('login-btn');
  if (!key) {
    errEl.style.display = 'block';
    errEl.textContent = '请输入 API Key';
    return;
  }
  errEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = '验证中...';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apiKey: key })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      errEl.style.display = 'block';
      errEl.textContent = data.error || 'API Key 错误';
      btn.disabled = false;
      btn.textContent = '验证并进入面板 →';
      return;
    }
    window.location.reload();
  } catch (err) {
    errEl.style.display = 'block';
    errEl.textContent = '网络请求失败: ' + err.message;
    btn.disabled = false;
    btn.textContent = '验证并进入面板 →';
  }
}
</script>
</body>
</html>`;
}

function renderDashboardHtml({ apiKey, defaultModel = 'gemini-3.8-flash' }) {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>WAA RELAY // 本地中转网关</title>
<style>
  :root {
    --bg: #fafafa;
    --surface: #ffffff;
    --surface-sub: #f4f4f5;
    --fg: #09090b;
    --muted: #71717a;
    --border: #e4e4e7;
    --border-strong: #18181b;
    --grid-line: rgba(9, 9, 11, 0.04);
    --danger: #dc2626;
    --danger-bg: rgba(220, 38, 38, 0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #09090b;
      --surface: #111113;
      --surface-sub: #18181b;
      --fg: #f4f4f5;
      --muted: #a1a1aa;
      --border: #27272a;
      --border-strong: #e4e4e7;
      --grid-line: rgba(244, 244, 245, 0.035);
      --danger: #f87171;
      --danger-bg: rgba(248, 113, 113, 0.08);
    }
  }
  :root[data-theme="dark"] {
    --bg: #09090b;
    --surface: #111113;
    --surface-sub: #18181b;
    --fg: #f4f4f5;
    --muted: #a1a1aa;
    --border: #27272a;
    --border-strong: #e4e4e7;
    --grid-line: rgba(244, 244, 245, 0.035);
    --danger: #f87171;
    --danger-bg: rgba(248, 113, 113, 0.08);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 28px 16px 48px;
    background-color: var(--bg);
    background-image:
      linear-gradient(to right, var(--grid-line) 1px, transparent 1px),
      linear-gradient(to bottom, var(--grid-line) 1px, transparent 1px);
    background-size: 28px 28px;
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .container { max-width: 1020px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
  .card {
    position: relative;
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 22px;
  }
  .card::before, .card::after {
    content: "";
    position: absolute;
    width: 8px;
    height: 8px;
    border-color: var(--border-strong);
    pointer-events: none;
  }
  .card::before { top: -1px; left: -1px; border-top: 2px solid var(--border-strong); border-left: 2px solid var(--border-strong); }
  .card::after { bottom: -1px; right: -1px; border-bottom: 2px solid var(--border-strong); border-right: 2px solid var(--border-strong); }
  h1, h2 { margin: 0; font-weight: 600; letter-spacing: -0.02em; }
  .top-bar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding-bottom: 16px; border-bottom: 1px solid var(--border); margin-bottom: 18px; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand-title { font-size: 18px; font-weight: 700; letter-spacing: -0.03em; display: flex; align-items: center; gap: 10px; }
  .poly-icon { width: 72px; height: 20px; stroke: var(--fg); fill: none; stroke-width: 1.5; stroke-linecap: square; stroke-linejoin: miter; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--fg); border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    font-weight: 500;
    border: 1px solid var(--border);
    background: var(--surface-sub);
    color: var(--fg);
  }
  .pill.green { border-color: var(--border-strong); font-weight: 600; }
  .pill.amber { border-style: dashed; color: var(--muted); }
  .pill.gray { color: var(--muted); }
  .pill.err { border-color: var(--danger); color: var(--danger); background: var(--danger-bg); }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  .stat-box {
    position: relative;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    padding: 14px 16px;
    overflow: hidden;
  }
  .stat-poly { position: absolute; right: 10px; bottom: 8px; width: 64px; height: 22px; stroke: var(--muted); opacity: 0.35; fill: none; stroke-width: 1.25; }
  .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-family: ui-monospace, monospace; }
  .stat-val { font-size: 22px; font-weight: 700; margin-top: 6px; font-family: ui-monospace, monospace; letter-spacing: -0.03em; }
  .stat-sub { font-size: 12px; color: var(--muted); margin-top: 4px; word-break: break-all; }
  .kv-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .kv-label { width: 140px; font-size: 12px; font-family: ui-monospace, monospace; text-transform: uppercase; color: var(--muted); flex-shrink: 0; }
  code.box {
    background: var(--surface-sub);
    border: 1px solid var(--border);
    padding: 8px 12px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px;
    color: var(--fg);
    word-break: break-all;
    flex: 1;
    min-width: 180px;
  }
  button, a.btn {
    padding: 7px 14px;
    border: 1px solid var(--border-strong);
    background: var(--fg);
    color: var(--bg);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-weight: 600;
    font-size: 12px;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    transition: all 0.12s ease;
  }
  button:hover, a.btn:hover { opacity: 0.85; }
  button.secondary, a.btn.secondary {
    background: transparent;
    color: var(--fg);
    border: 1px solid var(--border);
  }
  button.secondary:hover, a.btn.secondary:hover {
    border-color: var(--border-strong);
    background: var(--surface-sub);
  }
  button.danger {
    background: transparent;
    color: var(--danger);
    border: 1px solid var(--border);
    padding: 4px 10px;
    font-size: 11.5px;
  }
  button.danger:hover {
    border-color: var(--danger);
    background: var(--danger-bg);
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 11px 12px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; background: var(--surface-sub); }
  td { font-family: ui-monospace, monospace; font-size: 12.5px; }
  textarea, input[type="number"] {
    width: 100%;
    background: var(--surface-sub);
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 10px 12px;
    font-size: 12.5px;
    font-family: ui-monospace, monospace;
    outline: none;
  }
  textarea:focus, input[type="number"]:focus { border-color: var(--border-strong); }
  .import-bar { display: flex; gap: 10px; margin-top: 10px; align-items: center; flex-wrap: wrap; }
  pre {
    background: var(--surface-sub);
    border: 1px solid var(--border);
    padding: 14px;
    overflow-x: auto;
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: var(--fg);
    margin: 12px 0 0 0;
  }
  @media (max-width: 720px) {
    body { padding: 14px 10px 36px; }
    .card { padding: 16px 14px; }
    .grid { grid-template-columns: 1fr; gap: 10px; }
    .kv-row { flex-direction: column; align-items: stretch; gap: 6px; }
    .kv-label { width: auto; }
    code.box { width: 100%; min-width: 0; }
    .import-bar { flex-direction: column; align-items: stretch; }
    .import-bar input[type="number"] { width: 100% !important; }
    .import-bar button { width: 100%; padding: 10px; }
    table, thead, tbody, th, td, tr { display: block; }
    thead tr { display: none; }
    tbody tr {
      border: 1px solid var(--border);
      background: var(--surface-sub);
      margin-bottom: 10px;
      padding: 10px 12px;
    }
    td {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0;
      border-bottom: 1px dashed var(--border);
      text-align: right;
      gap: 8px;
      flex-wrap: wrap;
    }
    td:last-child { border-bottom: none; padding-top: 10px; justify-content: flex-end; }
    td::before {
      content: attr(data-label);
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      text-align: left;
    }
  }
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <div class="top-bar">
      <div class="brand">
        <svg class="poly-icon" viewBox="0 0 72 20"><polyline points="2,16 14,16 22,4 32,17 42,8 52,13 60,3 70,3"/></svg>
        <div class="brand-title">WAA // RELAY GATEWAY</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span id="overall-badge" class="pill amber">检测中...</span>
        <button class="secondary" onclick="toggleTheme()" id="theme-btn" style="padding:4px 10px;">◐ 主题</button>
        <a href="/logout" class="btn secondary" style="padding:4px 10px;">退出</a>
      </div>
    </div>
    <div class="grid">
      <div class="stat-box">
        <div class="stat-label">ACCOUNTS // 已加载账号</div>
        <div class="stat-val" id="stat-accounts">0</div>
        <svg class="stat-poly" viewBox="0 0 64 22"><polyline points="2,18 18,14 30,17 44,6 62,4"/></svg>
      </div>
      <div class="stat-box">
        <div class="stat-label" id="stat-pool-label">POOL // 当前账号池状态</div>
        <div class="stat-val" id="stat-pool">—</div>
        <div class="stat-sub" id="stat-pool-sub">未连接签名服务</div>
        <svg class="stat-poly" viewBox="0 0 64 22"><polyline points="2,16 16,8 28,14 46,4 62,10"/></svg>
      </div>
      <div class="stat-box">
        <div class="stat-label">STORAGE // Cookie 持久化</div>
        <div class="stat-val" id="stat-persist" style="font-size:15px;">—</div>
        <div class="stat-sub" id="stat-persist-sub"></div>
        <svg class="stat-poly" viewBox="0 0 64 22"><polyline points="2,12 20,12 28,5 38,18 48,12 62,12"/></svg>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>
      <span>01 // 导入 Cookie 凭证</span>
      <button class="secondary" onclick="refreshStatus()">↻ 刷新状态</button>
    </h2>
    <div style="margin-bottom:16px;">
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px;">粘贴油猴脚本导出的 JSON、原始 Cookie 字符串或 cURL 命令：</div>
      <textarea id="manual-cookie" rows="3" placeholder='{"ok":true,"authUser":0,"g1Tier":"TIER1","cookies":"..."} 或 SID=...; SAPISID=...'></textarea>
      <div class="import-bar">
        <button onclick="submitManualCookie()">+ 导入并保存</button>
      </div>
    </div>

    <div>
      <table>
        <thead>
          <tr>
            <th>账号 ID</th>
            <th>等级 (Tier)</th>
            <th>AuthUser</th>
            <th>Cookie 数</th>
            <th>池状态</th>
            <th>当日调用 (UTC+8 15:00 刷新)</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="accounts-tbody">
          <tr><td colspan="7" style="text-align:center;color:var(--muted);justify-content:center;">暂无账号，请先导入 Cookie</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>02 // OpenAI & Gemini 兼容接口</h2>
    <div class="kv-row">
      <span class="kv-label">OpenAI Base:</span>
      <code class="box" id="oai-base-url"></code>
    </div>
    <div class="kv-row">
      <span class="kv-label">API Key:</span>
      <code class="box" id="oai-api-key">${apiKey}</code>
    </div>
    <div class="kv-row">
      <span class="kv-label">Models:</span>
      <code class="box">gemini-4-argon(-high) / gemini-3.8-flash(-high) / gemini-3.5-flash-lite(-high) / gemini-nano-banana-2.1 / gemini-3.1-flash-image / gemini-3.1-flash-lite-image / gemini-3-pro-image</code>
    </div>
    <pre id="curl-example"></pre>
  </div>
</div>

<script>
(function initTheme() {
  const saved = localStorage.getItem('aispg_theme');
  if (saved === 'light' || saved === 'dark') {
    document.documentElement.setAttribute('data-theme', saved);
  }
})();
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  const isDark = cur ? cur === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  const next = isDark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('aispg_theme', next);
}

const API_KEY = ${JSON.stringify(apiKey)};
const DEFAULT_MODEL = ${JSON.stringify(defaultModel)};
const originUrl = window.location.origin;
document.getElementById('oai-base-url').textContent = originUrl + '/v1';
document.getElementById('curl-example').textContent =
\`curl -N \${originUrl}/v1/chat/completions \\\\
  -H "Content-Type: application/json" \\\\
  -H "Authorization: Bearer ***" \\\\
  -d '{
    "model": "\${DEFAULT_MODEL}",
    "stream": true,
    "messages": [{"role": "user", "content": "你好，请介绍一下自己"}]
  }'\`;

function fmtRemain(ts) {
  if (!ts) return '';
  const ms = ts - Date.now();
  if (ms <= 0) return '已过期';
  const m = Math.floor(ms / 60000);
  if (m < 60) return m + ' 分钟后到期';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' 小时后到期';
  return Math.floor(h / 24) + ' 天后到期';
}

function poolBadgeHtml(pool) {
  if (!pool) return '<span class="pill gray">COLD</span>';
  const cls = pool.state === 'hot' ? 'green' : 'amber';
  return '<span class="pill ' + cls + '" title="' + fmtRemain(pool.expiresAt) + '">' + String(pool.state).toUpperCase() + '</span>';
}

const testResults = {};

function renderTestBadge(accountId) {
  const r = testResults[accountId];
  if (!r) return '';
  if (r.loading) return '<span class="pill amber" style="margin-right:6px;">TESTING...</span>';
  if (r.alive) return '<span class="pill green" style="margin-right:6px;">OK · ' + r.elapsedMs + 'ms</span>';
  const shortErr = (r.error || '未知错误').slice(0, 42).replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return '<span class="pill err" style="margin-right:6px;" title="' + (r.error || '').replace(/"/g, '&quot;') + '">ERR · ' + shortErr + '</span>';
}

function renderUsageHtml(a) {
  const total = a.requestsCount || 0;
  const tops = Array.isArray(a.topModels) ? a.topModels : [];
  if (tops.length === 0) return '<span>' + total + '</span>';
  const tags = tops
    .map(t => '<span class="pill gray" style="padding:1px 6px;font-size:11px;">' + t.model + ' ×' + t.count + '</span>')
    .join(' ');
  return '<div style="display:flex;flex-direction:column;gap:4px;align-items:flex-start;"><strong>' + total + ' 次</strong><div style="display:flex;gap:4px;flex-wrap:wrap;">' + tags + '</div></div>';
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/status');
    if (res.status === 401) return window.location.reload();
    const data = await res.json();
    const accs = data.accounts || [];
    document.getElementById('stat-accounts').textContent = accs.length;

    const primary = accs[0];
    const pool = primary && primary.pool;
    document.getElementById('stat-pool-label').textContent =
      primary ? 'POOL // ' + primary.accountId : 'POOL // 当前账号池状态';
    document.getElementById('stat-pool').textContent = primary ? (pool ? String(pool.state).toUpperCase() : 'IDLE') : '—';
    document.getElementById('stat-pool-sub').textContent = pool
      ? '累计签名 ' + pool.mints + ' 次 · ' + fmtRemain(pool.expiresAt)
      : (primary ? '尚未在该签名服务中初始化' : '先导入一个 Cookie 账号');

    document.getElementById('stat-persist').textContent = data.cookiesDir
      ? 'DISK · ' + (data.cookieFiles || 0) + ' FILES'
      : 'MEMORY ONLY';
    document.getElementById('stat-persist-sub').textContent = data.cookiesDir || '';

    const badge = document.getElementById('overall-badge');
    if (accs.length > 0) {
      badge.className = 'pill green';
      badge.textContent = 'READY · ' + accs.length;
    } else {
      badge.className = 'pill amber';
      badge.textContent = 'NO COOKIE';
    }

    const tbody = document.getElementById('accounts-tbody');
    if (accs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);justify-content:center;">暂无账号，请先导入 Cookie</td></tr>';
    } else {
      tbody.innerHTML = accs.map(a => \`
        <tr>
          <td data-label="账号 ID"><code>\${a.accountId}</code></td>
          <td data-label="等级 (Tier)"><code>\${a.g1Tier || 'TIER0'}</code></td>
          <td data-label="AuthUser">\${a.authUser}</td>
          <td data-label="Cookie 数" title="\${(a.cookieNames || []).join(', ')}">\${a.cookieCount}</td>
          <td data-label="池状态">\${poolBadgeHtml(a.pool)}</td>
          <td data-label="当日调用 (15:00刷新)">\${renderUsageHtml(a)}</td>
          <td data-label="操作">
            \${renderTestBadge(a.accountId)}
            <button class="secondary" onclick="testAcc('\${a.accountId}')" style="padding:4px 10px;font-size:11.5px;margin-right:4px;">⚡ 测活</button>
            <button class="danger" onclick="removeAcc('\${a.accountId}')">移除</button>
          </td>
        </tr>
      \`).join('');
    }
  } catch (e) {}
}

async function submitManualCookie() {
  const raw = document.getElementById('manual-cookie').value.trim();
  if (!raw) return alert('请粘贴 Cookie 或油猴导出的 JSON');
  let bodyPayload = { cookies: raw, authUser: 0 };
  if (raw.startsWith('{')) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.cookies === 'string') {
        bodyPayload = {
          cookies: parsed.cookies,
          authUser: parsed.authUser !== undefined ? Number(parsed.authUser) : 0,
          g1Tier: parsed.g1Tier || null,
        };
      }
    } catch {}
  }
  const res = await fetch('/api/accounts/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(bodyPayload)
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    return alert('导入失败: ' + (data.error || '未知错误'));
  }
  document.getElementById('manual-cookie').value = '';
  refreshStatus();
}

async function testAcc(accountId) {
  testResults[accountId] = { loading: true };
  refreshStatus();
  try {
    const res = await fetch('/api/accounts/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accountId })
    });
    const data = await res.json();
    testResults[accountId] = {
      loading: false,
      alive: !!data.alive,
      elapsedMs: data.elapsedMs || 0,
      error: data.error || ''
    };
  } catch (e) {
    testResults[accountId] = { loading: false, alive: false, error: e.message };
  }
  refreshStatus();
}

async function removeAcc(accountId) {
  await fetch('/api/accounts/remove', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accountId })
  });
  refreshStatus();
}

refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>`;
}

module.exports = {
  MINIMAL_COOKIE_WHITELIST,
  extractMinimalCookies,
  computeAccountId,
  resolveModelAlias,
  RelayAccountStore,
  renderLoginHtml,
  renderDashboardHtml,
};
