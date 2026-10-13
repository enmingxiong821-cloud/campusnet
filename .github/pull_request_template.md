## 这次改了什么

<!-- 一两句话。修 bug 的话写清「什么情况下会出问题」，而不是只描述改法。 -->

## 怎么验证的

<!-- 贴命令和结果。文档里出现的命令必须自己跑过。 -->

- [ ] `python -m pytest project/backend/tests -q` 全绿
- [ ] 动到扩展时：`node project/frontend/extension/test-background.js` 全绿

## 自查

- [ ] 没把 `config.json`、`*.api-token`、`*.key`、`database/*.db`、`_tmp/`、
      `交付文件/`、`*.log` 加进提交（红线清单见 [CONTRIBUTING.md](../CONTRIBUTING.md)）
- [ ] 没引入运行时第三方依赖——后端保持纯标准库
- [ ] 没改协议层已实测逻辑（`srun.py` 的 challenge / HMAC-MD5 / xxtea、
      `portal.py` 的表单提交顺序）：改这些没法在本地对着真实门户验证
- [ ] 新测试在 ubuntu 与 windows 上都过，不写平台相关的绝对路径断言

## 关联

<!-- Closes #123 -->
