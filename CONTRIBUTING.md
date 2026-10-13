# 贡献指南

校园网自连：电脑有电就自动连校园网，并支持远程开关机、多端使用。后端纯 Python
标准库，运行时零第三方依赖。

## 本地跑起来

```bash
python utils/run.py doctor       # 一键自检运行环境
python utils/run.py serve        # 启动控制台（http://127.0.0.1:2268）
python utils/run.py console      # 打开控制台（含局域网访问地址与令牌）
```

## 测试

```bash
# 后端全量（CI 同款命令，只跑 backend/tests）
python -m pytest project/backend/tests -q

# 带覆盖率，门禁 90%（CI 用的就是这条；本地 pre-commit 不跑）
python -m pytest project/backend/tests -q \
  --cov=campusnet --cov-report=term-missing --cov-fail-under=90

# 浏览器扩展
node --check project/frontend/extension/background.js
node project/frontend/extension/test-background.js
```

> 别用裸 `python -m pytest -q`：本机 `_tmp/` 下放有参考项目源码，裸命令会把它们的
> 测试也收集进来直接报错。CI 与上面的命令都用 `project/backend/tests` 显式限定范围。

## 提交前检查

建议装一次提交钩子（复用分发扫描，本地拦敏感串与尾随空格）：

```bash
cp utils/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

也可全局启用：`git config core.hooksPath .githooks`，再把 `utils/pre-commit` 放到
`.githooks/pre-commit`。钩子命中时只报文件名与规则名，不打印凭据原文。

## 入库红线

以下内容保持本地，绝不 `git add`：

- `config.json`、`*.api-token`、`*.key`、`.campusnet_obf.key`
- `database/*.db`、`database/backup/`
- `_tmp/`、`交付文件/`
- `.coverage`、`*.log`、`nul`

本地私有的内网地址 / SSID 这类标识放进 `utils/local_sensitive.txt`（不入库），
分发扫描会据此拦截。

## 提交规范

- Conventional Commits，中文描述，附阶段项号（如 `feat(pair): 扫码配对（D2）`）。
- 一阶段一提交（或几个小提交），不攒大 PR。
- 只推 `origin/main`，不新建远端。

## 如何为本校提规则

新增一所学校从「改代码」变成「加一条数据」：门户规则是一份 JSON
（`project/backend/campusnet/profiles/*.json`，文件名数字前缀即优先级）。投稿链路：

1. 在**未认证**状态下抓取门户登录页：
   ```bash
   python utils/run.py portal-report --url http://<门户地址>/eportal/portal/login
   ```
   它会脱敏成 fixture（默认写 `_tmp/portal-report.html`），再据此识别厂商与字段。
2. 把 fixture 存到 `project/backend/tests/fixtures/`，用回放离线复现：
   ```bash
   python utils/run.py portal-report --replay <fixture 路径>
   ```
3. 加一条规则 JSON 并校验：
   ```bash
   python utils/run.py profiles --validate
   ```
4. 提 PR 时**贴脱敏 fixture + 规则 JSON**，不要贴真实学号/口令/内网地址。

本地私有的内网标识放进 `utils/local_sensitive.txt`（不入库），分发扫描据此拦截。

## 提 issue / PR

- 报故障时贴 `python utils/run.py doctor --json` 的输出。
- 提规则按上面的「如何为本校提规则」走，优先投稿 fixture 而不是只描述现象。

## 目录速览

```
project/backend/campusnet/   后端（纯标准库）
project/backend/tests/       测试
project/frontend/            Web 控制台 + 浏览器扩展
utils/run.py                 统一入口（规避中文路径）
database/schema.sql          表结构
```
