# AGENTS.md

## 提交目标

本项目所有提交与推送一律指向：

- 远端：`origin` → `https://github.com/enmingxiong821-cloud/campusnet.git`
- 分支：`main`（上游 `origin/main`）

即 `git push origin main`。不要新建其他远端，不要推到别的仓库。

## 入库红线

以下内容保持本地，绝不 `git add`：

- `config.json`、`*.api-token`、`*.key`、`.campusnet_obf.key`
- `database/*.db`、`database/backup/`
- `_tmp/`、`交付文件/`
- `.coverage`、`*.log`、`nul`

## 测试

```
python -m pytest -q
```
