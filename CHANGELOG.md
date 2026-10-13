# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，
条目按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的方式归类。

变更条目取自提交记录。**未发布**一节是 `main` 上已有、尚未打 tag 的内容；
这里只记「用户能感知到什么变了」，实现细节看对应提交。

## 未发布

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
- **首屏三大按钮 + 通知渠道预设**（D3+D4）：首屏加「关机 / 重启 / 立即检测」
  （拇指可达、带二次确认）；通知从「填一个 URL」升级为「选渠道 + 填 key」，
  内置 bark / Server酱 / 企业微信机器人 / 钉钉 / Telegram / ntfy 六种预设，
  并按掉线 / 恢复 / 电源 / 配对分级开关。

### 修复

- `notify`：refresh 改用 `getattr` 容错，最小假 Notifier 不再抛 `AttributeError`。

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

[未发布]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/enmingxiong821-cloud/campusnet/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/enmingxiong821-cloud/campusnet/releases/tag/v1.0.0
