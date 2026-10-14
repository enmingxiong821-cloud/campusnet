# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，
条目按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的方式归类。

变更条目取自提交记录。**未发布**一节是 `main` 上已有、尚未打 tag 的内容；
这里只记「用户能感知到什么变了」，实现细节看对应提交。

## [未发布]

## [2.0.1] - 2026-10-14

一次全项目体检后的加固：三处安全口子、若干只在特定环境下才现形的行为 bug，
以及分发与安装链路上几处已经断掉的东西。

### 安全

- **门户探测与发现只接受 http/https**：`probe` 与 `discover` 的地址参数是外部可控的，
  而 urllib 默认挂着 FileHandler，喂一个 `file://` 进来会把本地文件当门户页读出来
  （探测结果里的正文就是页面开头一段）。非 http/https 一律拒掉，发现流程退回默认探活地址。
- **配对码防猜解**：以前只有「码存在但过期」才累计失败次数，猜一个不存在的码不算数——
  而这正是暴力猜解走的那条路。现在同一个来源猜错也计数（5 分钟窗口内到限即 429）；
  同一个码被两个请求同时领，也只有第一个能成功。
- **加密密钥文件以 0600 直接建出**：原来是先按 umask（通常 0644）落盘再 chmod，
  这中间同机其他用户能读到这把钥匙，而它解开库里所有账号密码。
- **防 DNS 重绑定**：免令牌模式（服务绑回环）下 `/api/` 现在要求 Host 指向回环地址。
  以前只校验 Origin，而攻击者把自有域名解析到 `127.0.0.1` 时，浏览器认为「页面」与
  「被访问的服务」同源、Origin 与 Host 天然相等，该页面即可读走 `/api/settings`
  里的 webhook 密钥、直接调用 `/api/power/off`。**在服务前面放反向代理（含
  Tailscale Serve 这类会把 Host 换成外部域名的入口）的人需要设
  `CAMPUSNET_TRUST_PROXY=1`**，否则会收到 403；那一层要自己负责鉴权。

### 修复

- **windows 上必然失败的 4 条用例**：种子地址是回环（`127.0.0.1`）时会被当成
  「DNS 劫持」，凭空多扫一个 `http://127.0.0.1/`，用例红绿于是取决于那台机器上
  80 端口有没有人应答（Windows runner 上有系统服务占着）。回环与 IP 字面量不再参与劫持判定。
- **唤醒计划一天可能发两次**：判断「今天发过没有」从按时刻改成按日期，
  时钟被 NTP 或手动回拨到触发点之前时不再重复发 WOL。
- **锁文件写错人**：抢锁失败的实例会把持有者的 PID 抹掉，导致诊断指错人、
  锁文件残留在盘上；现在拿到锁之后才写 PID。
- **网卡名带单引号会让关机前置准备静默失败**：拼 PowerShell 命令前先按 PS 规则转义。
- **`powercfg` 只认中文输出**：换成中英双语匹配，英文版 Windows 上不再读不到交流电源设置。
- **唤醒端口越界时错误看不懂**：`port` 不在 1-65535 时直接给出明确错误，
  而不是让 `OverflowError` 冒到调用方、计划里只留一条 warning。
- **设置项类型往返丢失**：字符串恰好是合法 JSON（`"123"` / `"true"` / `"null"`）
  读回来会变成 int / bool / None，现在统一按字符串存。
- **一条坏设置能让服务再也起不来**：`PUT /api/settings` 不校验类型，写进一个
  `{"interval": "abc"}` 就会落库，之后每次启动都在 `int()` 上抛异常——重启也一样，
  只能手改数据库。现在写入按白名单校验类型（写错报 400，且整批校验通过才落库），
  读取侧也兜住库里已有的脏值。
- **`power prep --fix` 在没有终端时崩栈**：双击快捷方式、计划任务或 stdin 被重定向时，
  那句确认读不到输入会抛 `EOFError`（Ctrl-C 则是 `KeyboardInterrupt`），整条调用栈
  直接冒到用户面前。现在按「取消」处理并明确说没做任何修改——读不到确认就等于没确认，
  绝不会顺手改注册表或 `powercfg`。
- **电源体检虚报「可自动修复」**：PowerShell 的 NetAdapter 模块不可用时，网卡唤醒那项
  标着可修，但它自己也写着「请手动…」，`--fix` 会落回同一条分支、什么也改不了。
  控制台的确认框和命令行的「将尝试自动修复」都据此把它算进待修清单，用户确认一圈
  却什么都没发生。
- **接口参数不再能拖垮服务**：`timeout` / `limit` / `port` 夹到合理区间，
  越界端口报 400 而不是 500，免得一个超长请求占住一条连接。

### 工程与分发

- 分发 zip 里补上 `CHANGELOG.md` 与 `CONTRIBUTING.md`——README 里它们都是相对链接，
  漏发就是死链；相应地，README 相对链接的校验从「只查 `doc/`」放宽到「所有相对路径」。
- `install.ps1` 的 zip 兜底地址修正（原来指向 raw 根路径，必然 404）；
  `install.sh` 补上没装 git 时的分支归档兜底。
- 镜像 `:latest` 只由正式版更新，预发布 tag 不再顶掉它；
  Release 前置校验加上与 CI 同款的覆盖率门禁；CI 的 Python 作业加超时上限。
- `.dockerignore` 补上 `utils/local_sensitive.txt`；扩展清单版本号与后端版本对齐、
  去掉用不到的 `tabs` 权限；控制台两个「立即检测」入口共用一把锁，连点不会再并发跑两轮。
- **分支保护的必过检查从 2 项扩到 8 项**：六个测试矩阵格（`Linux`/`Windows` ×
  Python 3.10/3.12/3.13）纳入必过。矩阵格的检查名因此带上 OS——原来同一版本在
  两个平台同名，windows 那格的红会被 ubuntu 那格的绿顶掉，不改名就拦不住。
  提 PR 的人从「扩展逻辑 + 凭据扫描」两道门禁，变成还要过六个格的测试。

## [2.0.0] - 2026-10-14

1.x 只解决「校园网断了自己重连」。2.0 把它做成「有电就联网、出门也能管」，
三件事都齐了：**无人值守**（系统级 service / plist 无需登录即启动、容器部署与
GHCR 多架构镜像、四平台单文件分发）、**远程开关机**（前置条件逐项体检并一键修、
唤醒计划按时间表发魔术包、macOS 无人登录也能关）、**多端接入**（PWA 加到主屏离线
可用、扫码配对免手抄令牌、Tailscale / RustDesk 向导）。

### 新增

- **扫码配对**（D2）：控制台生成 6 位一次性配对码（5 分钟过期、单次使用），
  手机打开 `#pair=码` 链接即换取正式会话，不用手抄令牌。
- **电源体检 `power prep`**（C1）：逐项检查远程开关机的前置条件——快速启动、
  魔术包唤醒、可唤醒设备、睡眠超时、远程桌面、自动登录、时间偏差、BIOS 来电自启
  （Linux/macOS 各有对应项）。能自动修的用 `--fix` 一键改，需提权的标「需人工」。
- **唤醒计划与设备清单**（C2）：按时间表（如 `mon-fri 07:30`）定时发唤醒包，
  每档每天最多一次；由监控进程顺带调度，不另起进程，单条失败只记事件不打断探活。
- **远程接入向导 `access --guide`**（C3）：装 Tailscale → 装 RustDesk →
  手机怎么点的三步指引，只检测不改网络配置；缺哪一步明确报出来并给各平台安装命令。
- **控制台「电源」页**（C4）：三层远程开机方案卡片（L1 来电自启+智能插座 /
  L2 常开跳板 WOL / L3 同网段 App）、体检结果表、唤醒计划与设备清单管理。
- **门户发现补 DNS 劫持判定**（C5）：域名被解析到 RFC 1918 私有段即判为门户劫持，
  内网 IP 作为门户线索加入候选。
- **控制台 PWA**（D1）：可安装到手机桌面；Service Worker 只缓存外壳，
  离线打开显示「服务不可达」而不是白屏，`/api/` 一律不缓存不兜底。
- **四平台单文件**（E1）：单文件产物从只有 Windows x64 扩到 Linux x64 / Linux arm64 /
  macOS arm64，后三个平台不必再自己装 Python。打 tag 前可用 `workflow_dispatch`
  先把四个平台各编一遍，不必等 Release 发出去才发现某个平台编不出来。
- **系统级服务文件**（E4）：`packaging/` 下新增 systemd 单元与 macOS LaunchDaemon，
  开机即连、不用等人登录——`bootstart install` 生成的一直是「登录后」语义，
  「有电就连」缺的正是这一层。
- **容器部署**（E2）：新增 `Dockerfile`、`docker-compose.yml` 与 `.dockerignore`。
  镜像以非 root 用户运行，`HEALTHCHECK` 打公开的 `/healthz`（用解释器自带的 urllib，
  不额外装 curl）；账号、事件与令牌都落在 `database/` 一个卷里。
  `.dockerignore` 把 `config.json` / `*.key` / `*.db` 挡在构建上下文外——
  `.gitignore` 挡得住提交，挡不住 `COPY`。
- **多架构镜像与 tailnet 组网**（E3）：打 tag 时构建 `linux/amd64` + `linux/arm64`
  推到 GHCR，普通 push 不构建镜像；另给 `docker-compose.tailscale.yml`，
  让控制台只在 tailnet 上可达，不必把 2268 映射到局域网。
- **首屏三大按钮 + 通知渠道预设**（D3+D4）：首屏加「关机 / 重启 / 立即检测」
  （拇指可达、带二次确认）；通知从「填一个 URL」升级为「选渠道 + 填 key」，
  内置 bark / Server酱 / 企业微信机器人 / 钉钉 / Telegram / ntfy 六种预设，
  并按掉线 / 恢复 / 电源 / 配对分级开关。

### 修复

- `notify`：refresh 改用 `getattr` 容错，最小假 Notifier 不再抛 `AttributeError`。
- **macOS 无人登录时关不掉机**：`power off` / `power reboot` 原先在 macOS 上一律走
  `osascript`，而它只在图形登录会话里可用——LaunchDaemon（无人登录）与 SSH 会话
  都拿不到 Apple Events，命令失败后表现为「点了关机，什么都没发生」，而那正是
  「无需登录」要解决的场景。现在无图形会话时回退到 `shutdown`；有图形会话时仍走
  `osascript`，保留「应用能正常退出」的优雅关机语义。

### 变更

- **深度优化 v2 阶段 A+B**：
  - A1 `openapi` 建父目录、README 归位；
  - A2 发布包补全，CI 加死链与凭据泄露闸门；
  - A3 README 补责任边界与三层远程开机降级表；
  - A4 修扩展请求被 403（同源豁免 + 显式令牌）；
  - A5 单实例锁改内核级文件锁，消除 TOCTOU 窗口；
  - B1 一键安装 `install.ps1` / `install.sh`；
  - B2 计划任务加固 + 中文权限判定；
  - B4 `bootstart verify` 自证「开机后确实跑过」；
  - B5 登录失败分类熔断（终端凭据 600 秒冷却）。

### 文档

- 新增《深度优化方案 v2》《资料汇编：设计思路与源码参考》；
  `doc/工作状态存档.md` 补阶段 C；新增 `CONTRIBUTING.md` 与规则投稿 issue 模板。

### 内部

- 覆盖率治理：补齐锁、日志、自启动、菜单、快捷键、加密、电源、权限、接入等模块的
  降级分支与跨平台分支用例，整体覆盖率 90% → 92%；随后给 `server.py` 补 37 条
  边界与失败分支用例（87% → 100%），CI 挂上 `--cov-fail-under=90` 门禁。
- 扩展侧补 `loginByFetch` / `encodePassword` / `classify` 的模块级注释，
  测试按「纯函数 / 探活与登录 / 状态与消息」分三组汇总。
- 提交前钩子、Dependabot、CodeQL。
- README 命令冒烟测试，防止文档与代码漂移。
- HTTPS 上下文收敛到 `tls.portal_context`：探活 / 门户 / 深澜 / 门户发现四处原先
  各建一份，`check_hostname` 与 `verify_mode` 的先后顺序只在一处把关。
- `cli.py` 按主题拆成 `cli/` 包（account / net / power / service / system），
  解析器与入口留在包内 `__init__`，各子命令的 `--help` 输出逐行不变；
  菜单与快捷方式共用的提权命令行构造收进 `shortcuts.elevate_command_line`。
- `packaging/` 下的服务文件加防漂移测试：启动参数必须逐字等于
  `bootstart.launch_args()`、注册目标必须是「机器起来」而非「用户登录」、
  编码变量在、换行为 LF（新增 `.gitattributes` 把 unit 文件钉死成 LF，
  免得 Windows 上 `core.autocrlf` 把 CR 带进 systemd 的值里）。
- 发布流程的冒烟从「跑一下 `--version`」加强成断言真的读到规则：
  冻结后最容易静默出错的不是代码而是数据，`profiles` 目录没打进去时
  程序会一声不响地变成「0 条规则」。
- compose 文件显式写了 `name:`：不写就取目录名当项目名，而本仓库目录名
  「校园网自连」消毒之后是空串，`docker compose up` 会直接报
  `project name must not be empty`。

## [1.6.0] - 2026-10-12

### 新增

- 单文件 EXE（Windows 免装 Python），并接进发布流程。

### 测试

- 冻结路径用例改成跨平台不变量，去掉对具体平台路径的断言。

## [1.5.0] - 2026-10-12

### 新增

- 控制台 API 契约（OpenAPI）与 `/api/v1` 别名。

### 文档

- README 补截图。

## [1.4.0] - 2026-10-12

### 变更

- 扩展优先把检测交给本地服务，减少扩展与 Python 两套实现的漂移；
  本地服务不可用时静默回落到扩展自己的登录。

## [1.3.0] - 2026-10-12

### 新增

- 桌面入口扩成覆盖全部功能的菜单，并补手机窄屏适配。

## [1.2.0] - 2026-10-12

### 新增

- 字段候选接入登录路径，补 fixture 回归集与版本更新提示。

## [1.1.0] - 2026-10-12

### 新增

- 规则层接入认证判定，并打通 fixture 投稿闭环。

## [1.0.0] - 2026-10-12

首个公开版本：电脑有电就自动连上校园网，掉线自动重连，并能远程决定开关机。

### 新增

- 登录、掉线自动重连、开机自启动的基础闭环；补齐无人值守能力。
- 浏览器扩展：GBK 门户解码 + 稳定性加固。
- 深澜 SRUN 认证，以及前端 toast 去重。
- 登录过程实时进度反馈 + 本地模拟门户，便于离线验收。
- 局域网控制台令牌认证、Wi-Fi 身份检查与凭据封装升级。
- 远程电源、桌面快捷方式与多端访问；远程开机信息与语义澄清。
- 一键配置，降低上手门槛。
- 端口预检：端口被占用时说人话，而不是让命令挂着。
- 门户适配规则层：新增一所学校只需加一条数据。

### 修复

- SRUN 加密与门户复核按真实 `Portal.js` 对齐；换表 Base64 之前误用标准表（等于没换）。
- `result:10` / `result:12` 被误判成登录成功。
- 302 不等于掉线——正常跳转曾被误判。
- 四处 HTTP 请求强制直连，不再读 `http_proxy`。
- `_pid_alive` 在超大 PID 上抛 `OverflowError`。
- 浏览器登录的两个致命缺陷；适配西北农林科大 `srun_portal_pc` 门户。
- 手动检测不再被残留的停止标志打断。
- 裁剪统计改存 `settings`，不再自喂事件表。
- 打包时排除本地日志文件。
- 修复计划任务注册的真实缺陷；`status` 如实说明当前启动项的语义。
- 收紧入口安全与并发边界；限制探活跳转并加强发布安全检查；
  完善校园网门户相对跳转发现；修复发布凭据扫描脚本。

### 测试

- 补 CLI 与浏览器登录测试，覆盖率 75% → 88%。
- 补平台相关分支测试，`crypto` 75% → 98%。
- 补 `/api/progress` 实时进度路由、西北农林科大回归、HTTP 测试服务生命周期、
  探活线程正常退出等用例。

### 变更

- 公开前脱敏，并把桌面快捷方式合并为一个入口。
- 测试里的真实学号换成假学号；`config.json` 规则锚定到仓库根。
- 分离开发依赖并统一发布扫描。

### 文档

- 开源发布准备（README / LICENSE / CI / Release）、自测指南、
  端口冲突应对说明，以及「提交目标为 `origin/main`」的入库约定。

[未发布]: https://github.com/enmingxiong821-cloud/campusnet/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/enmingxiong821-cloud/campusnet/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.6.0...v2.0.0
[1.6.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/enmingxiong821-cloud/campusnet/releases/tag/v1.0.0
