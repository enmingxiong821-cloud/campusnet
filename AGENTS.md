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
- `_tmp/`、`交付文件/`、`协作记忆/`
- `.coverage`、`*.log`、`nul`

## 测试

```
python -m pytest project/backend/tests -q
```

必须带上路径：本机 `_tmp/` 下放有参考项目源码，裸 `python -m pytest -q` 会把它们的
测试一并收集进来直接报错（与 `CONTRIBUTING.md` 的说明一致）。
