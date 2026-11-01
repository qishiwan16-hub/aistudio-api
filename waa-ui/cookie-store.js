'use strict';

/**
 * Cookie Credential Persistence for Relay Client Gateway
 *
 * Layout:
 *   <COOKIES_DIR>/
 *     acc_<hash>.json     # one account (one cookie credential) per file
 *     ...
 *
 * File format:
 *   {
 *     "accountId": "acc_...",
 *     "authUser": 0,
 *     "g1Tier": "TIER1",
 *     "source": "manual",
 *     "addedAt": 1793372047555,
 *     "updatedAt": 1793372047555,
 *     "cookies": "SID=...; SAPISID=...; ..."
 *   }
 *
 * Security:
 *   - Directory is created with mode 0700, files with 0600 (best effort on Windows).
 *   - Writes are atomic (tmp file + rename) so a crash cannot truncate a credential.
 *   - Only whitelisted minimal cookies are stored (see MINIMAL_COOKIE_WHITELIST).
 */

const fs = require('node:fs');
const path = require('node:path');

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;

function safeFileName(accountId) {
  return String(accountId || 'account').replace(/[^A-Za-z0-9._-]/g, '_');
}

class CookieStore {
  constructor(dir) {
    this.dir = path.resolve(dir);
    fs.mkdirSync(this.dir, { recursive: true, mode: DIR_MODE });
    try {
      fs.chmodSync(this.dir, DIR_MODE);
    } catch {}
  }

  fileFor(accountId) {
    return path.join(this.dir, `${safeFileName(accountId)}.json`);
  }

  /** Read every valid `*.json` credential file (invalid files are skipped). */
  list() {
    const out = [];
    let names = [];
    try {
      names = fs.readdirSync(this.dir);
    } catch (err) {
      console.warn(`[cookie-store] 无法读取目录 ${this.dir}: ${err.message}`);
      return out;
    }
    for (const name of names) {
      if (!name.toLowerCase().endsWith('.json')) continue;
      const file = path.join(this.dir, name);
      try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
        if (parsed && typeof parsed.cookies === 'string' && parsed.cookies.trim()) {
          out.push({ file, data: parsed });
        } else {
          console.warn(`[cookie-store] 跳过缺少 cookies 字段的文件: ${file}`);
        }
      } catch (err) {
        console.warn(`[cookie-store] 跳过无法解析的文件: ${file} (${err.message})`);
      }
    }
    return out;
  }

  /** Atomically write one credential file. */
  save(entry) {
    const file = this.fileFor(entry.accountId);
    const tmp = `${file}.${process.pid}.tmp`;
    const payload = {
      accountId: entry.accountId,
      authUser: entry.authUser,
      g1Tier: entry.g1Tier || 'TIER0',
      source: entry.source || 'manual',
      addedAt: entry.addedAt,
      updatedAt: entry.updatedAt || Date.now(),
      requestsCount: entry.requestsCount || 0,
      dailyBucket: entry.dailyBucket || null,
      dailyModelCounts: entry.dailyModelCounts || {},
      cookies: entry.cookies,
    };
    fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, { mode: FILE_MODE });
    try {
      fs.chmodSync(tmp, FILE_MODE);
    } catch {}
    fs.renameSync(tmp, file);
    try {
      fs.chmodSync(file, FILE_MODE);
    } catch {}
    return file;
  }

  remove(accountId) {
    const file = this.fileFor(accountId);
    try {
      fs.unlinkSync(file);
      return true;
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
      return false;
    }
  }

  count() {
    try {
      return fs.readdirSync(this.dir).filter((n) => n.toLowerCase().endsWith('.json')).length;
    } catch {
      return 0;
    }
  }
}

module.exports = { CookieStore };
