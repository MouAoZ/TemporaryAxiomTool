# 已批准陈述注册库数据库

这个目录是 `TemporaryAxiom` 工作流使用的外部数据库。
它不属于形式化数学库本体，而是一个由脚本维护的注册与审计层。

统一管理入口：

- [scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)

正常工作流中不要手工编辑这里的 JSON 文件。应优先使用管理脚本，以保证：

- `current/` 与 `history/` 保持一致
- Lean 侧自动生成的注册表与数据库同步
- 审计、回滚与人工复核记录完整可追踪

## 目录结构

- `current/`: 当前已批准陈述快照，按书籍的 chapter/section 分片
- `history/`: 追加式历史事件日志，记录 `approve`、`commit`、`prune`、`rollback`

## 分片命名规则

当前快照文件名格式：

- `approved_statement_registry.chapter_<CC>.section_<SS>.json`

示例：

- `approved_statement_registry.chapter_00.section_02.json`

对应生成的 Lean 分片文件通常为：

- [TestProject3/ApprovedStatementRegistry/Shards/ApprovedStatementRegistry_Chapter00_Section02.lean](../TestProject3/ApprovedStatementRegistry/Shards/ApprovedStatementRegistry_Chapter00_Section02.lean)

其中：

- `<CC>` 是两位 chapter 编号
- `<SS>` 是两位 section 编号

## `current/` 快照格式

每个分片文件都是一个 JSON 对象，顶层字段如下：

- `schema_version`: 数据格式版本号
- `shard_id`: 分片编号，格式为 `chapter_<CC>.section_<SS>`
- `chapter`: chapter 编号，整数
- `section`: section 编号，整数
- `entries`: 当前分片包含的已批准定理列表

示例骨架：

```json
{
  "schema_version": 1,
  "shard_id": "chapter_00.section_02",
  "chapter": 0,
  "section": 2,
  "entries": []
}
```

### `entries` 条目格式

`entries` 中每个元素对应一个已批准陈述。当前实现中主要字段如下：

- `decl_name`: Lean 全限定声明名，例如 `TestProject3.SectionMainTheorem_2`
- `module`: 声明所在 Lean 模块，例如 `TestProject3.Section2`
- `statement_pretty`: 便于人工审阅的可读陈述
- 当前按 UTF-8 直接保存可读字符，不再使用 `\uXXXX` 转义
- `statement_hash`: 当前 elaborated statement 的稳定哈希值，当前实现中以字符串形式存储，例如 `"3056373314800698359"`
- `needs_human_review`: 是否存在 review note；若存在则为 `true`
- `review_notes`: 审核备注列表
- `review_status`: 根据 `review_notes` 聚合出的当前风险等级，当前可取值为 `clear`、`comment`、`warning`、`alert`
- `approved_by`: 最近一次执行 `approve` 的操作者
- `approval_reason`: 最近一次 `approve` 的原因说明
- `created_at`: 首次进入注册库的 UTC 时间，格式为 `YYYY-MM-DDTHH:MM:SSZ`
- `updated_at`: 最近一次被修改的 UTC 时间
- `approved_at`: 最近一次被 `approve` 的 UTC 时间

注意：

- `chapter`、`section`、`shard_id` 只出现在 shard 顶层，不再在每个 entry 中重复保存
- `module` 已足以标识定理来源的 Lean 模块，因此不再额外保存 `source_file`
- entry 内部字段顺序固定为“声明标识、陈述信息、审核信息、批准信息、时间信息”

## `review_notes` 格式

`review_notes` 是数组。每个元素代表一次 review commit，字段如下：

- `event_id`: 对应历史事件编号
- `timestamp`: 记录时间，UTC 格式 `YYYY-MM-DDTHH:MM:SSZ`
- `author`: 备注作者
- `severity`: 风险级别，可取 `comment`、`warning`、`alert`
- `message`: 备注正文

## `history/` 事件格式

`history/` 目录中的每个文件都是一个不可变事件，文件名通常就是其 `event_id`：

- `20260330T084637Z_approve_bbee340b.json`

事件顶层字段如下：

- `schema_version`: 数据格式版本号
- `event_id`: 事件编号
- `action`: 事件动作类型，目前为 `approve`、`commit`、`prune`、`rollback`
- `timestamp`: 事件发生时间
- `author`: 事件操作者
- `reason`: 本次操作的原因说明
- `changes`: 本次事件涉及的声明变更列表
- `rollback_of`: 仅在 `rollback` 事件中出现，表示被回滚的原事件编号

### `changes` 条目格式

`changes` 中每个元素表示一个声明在本次事件中的变化，字段如下：

- `decl_name`: 被修改的声明名
- `kind`: 变更类型，例如 `added`、`updated`、`annotated`、`removed`、`rolled_back`
- `before_shard`: 变更前所在 shard 的定位信息；若原先不存在则省略
- `after_shard`: 变更后所在 shard 的定位信息；若变更后不存在则省略
- `before`: 变更前的完整条目；若原先不存在则为 `null`
- `after`: 变更后的完整条目；若该条目被删除则为 `null`

其中 `before_shard` 和 `after_shard` 都是如下结构：

```json
{
  "shard_id": "chapter_00.section_02",
  "chapter": 0,
  "section": 2
}
```

这使得 `rollback` 可以在 entry 本体不保存 shard 信息的前提下，仍然可靠地做反向重放。

## 使用建议

- 日常维护只通过 [scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py) 操作
- 不在冲突状态下手工拼接 `current/` 和 `history/` 的 JSON
- 合并冲突后优先保留双方历史事件，再运行 `generate`
- 若需要检查数据库与 Lean 声明是否一致，运行 `audit`
- 若需要移除某个已批准定理，运行 `prune`
- 若需要撤销一次误操作，先用 `history` 找到事件，再执行 `rollback`
