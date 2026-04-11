# 下游项目升级说明

这份说明面向从旧版 `PreparedSession` / managed-attr 工作流迁移到当前架构的维护者。

当前架构的核心对象是：

- `TemporaryAxiomTool`
- `TemporaryAxiomTool.TheoremRegistry.Shards.<Module>`
- `.temporary_axiom_session/session.json`
- `.temporary_axiom_registry/proved_theorems.json`

## 1. 下游需要接入什么

如果某个项目模块需要被工具跟踪，只需要直接写：

```lean
import TemporaryAxiomTool
```

第一次 `prepare` 后，工具会给它补齐稳定 shard import：

```lean
import TemporaryAxiomTool.TheoremRegistry.Shards.<CurrentModule>
```

这条 import 会保留在源码里。

## 2. 下游调度器应如何读数据

新的调度顺序仍然是：

1. `prepare`
2. proving agent 工作
3. 外部 verifier / comparator 读取 `.temporary_axiom_session/session.json`
4. `cleanup`

推荐读取的字段：

- `freeze.target`
- `freeze.permitted_axioms_list`
- 如有需要再读 `freeze.permitted_axioms`

如果需要“完整 permitted 集合加 target 名字”，读取：

- `freeze.permitted_axioms_list`
- `freeze.target.decl_name`

## 3. 迁移时应更新的假设

迁移到当前版本后，下游应采用这些新假设：

- 跟踪范围由“是否直接 `import TemporaryAxiomTool`”决定
- tracked modules 会保留稳定 shard import
- `.temporary_axiom_registry/proved_theorems.json` 是本地持久状态
- `cleanup` 负责结束当前活动 session，并删除临时 shard import
- 会话冻结信息统一从 `.temporary_axiom_session/session.json` 读取

## 4. 旧版引用需要替换什么

如果下游仓库 vendored 过旧版工作流，建议清理这些旧引用：

- `TemporaryAxiomTool.PreparedSession`
- `PreparedSession.Target`
- `PreparedSession.Permitted`
- `cleanup.edits`
- 任何“cleanup 会回滚 managed attr / managed import”的逻辑

当前版本不再要求下游依赖这套旧运行时对象。

## 5. 升级后建议检查的文件

应使用当前仓库里的这些文件：

- [../TemporaryAxiomTool.lean](../TemporaryAxiomTool.lean)
- [../TemporaryAxiomTool/](../TemporaryAxiomTool/)
- [../scripts/temporary_axiom_session.py](../scripts/temporary_axiom_session.py)
- [../scripts/session_tool/](../scripts/session_tool/)
- [temporary_axiom.md](./temporary_axiom.md)
- [../README.md](../README.md)

同时允许这些运行期或本地产物存在：

- `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean`
- `.temporary_axiom_registry/proved_theorems.json`
- `.temporary_axiom_session/session.json`

## 6. 建议回归测试

```bash
python3 -m py_compile \
  scripts/temporary_axiom_session.py \
  scripts/session_tool/*.py

lake build TemporaryAxiomTool

python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target:goal
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.Target
```

通过标准：

- `prepare` 成功写出 `.temporary_axiom_session/session.json`
- 活动 session 中，permitted theorem 能被接受，target theorem 仍按普通 theorem elaboration
- `cleanup` 成功结束活动 session
- tracked module 的稳定 shard import 保留
- `.temporary_axiom_registry/proved_theorems.json` 持续存在
