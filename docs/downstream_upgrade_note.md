# 下游项目升级说明

这份说明面向从旧版 `PreparedSession` / managed-attr 工作流迁移到当前 `TheoremRegistry + local proved DB + module shards` 架构的维护者。

## 1. 核心变化

新版和旧版最关键的差别有四个：

1. 不再由脚本批量给源码插入 managed `@[temporary_axiom]`。
2. 不再使用单一 `PreparedSession.Target` / `PreparedSession.Permitted.*` runtime。
3. 改为每个 tracked module 稳定 import 一份 shard：
   `TemporaryAxiomTool.TheoremRegistry.Shards.<Module>`
4. 新增本地持久 proved theorem 数据库：
   `.temporary_axiom_registry/proved_theorems.json`

运行时也从“attribute 驱动的 axiomization”改成了“活动 session 中对已登记 theorem 自动处理”：

- target：验 theorem-side hash 后正常 elaboration
- persistent proved / session temporary：验 theorem-side hash 后直接注册同名 axiom
- other theorem：保持正常 elaboration

## 2. 调度侧接入

外层调度器的顺序仍然是：

1. `prepare`
2. proving agent 工作
3. 外部 verifier / comparator 读取 `.temporary_axiom_session/session.json` 的 `freeze`
4. `cleanup`

但 `cleanup` 的语义已经变了：

- 它会增量登记新证明成功的 theorem
- 它不会再删除 shard import
- 它不会再回滚源码 attribute

## 3. 下游模块需要做什么

如果某个项目模块需要被工具跟踪，要求很简单：

```lean
import TemporaryAxiomTool
```

第一次 `prepare` 后，工具会为所有 tracked modules 补齐稳定 shard import；对某个具体模块而言，会在它的 import 头加入：

```lean
import TemporaryAxiomTool.TheoremRegistry.Shards.<CurrentModule>
```

这条 import 之后会稳定保留。下游项目不应再把它当成一次性 session 残留去删除。

## 4. 旧版内容里哪些可以删除

如果你的下游仓库 vendored 过旧版 `PreparedSession`/managed-attr 工作流，可以删除旧引用和旧假设：

- `TemporaryAxiomTool.PreparedSession`
- `TemporaryAxiomTool.PreparedSession.Target`
- `TemporaryAxiomTool.PreparedSession.Permitted`
- 任何“cleanup 会回滚 managed import / managed attr”的下游逻辑
- 任何依赖 `session.json.cleanup` edit log 的下游逻辑

如果下游还有额外脚本专门处理“上一次 session 留下的 managed `@[temporary_axiom]`”，这类脚本也应一并删掉，因为当前版本不再生成这些 managed 标签。

## 5. 哪些内容应当替换而不是删除

- [../TemporaryAxiomTool.lean](../TemporaryAxiomTool.lean)
- [../TemporaryAxiomTool/](../TemporaryAxiomTool/)
- [../scripts/temporary_axiom_session.py](../scripts/temporary_axiom_session.py)
- [../scripts/session_tool/](../scripts/session_tool/)
- [temporary_axiom.md](./temporary_axiom.md)
- [../README.md](../README.md)

尤其注意：

- 新版需要把 `TemporaryAxiomTool/TheoremRegistry/Shards/**/*.lean` 当作真实生成物维护。
- 新版需要允许 `.temporary_axiom_registry/proved_theorems.json` 在本地存在。

## 6. 升级后应检查的旧引用

完成替换后，建议在下游项目里全局搜索并移除这些旧引用：

- `TemporaryAxiomTool.PreparedSession`
- `PreparedSession.Target`
- `PreparedSession.Permitted`
- `managed temporary_axiom`
- `cleanup.edits`
- 任何“删除 managed import / managed attr”的自定义清理逻辑

## 7. 推荐回归测试

```bash
python3 -m py_compile \
  scripts/temporary_axiom_session.py \
  scripts/session_tool/*.py

lake build TemporaryAxiomTool

python3 scripts/temporary_axiom_session.py prepare \
  --target TemporaryAxiomTool.TestFixture.Target.goal
lake build TemporaryAxiomTool.TestFixture.Target
python3 scripts/temporary_axiom_session.py cleanup
lake build TemporaryAxiomTool.TestFixture.Target
```

通过标准：

- `prepare` 成功写入 `.temporary_axiom_session/session.json`
- 活动 session 中，已登记 theorem 会被自动接受；未登记 theorem 不会被静默跳过
- `cleanup` 后活动 session 被撤销，但 shard import 仍保留
- `.temporary_axiom_registry/proved_theorems.json` 会持续存在并累积已证明 theorem
