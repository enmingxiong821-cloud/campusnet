/**
 * 扩展后台逻辑的行为测试（Node 环境，不依赖真实 Chrome）。
 * 用法：node project/frontend/extension/test-background.js
 *
 * 目的：classify / encodePassword / probeOnce / 状态流转这些纯逻辑
 * 在真实扩展里跑一次代价很高，这里用 stub 复现同一份代码来验证。
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, 'background.js'), 'utf8');

/* ---------------- 断言工具 ---------------- */

let passed = 0;
const failures = [];

/* ---------------- 分组 ---------------- */

/**
 * 三组是按「被测对象的依赖层级」分的，不是为了排版好看：
 *   ① 纯函数        —— 不碰 chrome 也不碰网络，挂了必定是逻辑错
 *   ② 探活与登录    —— 只依赖 fetch，是「没装 Python」时扩展自己的主线
 *   ③ 状态与消息    —— 依赖 chrome.storage / alarms / runtime，管的是跨轮次状态
 *
 * 分组名存在 currentGroup 上，check() 顺手把计数记进当前组，跑完按组汇总：
 * 某组整片挂掉时能立刻判断是哪一层塌了，而不是只看到一个总数。
 */
let currentGroup = '(未分组)';
const groupStats = new Map();

function describe(name) {
  currentGroup = name;
  if (!groupStats.has(name)) groupStats.set(name, { passed: 0, failed: 0 });
  console.log(`\n━━━ ${name} ━━━`);
}

function check(name, fn) {
  const stats = groupStats.get(currentGroup);
  try {
    fn();
    passed += 1;
    if (stats) stats.passed += 1;
    console.log(`  PASS  ${name}`);
  } catch (err) {
    failures.push({ name, message: err.message });
    if (stats) stats.failed += 1;
    console.log(`  FAIL  ${name}: ${err.message}`);
  }
}

function eq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(`${label || ''} 期望 ${e}，实际 ${a}`);
  }
}

function truthy(value, label) {
  if (!value) throw new Error(`${label || ''} 期望为真，实际 ${JSON.stringify(value)}`);
}

/* ---------------- chrome stub ---------------- */

function makeChrome(storage) {
  const listeners = {};
  const badges = [];
  const alarms = [];
  const notifications = [];
  const tabs = [];

  const local = {
    async get(keys) {
      if (keys === undefined || keys === null) return structuredClone(storage.data);
      if (Array.isArray(keys)) {
        const out = {};
        for (const k of keys) {
          if (k in storage.data) out[k] = structuredClone(storage.data[k]);
        }
        return out;
      }
      if (typeof keys === 'string') {
        return keys in storage.data ? { [keys]: structuredClone(storage.data[keys]) } : {};
      }
      return structuredClone(storage.data);
    },
    async set(obj) {
      Object.assign(storage.data, structuredClone(obj));
    },
    async remove(keys) {
      for (const k of Array.isArray(keys) ? keys : [keys]) delete storage.data[k];
    },
  };

  const chrome = {
    storage: { local },
    runtime: {
      getURL: p => `chrome-extension://test/${p}`,
      onMessage: { addListener: fn => { listeners.message = fn; } },
      onInstalled: { addListener: fn => { listeners.installed = fn; } },
      onStartup: { addListener: fn => { listeners.startup = fn; } },
    },
    alarms: {
      create: (name, info) => alarms.push({ name, ...info }),
      clear: async name => alarms.splice(alarms.indexOf(alarms.find(a => a.name === name)), 1),
      getAll: async () => alarms.slice(),
      onAlarm: { addListener: fn => { listeners.alarm = fn; } },
    },
    notifications: {
      create: async opts => notifications.push(opts),
      onClicked: { addListener: fn => { listeners.notificationClick = fn; } },
    },
    action: {
      setBadgeText: async opts => badges.push({ kind: 'text', ...opts }),
      setBadgeBackgroundColor: async opts => badges.push({ kind: 'color', ...opts }),
    },
    tabs: {
      create: async opts => {
        tabs.push(opts);
        return { id: tabs.length };
      },
    },
    __listeners: listeners,
    __badges: badges,
    __alarms: alarms,
    __notifications: notifications,
    __tabs: tabs,
  };
  return chrome;
}

function loadBackground(storage, fetchImpl) {
  const chrome = makeChrome(storage);
  const sandbox = {
    chrome,
    console,
    btoa: s => Buffer.from(s, 'binary').toString('base64'),
    fetch: fetchImpl,
    AbortController,
    setTimeout,
    clearTimeout,
    Date,
    Math,
    URLSearchParams,
    Error,
    JSON,
     Object,
     Array,
     Promise,
     structuredClone,
    String,
    encodeURIComponent,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'background.js' });
  return { chrome, api: sandbox, sandbox };
}

/* ---------------- 纯逻辑测试（不依赖 chrome 调用） ---------------- */

console.log('\n=== 扩展后台逻辑测试 ===\n');

const { api: probeApi } = loadBackground({ data: {} }, async () => ({}));

describe('① 纯函数：响应分类与密码编码');

console.log('[classify 响应分类]');
const cases = [
  ['门户 result:1 判成功', '{"result":1}', 200, '/', 'success'],
  ['门户 success:true 判成功', '{"success":true}', 200, '/', 'success'],
  ['已在线', '您已在线，请勿重复登录', 200, '/', 'already_online'],
  ['already online 英文', 'You are already online', 200, '/', 'already_online'],
  ['密码错误判失败', '用户名或密码错误', 200, '/', 'failed'],
  ['userdeny 判失败', '"result":0,"message":"userdeny"', 200, '/', 'failed'],
  ['登录失败判失败', '登录失败', 200, '/', 'failed'],
  ['HTTP 500 判错误', 'server error', 500, '/', 'error'],
];
for (const [name, text, status, url, expected] of cases) {
  check(name, () => eq(probeApi.classify(text, status, url).state, expected, 'state'));
}

check('未匹配失败标记时按成功处理', () => {
  eq(probeApi.classify('<html>欢迎</html>', 200, '/').state, 'success');
});

check('空响应体不崩', () => {
  truthy(probeApi.classify('', 204, '/').state);
});

console.log('\n[encodePassword 密码编码]');
check('plain 原样返回', () => eq(probeApi.encodePassword('abc', 'plain'), 'abc'));
check('plain 遇未知 mode 也原样返回', () => eq(probeApi.encodePassword('abc', 'md5'), 'abc'));
check('base64 编码 ASCII', () => eq(probeApi.encodePassword('abc', 'base64'), 'YWJj'));
check('base64 编码中文走 UTF-8', () =>
  eq(probeApi.encodePassword('中文', 'base64'), Buffer.from('中文', 'utf8').toString('base64')));
check('空密码不编码', () => eq(probeApi.encodePassword('', 'base64'), ''));

describe('② 探活与登录');

console.log('\n[probeOnce 探活判定]');

function probeWith(fetchImpl) {
  const { api } = loadBackground({ data: {} }, fetchImpl);
  return api.probeOnce('http://probe.test/x');
}

(async () => {
  await (async () => {
    console.log('\n[探活 —— 异步]');

    const online = await probeWith(async () => ({ ok: true, status: 200 }));
    check('HTTP 200 判在线', () => {
      truthy(online.online);
      eq(online.status, 'online');
    });

    const noContent = await probeWith(async () => ({ ok: false, status: 204 }));
    check('HTTP 204 判在线', () => truthy(noContent.online));

    const redirected = await probeWith(async () => ({
      status: 302,
      headers: { get: k => (k === 'location' ? 'http://portal/login' : null) },
    }));
    check('302 跳门户判未放行', () => {
      truthy(!redirected.online);
      eq(redirected.status, 'redirected');
      truthy(redirected.detail.includes('portal/login'), 'detail 含门户地址');
    });

    const errResp = await probeWith(async () => ({ ok: false, status: 500 }));
    check('HTTP 500 判未放行', () => {
      truthy(!errResp.online);
      eq(errResp.status, 'http_error');
    });

    const networkErr = await probeWith(async () => {
      const e = new Error('Failed to fetch');
      throw e;
    });
    check('网络异常判未放行且不抛', () => {
      truthy(!networkErr.online);
      eq(networkErr.status, 'error');
    });

    const abortErr = await probeWith(async () => {
      const e = new Error('aborted');
      e.name = 'AbortError';
      throw e;
    });
    check('超时判 timeout', () => {
      truthy(!abortErr.online);
      eq(abortErr.status, 'timeout');
    });

    console.log('\n[主流程 checkOnce]');

    // 场景一：在线 → 不应触发登录
    {
      const storage = { data: { enabled: true, portalUrl: 'http://portal/login' } };
      let fetchCount = 0;
      const { api, chrome } = loadBackground(storage, async () => {
        fetchCount += 1;
        return { ok: true, status: 200 };
      });
      const result = await api.checkOnce();
      check('在线时只探活一次、不登录', () => {
        eq(fetchCount, 1, 'fetch 次数');
        eq(result.online, true);
      });
      check('在线时 badge 变绿', () => {
        truthy(chrome.__badges.some(b => b.kind === 'text' && b.text === ''), '空 badge');
      });
      check('事件日志记录探活结果', () => {
        const events = storage.data.events || [];
        truthy(events.length >= 1, '有事件');
        truthy(events[0].message.includes('放行'), events[0].message);
      });
    }

    // 场景二：掉线 + 无门户 → 跳过登录
    {
      const storage = { data: { enabled: true, portalUrl: '' } };
      const { api } = loadBackground(storage, async () => ({
        status: 302,
        headers: { get: () => 'http://portal/login' },
      }));
      const result = await api.checkOnce();
      check('掉线且未配门户时跳过登录', () => eq(result.reason, 'no_portal'));
      check('掉线时状态记为离线', () => {
        const s = storage.data.__state;
        eq(s.online, false);
      });
    }

    // 场景三：掉线 + 登录成功 + 复检放行
    // mock 必须按 URL 路由：probeAll 会依次试多个探测地址，
    // 若按调用计数返回，第二个探测地址就会把「掉线」演变成「在线」，登录根本不会触发。
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://portal/login',
          username: 'u',
          password: 'p',
          fields: {},
          probeUrls: ['http://probe.test/x'],
        },
      };
      let posted = 0;
      let online = false;
      const { api } = loadBackground(storage, async (url) => {
        if (url === 'http://portal/login') {
          posted += 1;
          return { ok: true, status: 200, text: async () => '{"result":1}' };
        }
        if (online) return { ok: true, status: 200 };
        return { status: 302, headers: { get: () => 'http://portal/login' } };
      });
      const result = await api.checkOnce();
      check('掉线后登录并复检成功', () => {
        eq(posted, 1, '登录被调用一次');
        eq(result.online, false, '登录后未复检成功，仍离线');
        eq(storage.data.__state.online, false);
      });

      // 复检通过的场景：登录后网络确实放行
      const storage2 = {
        data: { ...storage.data, events: [] },
      };
      let posted2 = 0;
      let online2 = false;
      const { api: api2 } = loadBackground(storage2, async (url) => {
        if (url === 'http://portal/login') {
          posted2 += 1;
          online2 = true; // 登录成功后网络放行
          return { ok: true, status: 200, text: async () => '{"result":1}' };
        }
        if (online2) return { ok: true, status: 200 };
        return { status: 302, headers: { get: () => 'http://portal/login' } };
      });
      const okResult = await api2.checkOnce();
      check('登录成功后复检放行则判定在线', () => {
        eq(posted2, 1);
        eq(okResult.online, true);
        eq(storage2.data.__state.online, true);
      });
    }

    // 场景四：掉线 + 登录失败
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://portal/login',
          username: 'u',
          password: 'p',
          fields: {},
          probeUrls: ['http://probe.test/x'],
        },
      };
      const { api, chrome } = loadBackground(storage, async (url) => {
        if (url === 'http://portal/login') {
          return { ok: true, status: 200, text: async () => '密码错误' };
        }
        return { status: 302, headers: { get: () => 'http://portal/login' } };
      });
      const result = await api.checkOnce();
      check('密码错误时保持离线', () => {
        eq(result.online, false);
        eq(storage.data.__state.online, false);
      });
      check('登录失败时 badge 显示错误', () => {
        truthy(chrome.__badges.some(b => b.kind === 'text' && b.text === '×'), '错误 badge');
      });
    }

    // 场景五：未开启监控时跳过
    {
      const storage = { data: { enabled: false } };
      let called = 0;
      const { api } = loadBackground(storage, async () => { called += 1; return { ok: true, status: 200 }; });
      const result = await api.checkOnce();
      check('未开启监控时直接跳过', () => {
        eq(result.skipped, true);
        eq(called, 0);
      });
    }

    console.log('\n[probeAll 多地址探测]');
    {
      const storage = { data: {} };
      let n = 0;
      const { api } = loadBackground(storage, async () => {
        n += 1;
        // 第一个不通，第二个放行
        if (n === 1) return { status: 302, headers: { get: () => '/p' } };
        return { ok: true, status: 200 };
      });
      const r = await api.probeAll(['http://a', 'http://b', 'http://c']);
      check('任一放行即返回在线', () => {
        truthy(r.online);
        eq(n, 2, '在第二个就停');
      });
    }

    {
      const { api } = loadBackground({ data: {} }, async () => ({
        status: 302, headers: { get: () => '/p' },
      }));
      const r = await api.probeAll(['http://a']);
      check('全部不通时返回未放行', () => truthy(!r.online));
    }

    {
      const { api } = loadBackground({ data: {} }, async () => ({ ok: true, status: 200 }));
      const r = await api.probeAll([]);
      check('空地址列表有兜底提示', () => {
        eq(r.status, 'no_probe');
        truthy(!r.online);
      });
    }

    console.log('\n[登录表单提交]');
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://p/login',
          username: '20240001',
          password: 'secret',
          passwordMode: 'base64',
          probeUrls: ['http://probe.test/x'],
          fields: { usernameField: 'DDDDD', passwordField: 'upass', '0MKKey': '1' },
        },
      };
      let captured = null;
      const { api } = loadBackground(storage, async (url, opts) => {
        if (url === 'http://p/login') {
          captured = { url, body: opts.body, method: opts.method };
          return { ok: true, status: 200, text: async () => '{"result":1}' };
        }
        return { status: 302, headers: { get: () => 'http://p/login' } };
      });
      await api.checkOnce();
      check('字段名与隐藏域都进了请求体', () => {
        truthy(captured, '捕获到 POST');
        eq(captured.url, 'http://p/login', '提交地址');
        eq(captured.method, 'POST', '提交方法');
        truthy(captured.body.includes('DDDDD=20240001'), captured.body);
        truthy(captured.body.includes('0MKKey=1'), captured.body);
        truthy(
          captured.body.includes('upass=' + Buffer.from('secret', 'binary').toString('base64')),
          captured.body,
        );
      });
    }

    // fields 里只写了字段名说明、没带任何实际参数时应有的样子
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://p/login',
          username: 'u1',
          password: 'p1',
          probeUrls: ['http://probe.test/x'],
          fields: { usernameField: 'DDDDD', passwordField: 'upass' },
        },
      };
      let captured = null;
      const { api } = loadBackground(storage, async (url, opts) => {
        if (url === 'http://p/login') {
          captured = opts.body;
          return { ok: true, status: 200, text: async () => '{"result":1}' };
        }
        return { status: 302, headers: { get: () => 'http://p/login' } };
      });
      await api.checkOnce();
      check('两个说明性键本身不提交给门户', () => {
        truthy(captured, '捕获到 POST');
        eq(captured.includes('usernameField'), false, 'usernameField 不该出现在请求体里');
        eq(captured.includes('passwordField'), false, 'passwordField 不该出现在请求体里');
        truthy(captured.includes('DDDDD=u1'), captured);
        truthy(captured.includes('upass=p1'), captured);
      });
    }

    // fields 整个缺失（老版本 storage 里没这个键）：退回默认键名，不能崩
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://p/login',
          username: 'u2',
          password: 'p2',
          probeUrls: ['http://probe.test/x'],
        },
      };
      let captured = null;
      const { api } = loadBackground(storage, async (url, opts) => {
        if (url === 'http://p/login') {
          captured = opts.body;
          return { ok: true, status: 200, text: async () => '{"result":1}' };
        }
        return { status: 302, headers: { get: () => 'http://p/login' } };
      });
      await api.checkOnce();
      check('fields 缺失时用默认 username/password 键名', () => {
        truthy(captured, '捕获到 POST');
        truthy(captured.includes('username=u2'), captured);
        truthy(captured.includes('password=p2'), captured);
      });
    }

    console.log('\n[并发保护]');
    {
      const storage = { data: { enabled: true } };
      let inflight = 0;
      let maxInflight = 0;
      const { api } = loadBackground(storage, async () => {
        inflight += 1;
        maxInflight = Math.max(maxInflight, inflight);
        await new Promise(r => setTimeout(r, 20));
        inflight -= 1;
        return { ok: true, status: 200 };
      });
      const [a, b] = await Promise.all([api.checkOnce(), api.checkOnce()]);
      check('并发调用不会重复探活', () => {
        eq(maxInflight, 1, '最大并发数');
        eq(b.skipped, true, '第二次被跳过');
        truthy(a.online);
      });
    }

    describe('③ 状态、通知、消息通道与本地服务');

    console.log('\n[setEnabled 开关与闹钟]');
    {
      const storage = { data: {} };
      const { api, chrome } = loadBackground(storage, async () => ({ ok: true, status: 200 }));
      await api.setEnabled(true);
      check('开启后创建周期闹钟', () => {
        truthy(chrome.__alarms.some(a => a.name === 'campusnet-check'));
        eq(storage.data.enabled, true);
      });

      await api.setEnabled(false);
      check('关闭后清掉闹钟', () => {
        eq(chrome.__alarms.filter(a => a.name === 'campusnet-check').length, 0);
        eq(storage.data.enabled, false);
      });
      check('关闭后状态归 null', () => eq(storage.data.__state.online, null));
    }

    {
      const storage = { data: {} };
      const { api, chrome } = loadBackground(storage, async () => ({ ok: true, status: 200 }));
      await api.setEnabled(true);
      await api.setEnabled(true);
      check('重复开启不会堆叠多个闹钟', () => {
        eq(chrome.__alarms.filter(a => a.name === 'campusnet-check').length, 1);
      });
    }

    {
      const storage = { data: {} };
      const { api, chrome } = loadBackground(storage, async () => ({ ok: true, status: 200 }));
      await api.setEnabled(true);
      const tiny = chrome.__alarms.find(a => a.name === 'campusnet-check');
      eq(tiny.periodInMinutes, 1, '默认 1 分钟');
    }

    console.log('\n[通知去重：只在掉线瞬间响一次]');
    {
      const storage = { data: { enabled: true, portalUrl: 'http://p/login' } };
      let online = true;
      const { api, chrome } = loadBackground(storage, async () => {
        if (!online) return { status: 302, headers: { get: () => '/p' } };
        return { ok: true, status: 200 };
      });

      // 先在线跑一轮，把状态置为 online
      await api.checkOnce();
      eq(storage.data.__state.online, true, '初始在线');

      // 转成掉线，触发一次通知
      online = false;
      await chrome.__listeners.alarm({ name: 'campusnet-check' });
      eq(chrome.__notifications.length, 1, '掉线时通知一次');

      // 继续掉线，不再通知
      await chrome.__listeners.alarm({ name: 'campusnet-check' });
      eq(chrome.__notifications.length, 1, '持续掉线不重复通知');

      // 恢复在线后再次掉线，应重新通知
      online = true;
      await chrome.__listeners.alarm({ name: 'campusnet-check' });
      eq(storage.data.__state.online, true, '恢复在线');
      online = false;
      await chrome.__listeners.alarm({ name: 'campusnet-check' });
      eq(chrome.__notifications.length, 2, '再次掉线重新通知');
    }

    {
      const storage = { data: { enabled: true, portalUrl: 'http://p/login' } };
      const { api, chrome } = loadBackground(storage, async () => ({
        status: 302, headers: { get: () => '/p' },
      }));
      // 从未在线过，直接掉线：也应通知
      await chrome.__listeners.alarm({ name: 'campusnet-check' });
      eq(chrome.__notifications.length, 1, '首次即掉线也通知');
    }

    console.log('\n[消息通道]');
    {
      const storage = { data: {} };
      const { api, chrome } = loadBackground(storage, async () => ({ ok: true, status: 200 }));

      const call = (msg) =>
        new Promise(resolve => chrome.__listeners.message(msg, {}, resolve));

      const st = await call({ type: 'getState' });
      check('getState 返回状态与配置', () => {
        eq(st.ok, true);
        truthy(st.state !== undefined);
        truthy(st.settings !== undefined);
      });

      const en = await call({ type: 'setEnabled', enabled: true });
      check('setEnabled 走消息通道生效', () => eq(en.enabled, true));

      const bad = await call({ type: 'nonsense' });
      check('未知消息返回错误而非崩溃', () => eq(bad.ok, false));

      const noPortal = await call({ type: 'openPortal' });
      check('未配门户时 openPortal 报错', () => eq(noPortal.ok, false));
    }

    {
      const storage = { data: { portalUrl: 'http://p/login', events: [{ id: 'x' }] } };
      const { chrome } = loadBackground(storage, async () => ({ ok: true, status: 200 }));
      const call = msg => new Promise(resolve => chrome.__listeners.message(msg, {}, resolve));

      const open = await call({ type: 'openPortal' });
      check('openPortal 打开标签页', () => {
        eq(open.result.state, 'manual');
        eq(chrome.__tabs.length, 1);
        eq(chrome.__tabs[0].url, 'http://p/login');
      });

      await call({ type: 'clearEvents' });
      check('clearEvents 清空日志', () => eq(storage.data.events.length, 0));
    }

    console.log('\n[Worker 状态恢复]');
    {
      const storage = { data: { enabled: true, __state: { checking: true } } };
      let calls = 0;
      const fetchImpl = async () => {
        calls += 1;
        return { ok: true, status: 200 };
      };
      const first = loadBackground(storage, fetchImpl);
      const a = await first.api.checkOnce();
      const b = await first.api.checkOnce();
      const restarted = loadBackground(storage, fetchImpl);
      const c = await restarted.api.checkOnce();
      check('旧互斥状态不阻止连续检测及 Worker 重建', () => {
        truthy(a.online && b.online && c.online);
        eq(calls, 3);
        eq(Object.hasOwn(storage.data.__state, 'checking'), false);
      });
    }

    console.log('\n[事件日志上限]');
    {
      const storage = { data: { enabled: true, maxEvents: 5 } };
      const { api } = loadBackground(storage, async () => ({ ok: true, status: 200 }));
      for (let i = 0; i < 9; i += 1) {
        await api.logEvent('info', 'probe', `第 ${i} 条`);
      }
      check('日志按 maxEvents 截断且最新的在前', () => {
        eq(storage.data.events.length, 5);
        truthy(storage.data.events[0].message.includes('第 8 条'), storage.data.events[0].message);
      });
    }

    console.log('\n[优先使用本地服务]');

    // 本地服务可用：掉线时把这一轮交给它，而不是走扩展自己的 fetch 登录
    {
      const storage = { data: { enabled: true, portalUrl: 'http://portal/login' } };
      const calls = [];
      let checkOptions = null;
      const { api } = loadBackground(storage, async (url, options = {}) => {
        calls.push(url);
        if (url.endsWith('/api/health')) {
          return { ok: true, status: 200, json: async () => ({ ok: true, version: '1.4.0' }) };
        }
        if (url.endsWith('/api/check')) {
          checkOptions = options;
          return {
            ok: true,
            status: 200,
            json: async () => ({
              ok: true,
              results: [{ online: true, state: 'online', message: '网络已放行' }],
            }),
          };
        }
        return { status: 302, headers: { get: () => 'http://portal/login' } };
      });
      const result = await api.checkOnce();
      check('本地服务可用时交给它处理', () => {
        eq(result.via, 'local-service');
        eq(result.online, true);
        truthy(calls.some(u => u.endsWith('/api/check')), '调用了 /api/check');
        eq(storage.data.__state.online, true);
      });
      // 默认配置（服务只在 127.0.0.1）不该带令牌：服务端按「扩展来源 + 回环」放行。
      // 带上多余的头只会让排查问题时更难看清是谁在拦。
      check('未配置令牌时不带 X-CampusNet-Token', () => {
        const headers = (checkOptions && checkOptions.headers) || {};
        eq(headers['X-CampusNet-Token'], undefined, '令牌头');
        eq(headers['Content-Type'], 'application/json', '内容类型');
      });
    }

    // 服务绑到局域网（serve --host 0.0.0.0）时 /api/check 要求显式令牌，必须带上
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://portal/login',
          apiToken: 'tok-abcdefghijklmnop',
        },
      };
      let checkOptions = null;
      const { api } = loadBackground(storage, async (url, options = {}) => {
        if (url.endsWith('/api/health')) {
          return { ok: true, status: 200, json: async () => ({ ok: true }) };
        }
        if (url.endsWith('/api/check')) {
          checkOptions = options;
          return {
            ok: true,
            status: 200,
            json: async () => ({ ok: true, results: [{ online: true, state: 'online' }] }),
          };
        }
        return { status: 302, headers: { get: () => 'http://portal/login' } };
      });
      await api.checkOnce();
      check('配置了令牌就给 /api/check 带上令牌头', () => {
        const headers = (checkOptions && checkOptions.headers) || {};
        eq(headers['X-CampusNet-Token'], 'tok-abcdefghijklmnop', '令牌头');
      });
    }

    // 本地服务不可用（没装 / 端口不同 / 需要令牌）：静默回落，不能报错
    {
      const storage = {
        data: {
          enabled: true,
          portalUrl: 'http://portal/login',
          username: 'u',
          password: 'p',
          probeUrls: ['http://probe.test/x'],
        },
      };
      let posted = 0;
      const { api } = loadBackground(storage, async (url) => {
        if (url.includes('127.0.0.1:2268')) throw new Error('Failed to fetch');
        if (url === 'http://portal/login') {
          posted += 1;
          return { ok: true, status: 200, text: async () => '{"result":1}' };
        }
        if (url === 'http://probe.test/x') {
          return { status: 302, headers: { get: () => 'http://portal/login' } };
        }
        return { ok: true, status: 200 };
      });
      await api.checkOnce();
      check('本地服务不可用时回落到扩展自己的登录', () => {
        eq(posted, 1, '扩展自己发起了登录');
      });
    }

    // —— DEFAULTS 有两份副本：background.js 与 options.html（设置页内联脚本）。
    // 两边各自被 chrome.storage 读写，少列一个键，「恢复默认设置」
    // （remove(Object.keys(DEFAULTS))）就会漏掉它，load() 那侧还会读到 undefined。
    // 典型的「改一处忘另一处」，用文本对照锁住，不靠人记得。
    describe('默认配置 DEFAULTS（两份副本）');
    check('options.html 的 DEFAULTS 键与 background.js 完全一致', () => {
      const keysOf = (file) => {
        const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
        const m = /const DEFAULTS = \{[\s\S]*?\n\};/.exec(src);
        if (!m) throw new Error(`${file} 里没找到 DEFAULTS`);
        const literal = m[0].replace(/^const DEFAULTS = /, '').replace(/;$/, '');
        return Object.keys(vm.runInNewContext('(' + literal + ')')).sort();
      };
      eq(
        JSON.stringify(keysOf('options.html')),
        JSON.stringify(keysOf('background.js')),
        '两处 DEFAULTS 的键集合必须相同'
      );
    });

    // —— popup.html / options.html 的交互都写在内联 <script> 里，而 CI 只对
    // background.js 跑 node --check，这两个页面改坏了要等用户点开才发现。
    // 这里至少把语法解析一遍：模板字符串少个反引号、括号没配对，整页脚本会
    // 一声不响地不执行，页面看上去却一切正常。
    describe('内联脚本可解析（popup.html / options.html）');
    for (const file of ['popup.html', 'options.html']) {
      check(`${file} 的内联脚本能被解析`, () => {
        const html = fs.readFileSync(path.join(__dirname, file), 'utf8');
        const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
        if (!scripts.length) throw new Error(`${file} 里没有内联脚本`);
        scripts.forEach((src, i) => {
          try {
            new vm.Script(src, { filename: `${file}#${i + 1}` });
          } catch (e) {
            throw new Error(`${file} 第 ${i + 1} 段脚本解析失败：${e.message}`);
          }
        });
      });
    }

    console.log(`\n=== 结果：${passed} 通过，${failures.length} 失败 ===\n`);
    for (const [name, s] of groupStats) {
      const mark = s.failed === 0 ? 'OK  ' : 'FAIL';
      const bad = s.failed ? `，${s.failed} 失败` : '';
      console.log(`  ${mark}  ${name}：${s.passed} 通过${bad}`);
    }
    console.log('');
    if (failures.length) {
      for (const f of failures) console.log(`  - ${f.name}: ${f.message}`);
      process.exit(1);
    }
  })();
})();
