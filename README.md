# 校园网自连

校园网掉线自动重连。探活发现断网后，自动登录认证门户把网络接回来。

提供三种形态，随你挑：**Web 控制台**、**命令行**、**浏览器插件**。

- 纯 Python 标准库运行时，3.10+ 装完就能跑
- 自动识别 Dr.COM / 深澜 / H3C / 新网 / 华为 / 锐捷 / 斐讯 / CAS 等门户
- 支持 GB2312 / GBK 中文门户（不会解成乱码）
- 登录过程逐阶段实时反馈，不是干等十几秒的黑盒

---

## 目录

- [快速开始](#快速开始)
- [三种用法](#三种用法)
- [真实校园网怎么配](#真实校园网怎么配)
- [没有掉线环境怎么测](#没有掉线环境怎么测)
- [浏览器插件](#浏览器插件)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [开发](#开发)

---

## 快速开始

### Windows

```
git clone https://github.com/enmingxiong821-cloud/campusnet-autoconnect.git
cd campusnet-autoconnect

python -m pip install -r utils\requirements.txt
python utils\run.py serve
```

浏览器打开 <http://127.0.0.1:2268>。

### Linux / macOS

```
git clone https://github.com/enmingxiong821-cloud/campusnet-autoconnect.git
cd campusnet-autoconnect

python3 -m pip install -r utils/requirements.txt
./utils/start.sh
```

浏览器打开 <http://127.0.0.1:2268>。

> Linux 下若中文显示乱码，先 `export PYTHONIOENCODING=utf-8`。

---

## 三种用法

### 1. Web 控制台（推荐）

```
python utils/run.py serve
```

打开 <http://127.0.0.1:2268>。

- 添加账号：门户地址、账号密码、登录方式
- 一键检测：掉线自动重连，顶部实时显示每个阶段
- 事件日志：完整的探活与登录记录
- 状态徽标：在线 / 掉线 / 登录失败一眼可见

### 2. 命令行

```
# 添加账号
python utils/run.py add \
  --name 宿舍 \
  --portal http://10.0.0.1/eportal/portal/login \
  --username 20240001 \
  --password 你的密码 \
  --password-mode base64

# 立即检测（掉线会自动登录）
python utils/run.py check

# 列出账号
python utils/run.py list

# 查看日志
python utils/run.py events --limit 20

# 自动发现本网络的门户（掉线状态下最有用）
python utils/run.py discover

# 启动后台监控
python utils/run.py serve
```

### 3. 浏览器插件

见 [浏览器插件](#浏览器插件)。

---

## 真实校园网怎么配

### 步骤一：确认处于掉线状态

```
python utils/run.py check
```

显示 `FAIL` 或 `logged_in` 说明网络是通的，此时配置不影响真实登录流程。
要测真实门户，需要先断开认证（拔网线 / 退出 Wi-Fi）。

### 步骤二：自动发现门户

**在掉线状态下**运行：

```
python utils/run.py discover
```

它会扫描本机网关的常见门户路径、识别厂商、提取表单字段名，然后打印出可直接复制的一键建号命令：

```
探活状态: redirected (HTTP 302 -> http://10.0.0.1/eportal/portal/login)

找到 3 个门户候选：

[1] Dr.COM / 深澜  HTTP 200
    地址   : http://10.0.0.1/eportal/portal/login
    标题   : Dr.COM 认证系统
    账号框 : DDDDD
    密码框 : upass
    提交路径: /eportal/portal/login/
    建议方式: login_mode=http password_mode=base64
    隐藏域 : 0MKKey, user_ip, user_mac

一键建号命令（用最佳候选）：
  python utils/run.py add --name 我的校园网 ...
```

### 步骤三：建号并验证

```
python utils/run.py check
```

### 步骤四：常驻运行

```
python utils/run.py serve
```

### 手动配置

若 `discover` 没识别出来，通常只需要填两个值：

| 字段 | 说明 |
|---|---|
| 门户地址 | 浏览器打开认证页后的地址 |
| 登录方式 | `http` / `browser` / `manual` |

**字段名留空会自动识别**，识别错了再手工指定。

### 各厂商参考

| 厂商 | 登录方式 | 密码编码 | 备注 |
|---|---|---|---|
| Dr.COM / 深澜 | http | base64 | 隐藏域 `0MKKey` `user_ip` `user_mac` 必须带 |
| 深菱 spl | http | plain | 路径含 `/sportal/` |
| H3C / 新网 | http | plain | |
| 华为 | browser | base64 | 多有验证码 |
| 锐捷 | http | plain | |
| 斐讯 Pholus | browser | base64 | 多有验证码 |
| CAS 单点登录 | browser | plain | 需真浏览器执行 JS |

---

## 没有掉线环境怎么测

绝大多数人平时网络都是通的，没法触发登录流程。
仓库自带一套本地模拟门户，可完整复现掉线与重连：

**窗口一**：

```
python utils/mock_portal.py --drop
```

模拟 Dr.COM 形态：GB2312 页面、字段 `DDDDD`/`upass`、密码 Base64、
隐藏域 `0MKKey`/`user_ip`/`user_mac`。掉线时探测地址 302 跳门户，登录成功才放行。

**窗口二**：

```
python utils/run.py add --name 模拟账号 \
  --portal http://127.0.0.1:2270/eportal/portal/login \
  --probe http://127.0.0.1:2271/probe \
  --username 20240001 --password campus@2024 \
  --password-mode base64 --login-mode http

python utils/run.py check
```

反复测掉线恢复：打开 <http://127.0.0.1:2270/admin> 点「切换 放行 / 掉线」。

> **注意**：模拟门户的放行开关是全局共享的。
> 测失败路径（密码写错）时，先删掉能登录成功的账号，
> 否则前面的账号会把网络恢复放行，后面的就检测不到掉线。

完整步骤见 [doc/自测指南.md](doc/自测指南.md)。

---

## 浏览器插件

Chrome / Edge：

1. 打开 `chrome://extensions`
2. 右上角开启「开发者模式」
3. 「加载已解压的扩展程序」
4. 选目录 `project/frontend/extension`
5. 点图标 → 「设置」，填入门户地址与账号

按需安装浏览器登录依赖（仅 `browser` 模式需要）：

```
python -m pip install playwright
python -m playwright install chromium
```

插件基于 Manifest V3，用 `chrome.alarms` 周期探活。掉线与恢复两个时刻会弹通知。

---

## 常见问题

**控制台打不开 / 启动后没反应**
端口被占。程序会在启动前检查并直接告诉你占用者：

```
[错误]   端口 2268（服务）已被占用，占用者：PID 12345（多半是上一个没关掉的实例）
        换个端口启动：python utils/run.py serve --port 2288
        或先关掉占用它的那个程序。
```

也可以让它自己找空闲端口：`python utils/run.py serve --auto-shift`

**显示 `连不上门户`**
门户地址不对，或当前不在该校园网网段。先跑 `discover` 确认。

**提示 `未匹配到明确失败标记`**
门户返回了非预期内容。可能需要浏览器模式，或该门户有额外验证。

**登录成功但探活仍失败**
门户认证成功但需要几秒生效。调大 `max_retries`，或稍后重试。

**有图片验证码**
纯 HTTP 无法处理。改用 `browser` 模式；若是扭曲图片验证码仍需人工介入，
程序会明确报错，不会假装成功。

**Linux 下中文乱码**
`export PYTHONIOENCODING=utf-8`。

**GBK 门户字段识别不出**
已内置 GB2312/GBK 解码。若仍失败，手工指定账号密码字段名。

---

## 安全说明

请认真阅读，这一点直接影响你是否适合用这个工具。

- **密码在本地混淆存储**，算法是 HMAC 派生的流密码，密钥存在
  `~/.campusnet_obf.key`。这能挡住明文肉眼检索，**挡不住本机 root 或其他本机用户**。
- **浏览器插件的密码是明文存在 `chrome.storage.local` 里**。浏览器扩展的存储
  本身无法加密，共用电脑或共享浏览器配置请勿使用插件形态。
- **命令行添加账号时密码会进 shell 历史**。建议改用 Web 控制台添加，
  或在命令前加空格（`--password` 前）临时关闭该行为。
- 本工具**只做你所在校园网的 Web 门户认证**，不涉及 802.1X 驱动层、
  不做 MAC 绑定绕过。请在你有权使用的网络环境下使用。

发现安全问题请开 issue 或直接联系作者。

---

## 开发

```
# 全部测试
python -m pytest project/backend/tests -q

# 扩展测试（改了 background.js 后必跑，MV3 无法在 Node 直接跑）
node project/frontend/extension/test-background.js
```

### 架构

```
utils/
  run.py            启动入口（内部处理 sys.path，规避中文路径问题）
  mock_portal.py    本地模拟校园网
project/backend/campusnet/
  paths.py          路径与端口常量
  charset.py        GBK/GB2312 字符集嗅探
  crypto.py         密码编码与本地混淆
  storage.py        SQLite 存储
  probe.py          探活判定
  portal.py         表单解析 + HTTP/浏览器登录
  monitor.py        周期调度与实时进度
  discovery.py      厂商无关的门户自动发现
  server.py         HTTP API + 静态托管
  lockfile.py       单实例互斥
  cli.py            命令行入口
project/frontend/
  index.html        Web 控制台
  extension/        浏览器插件（MV3）
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CAMPUSNET_API_PORT` | 2268 | 服务端口 |
| `CAMPUSNET_DB` | `database/campusnet.db` | 数据库路径 |
| `CAMPUSNET_OBF_KEY` | 自动生成 | 密码混淆密钥 |
| `PYTHONIOENCODING` | — | Linux 下建议设为 `utf-8` |

---

## License

[MIT](LICENSE)