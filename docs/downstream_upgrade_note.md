# 下游项目升级说明

这份说明只面向旧版 registry 工作流的维护者，说明下游如何切换到当前版本。

## 1. 迁移结果

新版只保留：

- `prepare`
- `cleanup`
- 单一 `.temporary_axiom_session/session.json`

## 2. 调度侧接入

外层调度器按下面顺序接入：

1. `prepare`
2. proving agent 工作
3. 外部 verifier / comparator 读取 `.temporary_axiom_session/session.json` 的 `freeze` 字段
4. `cleanup`

## 3. 旧版内容里哪些可以直接删除

如果你的下游项目以前是按旧版 `main` 分支把工具直接 vendored 进仓库，那么下面这些内容属于旧 registry 工作流专用文件；切到当前 session 工作流后，可以直接安全删除：

- [../TemporaryAxiomTool/ApprovedStatementRegistry.lean](../TemporaryAxiomTool/ApprovedStatementRegistry.lean)
- 目录 `TemporaryAxiomTool/ApprovedStatementRegistry/`
- 目录 `approved_statement_registry_db/`
- [../scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)
- 目录 `scripts/registry_tool/`

如果你的下游项目没有额外依赖这些文件做发布记录，下面这些仓库级元数据也可以一起删除：

- [../CHANGELOG.md](../CHANGELOG.md)
- `VERSION`

这些内容之所以可以删，是因为当前版本已经不再维护批准注册库、history/current 数据库、registry 生成入口或对应的审计命令。

## 4. 哪些内容应当替换而不是删除

- [../TemporaryAxiomTool.lean](../TemporaryAxiomTool.lean)
- [../TemporaryAxiomTool/](../TemporaryAxiomTool/)
- [../scripts/temporary_axiom_session.py](../scripts/temporary_axiom_session.py)
- [../scripts/session_tool/](../scripts/session_tool/)
- [temporary_axiom.md](./temporary_axiom.md)
- [../README.md](../README.md)

如果下游项目已经有自己的 CI 文件，通常不是整份删除，而是把其中旧 registry 相关步骤替换掉。

## 5. 升级后应检查的旧引用

完成文件替换后，建议在下游项目里全局搜索并清掉这些旧引用：

- `TemporaryAxiomTool.ApprovedStatementRegistry`
- `manage_approved_statement_registry.py`
- `approved_statement_registry_db`
- `registry_tool`
- `audit-temporary-axioms`
- `manage_approved_statement_registry.py approve`
- `manage_approved_statement_registry.py report`
- `manage_approved_statement_registry.py commit`

CI 里最常见的旧残留，是仍在调用旧的 registry audit 命令；这些都应改成当前 session 工作流自己的调度流程。

## 6. 最小回归测试

```bash
python3 -m py_compile \
  scripts/temporary_axiom_session.py \
  scripts/session_tool/*.py

lake build TemporaryAxiomTool

lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target.goal
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.Target
```

通过标准：

- `prepare` 成功写入 `.temporary_axiom_session/session.json`
- 构建后只有 permitted declarations 被接受为 `@[temporary_axiom]`
- `session.json` 的 `freeze` 字段正确包含 target 与 permitted axioms
- `cleanup` 后 managed import 和 managed attribute 被移除
