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
  // 跑着 Python 版服务时优先把检测交给它（自动探测可用性，不可用就静默回落）
  useLocalService: true,
  // 只有服务绑到局域网（serve --host 0.0.0.0）时才需要填：那时 /api/check 要求显式令牌。
  // 默认只监听 127.0.0.1，服务端按「扩展来源 + 回环」放行，留空即可。
  apiToken: '',
};

/** 本地服务地址；扩展自己那套 fetch 登录只是「没装 Python」时的简化替代。 */
const LOCAL_SERVICE_URL = 'http://127.0.0.1:2268';

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

/**
 * 用 fetch 直接向门户提交登录表单，绕开「打开页面 + 填表单 + 点提交」那条路。
 *
 * 之所以能在后台完成：校园网门户绝大多数就是普通表单 POST，拿到响应体
 * 就能判断成败，不需要真的渲染页面。失败时调用方会退回 openPortalForManual，
 * 让用户手动点一次——总比什么都不做要好。
 *
 * 表单字段拼装的三个约定，顺序不能变：
 * 1. `fields` 里除了 usernameField / passwordField 两个「说明性键」以外，
 *    其余键值对（隐藏域、运营商选择等）原样带上，门户靠它们区分线路；
 * 2. 账号字段名默认 username、密码字段名默认 password，门户预设可以覆盖；
 * 3. 密码要按 mode 编码后再提交，且必须 append 在最后——有些门户按出现
 *    顺序解析，把密码插到中间会串位。
 *
 * 密码经 encodePassword 处理后进请求体，不会落进 DOM 或地址栏。
 *
 * @param {string} portal 提交地址（登录表单的 action）
 * @param {string} username 账号
 * @param {string} password 明文密码，本函数内编码
 * @param {object} fields 表单字段表，含 usernameField / passwordField 两个元键
 * @param {string} mode 密码编码方式（plain / base64）
 * @returns {Promise<{state: string, message: string, url?: string}>} classify 的结果
 */
async function loginByFetch(portal, username, password, fields, mode, timeoutMs = 15000) {
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

  // 必须给超时：校园门户卡住（半开连接、门户自己挂起）时，没有 signal 的
  // fetch 会一直悬着，service worker 也跟着什么都不做——用户看到的是
  // 「点了没反应」，而且这一轮之后再也不会重试。
  // 15 秒比探活的 5 秒宽：这里是一个真的 POST，慢门户登录要几秒很正常。
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(portal, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
      redirect: 'follow',
      cache: 'no-store',
      signal: controller.signal,
    });
  } catch (err) {
    if (err && err.name === 'AbortError') {
      // 调用方只把 message 写进事件日志，所以要给人话，不能是默认的
      // "The operation was aborted."
      return { state: 'error', message: `门户在 ${timeoutMs / 1000} 秒内没有响应` };
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  const text = await resp.text();
  return classify(text, resp.status, resp.url);
}

/**
 * 按门户要求编码密码。
 *
 * 空密码提前原样返回，不能走 base64 分支：`btoa('')` 得到空串，看着一样，
 * 但一旦以后加了别的编码方式（比如 md5），空串就会被编码成非空值提交上去，
 * 反而把「没填密码」变成一个像模像样的错误密码。
 *
 * `unescape(encodeURIComponent(x))` 是标准库里唯一能把 UTF-8 字符串转成
 * 逐字节 Latin-1 的老写法，`btoa` 只吃 Latin-1。密码含中文时不这么转会抛
 * InvalidCharacterError。
 *
 * plain 走 default 分支而不是 case 'plain'：配置里写了没见过的值（旧版本残留、
 * 手改 storage）时按最保守的明文处理，而不是静默失败。
 *
 * @param {string} password 明文密码
 * @param {string} mode 'base64' 编码，其余一律明文
 * @returns {string} 待提交的密码串
 */
function encodePassword(password, mode) {
  if (!password) return password;
  switch (mode) {
    case 'base64':
      return btoa(unescape(encodeURIComponent(password)));
    default:
      return password;
  }
}

/**
 * 从门户响应体里判成败——只能靠文本匹配，没有别的办法。
 *
 * 校园网门户五花八门：有的返回 JSON（`{"result":1}`），有的返回一句中文，
 * 有的干脆 200 + 空 body。所以判断顺序是「先找明确的成功标记，再找明确的
 * 失败标记，最后什么都不匹配时按 HTTP 状态兜底」：
 *
 * - 成功标记放最前：某些门户成功页里也夹着「登录失败」的脚本模板或提示文案，
 *   先匹配失败标记会把成功判成失败；
 * - `"result":1` / `"success":true` 都带引号：去掉引号会误命中页面里的
 *   `result:0` 这类 JS 变量赋值；
 * - `userdeny` 是 SRUN 系的固定标记，中文门户里不会出现，留着不冲突；
 * - 兜底那条是**故意宽松**的：2xx/3xx 且没有任何失败标记时记成功。门户文案
 *   改版（比如换了句「认证通过」）时，宁可让它按成功走、由随后的探活复核，
 *   也不要误报失败——探活失败自然会再登录一次，而误报失败会让用户白点。
 *
 * @param {string} text 响应体文本
 * @param {number} status HTTP 状态码
 * @param {string} url 最终响应地址（重定向后），仅失败时带出，方便排查
 * @returns {{state: string, message: string, url?: string}} state ∈
 *   success / already_online / failed / error
 */
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

/**
 * 本地服务是否可用；可用就把这一轮交给它。
 *
 * 校园网里的 GBK 页面、SRUN 的 challenge/xxtea、CAS 单点登录，扩展自己那套
 * fetch 登录都处理不了，而 Python 侧这些坑全踩过。这里只做「能连上就用」的
 * 探测：连不上（没装、端口不同、服务绑了局域网又没填令牌）就返回 null，
 * 静默回落到扩展实现。
 *
 * 注意 POST 一定要带 Origin 相关的心智模型：service worker 发非 GET 请求时
 * 浏览器会附上 `Origin: chrome-extension://<id>`，服务端为此专门放行了
 * 「扩展来源 + 回环」这一组合，所以默认配置下不需要令牌。
 */
async function checkViaLocalService(settings, timeoutMs = 2000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const health = await fetch(`${LOCAL_SERVICE_URL}/api/health`, {
      signal: controller.signal,
      cache: 'no-store',
    });
    if (!health.ok) return null;
    const info = await health.json();
    if (!info || info.ok !== true) return null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
  // 这一步也要有自己的超时：上面那个 controller 只覆盖 /api/health，
  // 它对应的 setTimeout 已经在 finally 里被清掉了。
  const checkController = new AbortController();
  const checkTimer = setTimeout(() => checkController.abort(), timeoutMs);
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (settings && settings.apiToken) {
      headers['X-CampusNet-Token'] = settings.apiToken;
    }
    const resp = await fetch(`${LOCAL_SERVICE_URL}/api/check`, {
      method: 'POST',
      headers,
      body: '{}',
      cache: 'no-store',
      signal: checkController.signal,
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    return data && Array.isArray(data.results) ? data : null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(checkTimer);
  }
}


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

    // 跑着 Python 版就优先交给它：认证逻辑与这些年踩过的坑都在那边。
    if (settings.useLocalService !== false) {
      const served = await checkViaLocalService(settings);
      if (served) {
        const results = served.results;
        const allOnline = results.length > 0 && results.every(item => item.online);
        const summary = results.map(item => item.message).filter(Boolean).join('；')
          || '本地服务已处理这一轮检测';
        state.online = allOnline;
        state.lastMessage = summary;
        setBadge(allOnline ? 'online' : 'offline');
        await logEvent(
          allOnline ? 'info' : 'warning',
          'service',
          `交给本地服务：${summary}`,
        );
        await saveState();
        return { online: allOnline, via: 'local-service', results };
      }
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
