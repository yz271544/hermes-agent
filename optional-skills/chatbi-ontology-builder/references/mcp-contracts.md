# MCP 与文档能力约定

工具名随 Hermes 安装而异。**按能力发现工具，经项目配置绑定**；不要假设服务器就叫
`doris-mcp` 或 `pg-ripple-mcp`。端点与凭证放在 Skill bundle 之外。

## Doris / GBase 8a 元数据能力

活接口应能：列 catalog/database/表，描述列及 Doris 键/聚合角色，取授权视图定义，
跑有界只读元数据/profile 查询。

优先的高层工具形如 `list_tables`、`describe_table`、`get_table_ddl`、`profile_columns`、
`execute_readonly_sql`。只暴露通用 SQL 时：只用元数据视图（只读）、标识符全限定、
显式行/时间限制。生成期间拒绝 DDL、DML、多语句 SQL、对仓表的无限制 `SELECT *`。

记录快照导出时间、环境、catalog 范围、checksum；缺失时标新鲜度未知。

没有合适 MCP 时通过 `terminal` 运行 `scripts/extract_database.py`。统一证据格式不等于
SQL 方言或元数据字段完全相同：Doris 的 catalog/键模型/聚合角色与 GBase 的分布等信息
优先保留原生 DDL；解析不足时保留待核实，不臆造 MySQL FK、索引或事务语义。
GBase 8a 不等于 GBase 8s。PyMySQL 连接路径需在具体 8a 版本与认证配置上先验收。

## 文档能力

接受上传或连接器可达的 PDF、DOCX、表格、Markdown、文本、图片/OCR。每条断言保留：
来源 ID 与定位符、原文语言、抽取置信度、归一化断言、受影响资源、评审状态。

## 仓库能力

本地只读 Git checkout 优先；远端可通过已有授权连接器取得。固定 commit 后读取 DDL、
ORM、MyBatis、SQL/ETL/dbt、报表配置；保留路径/行号和未解析模板。
`scripts/extract_repository.py` 提供源码块与静态 SQL AST；模型判断语义，不执行源码或迁移。

## 本体/SPARQL 能力

优先只读工具形如 `query_sparql`、`list_graphs`、`describe_resource`、`validate_graph`；
受控写形如 `stage_graph`、`promote_graph`、`rollback_graph`。

只有 pg-ripple HTTP 或通用 PostgreSQL MCP 时，可用适配器包装。IRI/图名参数化。
绝不在 prompt、日志、报告、Turtle 中暴露 token。

## PostgreSQL 18 + pg-ripple 能力

该连接服务语义控制面，不是 Doris/GBase 分析面。用途：扩展/函数发现、named graph 发布、
SPARQL 回归、SHACL 校验（若支持）、可选向量检索、授权后的租户检查。

不要把 `_pg_ripple` 内部表当应用契约查询或修改；用公开函数/视图或本体 MCP。
仓库元数据快照是实现证据，不是稳定 API。

起草 bundle 不需要该连接；但声称 bundle 在目标构建上可加载、行为正确之前必须有它。

## 运行时推理链

```text
question
  -> SKOS/向量候选概念
  -> SPARQL 确认的概念与关系
  -> 指标/场景语义
  -> confirmed 数据能力与连接契约
  -> 策略检查
  -> 目标引擎（Doris/GBase 8a）的 SQL 计划与授权执行
```

把逻辑角色映射到安装特定的 MCP 工具名，写在外部配置里。
