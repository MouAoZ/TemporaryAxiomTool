# 已批准陈述注册库数据库

这个目录保存 `TemporaryAxiomTool` 的外部注册库。

它不是宿主数学库的一部分，而是由脚本维护的离线数据库层，用来支持：

- 可跳过定理的批准登记
- statement hash 审计
- 历史事件回滚
- review note / warning / alert
- 历史归档

统一管理入口：

- [../scripts/manage_approved_statement_registry.py](../scripts/manage_approved_statement_registry.py)

## 目录结构

- `current/`: 当前批准快照，按 chapter/section 分片
- `history/`: live 历史事件
- `archive/`: 归档后的历史事件包, 可以手动删除

当前仓库只保留空目录模板，不再附带任何示例 theorem 数据。

## 当前快照文件命名

格式：

- `approved_statement_registry.chapter_<CC>.section_<SS>.json`

示例：

- `approved_statement_registry.chapter_03.section_02.json`

对应 Lean 侧自动生成文件位于：

- [../TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean](../TemporaryAxiomTool/ApprovedStatementRegistry/Generated.lean)
- `../TemporaryAxiomTool/ApprovedStatementRegistry/Shards/` 下的内部分片模块

说明：

- `Generated.lean` 是统一聚合入口
- `Shards/` 中的文件按 chapter/section 拆分，由脚本自动生成；当 current 快照为空时，该目录可能暂时不存在

## `current/` 顶层结构

每个 shard 文件结构如下：

```json
{
  "schema_version": 1,
  "shard_id": "chapter_03.section_02",
  "chapter": 3,
  "section": 2,
  "entries": []
}
```

顶层字段含义：

- `schema_version`: 数据格式版本
- `shard_id`: 分片编号
- `chapter`: chapter 编号
- `section`: section 编号
- `entries`: 当前分片的已批准声明列表

## `entries` 条目字段

每个条目表示一个已批准声明，主要字段如下：

- `decl_name`: Lean 全限定声明名
- `module`: 声明所属 Lean 模块
- `statement_pretty`: 供人工审阅的可读陈述
- `statement_hash`: elaborated statement 的稳定哈希值，当前以字符串形式保存
- `needs_human_review`: 当前是否存在待人工关注事项
- `review_notes`: review note 列表
- `review_status`: 聚合后的风险等级，取值为 `clear`、`comment`、`warning`、`alert`
- `approved_by`: 最近一次执行 `approve` 的操作者
- `approval_reason`: 最近一次 `approve` 的说明
- `created_at`: 首次进入注册库的时间
- `updated_at`: 最近一次修改时间
- `approved_at`: 最近一次批准时间

注意：

- `chapter`、`section`、`shard_id` 只保留在 shard 顶层
- `source_file` 不再保存，因为 `module` 已足够标识来源

## `review_notes`

`review_notes` 是数组，每个元素表示一次 review commit：

- `event_id`: 对应历史事件编号
- `timestamp`: 时间戳
- `author`: 记录者
- `severity`: `comment` / `warning` / `alert`
- `message`: 备注内容

## `history/` 事件

`history/` 下每个 JSON 文件都是一个不可变事件，文件名通常即 `event_id`。

顶层字段：

- `schema_version`
- `event_id`
- `action`
- `timestamp`
- `author`
- `reason`
- `changes`
- `rollback_of`

其中 `action` 当前支持：

- `approve`
- `commit`
- `prune`
- `rollback`

### `changes` 字段

`changes` 是数组，每个元素描述一个声明在该事件中的变化：

- `decl_name`
- `kind`
- `before_shard`
- `after_shard`
- `before`
- `after`

`before_shard` / `after_shard` 用于在不把 shard 信息内嵌到 entry 的前提下，仍然支持稳定回滚。

## `archive/` 归档包

`archive/` 下每个 JSON 文件都是一次历史归档操作生成的事件包。

顶层字段：

- `schema_version`
- `archive_id`
- `created_at`
- `author`
- `reason`
- `mode`
- `decl_filter`
- `source_event_count`
- `source_event_ids`
- `events`

归档后：

- live `history/` 可以被清空或压缩
- 旧事件仍可通过 `history --include-archive` 查看
- `rollback --event-id <EVENT_ID>` 仍可直接从归档包中查找并执行回滚

## 使用建议

- 不手工编辑这里的 JSON，优先通过脚本维护
- 合并冲突时优先保留历史事件，再执行 `generate`
- 定期运行 `audit` 确认 hash 没有漂移
- 若 live history 过大，可使用 `history --archive ... --execute` 做安全归档
