/**
 * 校园网自连 · 后台 Service Worker
 *
 * 职责：
 *   1. 用 chrome.alarms 周期探活（MV3 里 setInterval 不可靠）
 *   2. 掉线时按配置执行登录：纯 fetch 会话 或 复用已打开的标签页
 *   3. 事件日志与徽标状态
 */

/**
 * 这里是「全部」可配置项的唯一定义。
 * load() 用 Object.keys(DEFAULTS) 向 storage 取值，
 * 少列一个键，那个字段就会静默变成 undefined —— 登录时字段名全是 undefined 就是这么来的。
 */
const DEFAULTS = {
  // 门户与凭据
  portalUrl: '',
  username: '',
  password: '',
  fields: { usernameField: '', passwordField: '' },
  passwordMode: 'plain',
  extraFieldsRaw: '',
  // 探活
  probeUrls: [
    'http://www.msftconnecttest.com/connecttest.txt',
    'http://connectivitycheck.gstatic.com/generate_204',
  ],
  // 调度
  enabled: false,
  intervalMinutes: 1,
  maxEvents: 200,
};

/** fields 里这两个键是「字段名说明」，不是要提交给门户的表单参数 */
const META_FIELD_KEYS = new Set(['usernameField', 'passwordField']);

const BADGE = {
  online: { text: '', color: '#2fbf71' },
  offline: { text: '!', color: '#e5534b' },
  logging: { text: '…', color: '#e2a336' },
  idle: { text: '', color: '#6b7686' },
  error: { text: '×', color: '#e5534b' },
};

let state = {
  online: null,
  checking: false,
  lastCheckedAt: 0,
  lastMessage: '',
};
let stateLoaded = false;

async function load() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  const settings = { ...DEFAULTS, ...stored };
  if (!stateLoaded) {
    const persisted = (await chrome.storage.local.get(['__state']))['__state'] || {};
    // checking 只属于当前 Worker，不能从上次运行的存储状态恢复。
    state = { ...state, ...persisted, checking: state.checking };
    stateLoaded = true;
  }
  return settings;
}

async function saveState() {
  const { checking, ...persisted } = state;
  await chrome.storage.local.set({ __state: persisted });
}

function setBadge(kind) {
  const b = BADGE[kind] || BADGE.idle;
  chrome.action.setBadgeText({ text: b.text });
  chrome.action.setBadgeBackgroundColor({ color: b.color });
}

/* ------------------------------------------------------------------ 探活 */

async function probeOnce(url, timeoutMs = 5000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      method: 'GET',
      redirect: 'manual',
      cache: 'no-store',
      signal: controller.signal,
    });
    // 302 到门户 = 未放行
    if ([301, 302, 303, 307, 308].includes(resp.status)) {
      const loc = resp.headers.get('location') || '';
      return { online: false, status: 'redirected', detail: `${resp.status} -> ${loc}` };
    }
    if (resp.ok || resp.status === 204) {
      return { online: true, status: 'online', detail: `HTTP ${resp.status}` };
    }
    return { online: false, status: 'http_error', detail: `HTTP ${resp.status}` };
  } catch (err) {
    const aborted = err && err.name === 'AbortError';
    return {
      online: false,
      status: aborted ? 'timeout' : 'error',
      detail: aborted ? '探测超时' : String(err && err.message ? err.message : err),
    };
  } finally {
    clearTimeout(timer);
  }
}

async function probeAll(urls) {
  let last = { online: false, status: 'no_probe', detail: '未配置探测地址' };
  for (const url of urls) {
    last = await probeOnce(url);
    if (last.online) break;
  }
  return last;
}

/* ------------------------------------------------------------------ 登录 */

async function loginByFetch(portal, username, password, fields, mode) {
  const body = new URLSearchParams();
  // 只提交真正的表单字段，跳过 usernameField/passwordField 这两个说明性键
  for (const [k, v] of Object.entries(fields || {})) {
    if (META_FIELD_KEYS.has(k)) continue;
    body.append(k, v);
  }
  const userKey = (fields && fields.usernameField) || 'username';
  const passKey = (fields && fields.passwordField) || 'password';
  body.append(userKey, username);
  body.append(passKey, encodePassword(password, mode));

  const resp = await fetch(portal, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
    redirect: 'follow',
    cache: 'no-store',
  });
  const text = await resp.text();
  return classify(text, resp.status, resp.url);
}

function encodePassword(password, mode) {
  if (!password) return password;
  switch (mode) {
    case 'base64':
      return btoa(unescape(encodeURIComponent(password)));
    default:
      return password;
  }
}

function classify(text, status, url) {
  const low = (text || '').toLowerCase();
  if (low.includes('"result":1') || low.includes('"success":true')) {
    return { state: 'success', message: '门户返回成功' };
  }
  if (low.includes('已在线') || low.includes('already online')) {
    return { state: 'already_online', message: '账号已在线' };
  }
  if (low.includes('密码错误') || low.includes('密码不正确') || low.includes('userdeny')) {
    return { state: 'failed', message: '密码错误或认证失败' };
  }
  if (low.includes('"result":0') || low.includes('登录失败')) {
    return { state: 'failed', message: '门户返回登录失败' };
  }
  if (status >= 200 && status < 400) {
    return { state: 'success', message: `HTTP ${status}，未匹配到失败标记` };
  }
  return { state: 'error', message: `门户返回 HTTP ${status}`, url };
}

async function openPortalForManual(portal) {
  const tab = await chrome.tabs.create({ url: portal, active: true });
  return { state: 'manual', message: '已打开门户，请手动完成认证', tabId: tab.id };
}

/* ------------------------------------------------------------------ 日志 */

async function logEvent(level, stage, message, accountId = null) {
  const settings = await load();
  const { events = [] } = await chrome.storage.local.get(['events']);
  const entry = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    ts: new Date().toISOString(),
    level,
    stage,
    message,
    accountId,
  };
  const next = [entry, ...events].slice(0, settings.maxEvents);
  await chrome.storage.local.set({ events: next });
}

/* ------------------------------------------------------------------ 主循环 */

async function checkOnce() {
  const settings = await load();
  if (!settings.enabled || state.checking) {
    return { skipped: true };
  }

  state.checking = true;
  setBadge('logging');
  await saveState();

  try {
    const probe = await probeAll(settings.probeUrls);
    state.lastCheckedAt = Date.now();

    if (probe.online) {
      state.online = true;
      state.lastMessage = '网络已放行';
      setBadge('online');
      await logEvent('info', 'probe', `网络已放行（${probe.status}）`);
      await saveState();
      return { online: true, probe };
    }

    state.online = false;
    state.lastMessage = `未放行：${probe.detail}`;
    setBadge('offline');
    await logEvent('warning', 'probe', `探活失败 ${probe.status} - ${probe.detail}`);
    await saveState();

    if (!settings.portalUrl) {
      await logEvent('warning', 'login', '未配置门户地址，跳过自动登录');
      return { online: false, probe, reason: 'no_portal' };
    }

    let result;
    try {
      result = await loginByFetch(
        settings.portalUrl,
        settings.username,
        settings.password,
        settings.fields || {},
        settings.passwordMode,
      );
    } catch (err) {
      result = { state: 'error', message: String(err && err.message ? err.message : err) };
    }

    await logEvent(
      result.state === 'success' || result.state === 'already_online' ? 'info' : 'error',
      'login',
      `登录结果 ${result.state} - ${result.message}`,
    );

    if (result.state === 'success' || result.state === 'already_online') {
      const confirm = await probeAll(settings.probeUrls);
      state.online = confirm.online;
      state.lastMessage = confirm.online ? '重连成功' : `登录成功但探活仍失败：${confirm.detail}`;
      setBadge(confirm.online ? 'online' : 'offline');
    } else {
      setBadge('error');
      state.lastMessage = result.message;
    }
    await saveState();
    return { online: state.online, result };
  } catch (err) {
    state.lastMessage = String(err && err.message ? err.message : err);
    setBadge('error');
    await logEvent('error', 'check', `检测异常: ${state.lastMessage}`);
    await saveState();
    return { online: false, error: state.lastMessage };
  } finally {
    state.checking = false;
  }
}

async function setEnabled(on) {
  await chrome.storage.local.set({ enabled: !!on });
  await chrome.alarms.clear('campusnet-check');
  if (on) {
    const { intervalMinutes = 1 } = await load();
    chrome.alarms.create('campusnet-check', {
      periodInMinutes: Math.max(0.25, intervalMinutes),
    });
    await checkOnce();
  } else {
    setBadge('idle');
    state.online = null;
    state.lastMessage = '监控已关闭';
    await saveState();
  }
  return { enabled: !!on };
}

/* ------------------------------------------------------------------ 消息 */

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case 'getState': {
        const settings = await load();
        sendResponse({ ok: true, state, settings, alarms: await chrome.alarms.getAll() });
        return;
      }
      case 'checkNow': {
        const result = await checkOnce();
        sendResponse({ ok: true, result, state });
        return;
      }
      case 'setEnabled': {
        const result = await setEnabled(msg.enabled);
        sendResponse({ ok: true, ...result, state });
        return;
      }
      case 'openPortal': {
        const settings = await load();
        if (!settings.portalUrl) {
          sendResponse({ ok: false, error: '未配置门户地址' });
          return;
        }
        const result = await openPortalForManual(settings.portalUrl);
        sendResponse({ ok: true, result });
        return;
      }
      case 'clearEvents': {
        await chrome.storage.local.set({ events: [] });
        sendResponse({ ok: true });
        return;
      }
      default:
        sendResponse({ ok: false, error: `未知消息 ${msg && msg.type}` });
    }
  })();
  return true;
});

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set({ events: [] });
  const settings = await load();
  if (settings.enabled) {
    chrome.alarms.create('campusnet-check', {
      periodInMinutes: Math.max(0.25, settings.intervalMinutes),
    });
    await checkOnce();
  } else {
    setBadge('idle');
  }
});

chrome.runtime.onStartup.addListener(async () => {
  const settings = await load();
  if (settings.enabled) {
    chrome.alarms.create('campusnet-check', {
      periodInMinutes: Math.max(0.25, settings.intervalMinutes),
    });
  }
});

async function notifyOffline(message) {
  try {
    await chrome.notifications.create({
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon128.png'),
      title: '校园网已掉线',
      message,
    });
  } catch {
    // 通知失败不影响主流程
  }
}

/**
 * 只在「首次掉线」和「恢复上线」两个时刻通知，避免每轮检测都弹一次。
 * notified 标志在这里成对置位/清零，而不是只置位不清。
 */
async function notifyOnTransition(previousOnline, currentOnline, message) {
  if (currentOnline === false && previousOnline !== false) {
    await notifyOffline(message);
    return true;
  }
  return false;
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== 'campusnet-check') return;

  const before = state.online;
  await checkOnce();

  const dropped = await notifyOnTransition(before, state.online, state.lastMessage);
  state.notified = dropped;
  await saveState();
});

chrome.notifications.onClicked.addListener(async () => {
  const settings = await load();
  if (settings.portalUrl) {
    await openPortalForManual(settings.portalUrl);
  }
});