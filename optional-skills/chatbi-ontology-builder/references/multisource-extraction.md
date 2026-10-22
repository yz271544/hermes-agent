# 多源抽取与本体合成契约

## 1. 运行工作区与统一证据

每次采集使用新的输出文件/目录，已有文件不覆盖。维护既有本体时允许复用稳定 IRI、
已审核语义与历史基线；必须刷新本次受影响来源并记录版本差异，不复制旧结果冒充新采集。
建议受控工作区：`evidence/`（原始证据）、`decisions.json`、`review/`（未决项/合并台账）、
`bundle/`（可发布本体）。工作区不等于 skill 安装目录，原始证据不默认进入发布图。

统一采集 envelope：

```json
{
  "evidence_version": "1.0",
  "source_id": "warehouse-a",
  "source_type": "database",
  "extracted_at": "<UTC>",
  "identity": {},
  "coverage": {"complete": true, "issues": []},
  "blocks": [{"block_id": "<stable ID>", "block_type": "table_metadata", "locator": {}, "content": {}}]
}
```

`source_id` 在同一工作区内唯一；文档自动来源 ID 含显式文件序号，变更输入列表时不要用它作业务 IRI。
`block_id` 由来源 ID、定位符、内容的哈希生成。文件来源保留原始 checksum；数据库证据保留
查询时间、数据库/引擎身份、限定表和覆盖报告，其证据文件自身 SHA256 由合成器写入 manifest。
多表顺序采集不保证跨表事务一致性快照；涉及结构变更窗口时另行协调并重采受影响对象。
采样记录过滤参数摘要，不保存实际参数值。源码/文档包含业务敏感信息时按项目数据权限保存；
内置凭证文本脱敏只是辅助，不能保证任意秘密格式均已识别，不把原始证据当公开交付物。

脚本返回码：0=该采集步骤完成（仍读 coverage），1=错误，2=覆盖不全/待核实。
文档 Office/PDF 的已知图像/版面限制写 coverage，文本解析成功也不能宣称所有视觉语义已抽取。

## 2. 数据库：Doris 与 GBase 8a

通过已有元数据 MCP 或 `extract_database.py --config <json> --output <new.json>` 采集。
外部配置样例在 `deployment/doris.example.json` 与 `deployment/gbase8a.example.json`。
配置必须显式提供引擎、host/port、database、只读用户、password_env、表清单，不枚举全库作为默认范围。
只读由数据库账号权限保证，不仅依赖脚本检查。MCP 路径也必须满足相同范围和证据契约。

共同采集：限定表的 `information_schema.TABLES/COLUMNS`、原生类型/可空性/注释/序号、
列元数据公开的键角色、`SHOW CREATE TABLE`。不因视图无 FK 就断言业务无关联。
此实现不额外查询 FK/index 系统表；有需求时用对应版本公开接口补采，并单独记录覆盖。
`SHOW CREATE TABLE` 不可用时记录问题并返回 2，不把 DDL 缺失说成已完整采集。

| 项目 | Doris | GBase 8a |
|---|---|---|
| 协议路径 | PyMySQL/MySQL 协议连接 FE 查询端口 | PyMySQL 协议路径，须先验收实际版本/认证与集群入口 |
| catalog | 可显式 SWITCH，默认服务端当前 catalog | 不接受 Doris catalog 配置 |
| 键与分布 | 保留键模型、聚合、分区/分桶等原生 DDL | 保留版本对应分布/复制、分区等原生 DDL |
| 超时 | 设置会话 query_timeout + 客户端读写超时 | 客户端超时；采样另要求 DBA/网关验证服务端预算 |
| 类型 | 保存 native type，另经评审映射 xsd | 同左；不按 MySQL 类型名称直接假定语义 |

参考官方 [GBase 8a 常用命令](https://www.gbase.cn/community/post/3650)、
[Doris 系统表](https://doris.apache.org/docs/3.x/admin-manual/system-tables/overview/)。
协议可连接不证明元数据/事务/认证/TLS 全兼容；首轮验收用 1 张已知表，不猜生产端口。

### 有界采样

默认 `sampling.enabled=false`。开启时显式列出每表 columns，必要时给时间/分区过滤，
只支持结构化的 `=、>=、>、<=、<` 单列谓词，值参数化；标识符对当前元数据验证后引用。
默认 20 行、最多 1000 行；不排序随机、不做全表 COUNT/DISTINCT，不接受任意 SQL 片段。
保存样本行数、样本空值/不同值/最大重复次数，**不是总体空值率、全局基数或主键证明**。
只有列入 `expose_values[table]` 的列才保存最多 1000 字符的值；其他列仅保存无具体值统计。
expose_values 是明确数据范围选择，仍需按用户授权与敏感数据策略配置；不把样本值写 TTL。
SQL 模板中保留谓词结构，参数只存摘要。前 N 行不是随机样本；达到上限时标记 limit_reached。
GBase 采样要求 `server_budget_enforced=true`，这是 DBA 已落实的配置声明，不是脚本设置预算。
只读 SQL 和 LIMIT 仍可能昂贵；若不能落实资源预算，保持元数据模式。

### 物理身份

物理对象 IRI 的身份键：`source_id|engine|catalog|database|table|column`；路径安全编码或使用
稳定哈希，label 保留可读名。Doris 和 GBase 同名表分别建资源；迁移前后语义对齐不代表同一物理表。
声明 dw:sourceId / dw:engine 与全限定物理名，不把源 host/口令放 IRI。
跨引擎连接只形成候选逻辑关系；没有联邦/ETL 支撑时不能生成可执行跨源 JOIN。

## 3. 文档分解

`extract_documents.py <明确文件列表> --output <新目录>`：

- DOCX：按正文顺序抽段落/表格，定位 body index、表行；不虚构页码。
- XLSX：遍历全部 sheet 和有效行列，保留 A1 坐标、公式文本，不运行宏或重算公式。
- PDF：按页抽文本；无文本页标 OCR required；复杂表格几何、图像、公式需视觉复核。
- PPTX：抽 slide/shape 文本、表格和备注；图片/图表/组内图形需视觉复核。
- UTF-8 文本、SQL、DDL、JSON/YAML/XML、CSV/TSV：保留行号/表行。

结构化字典/指标定义表全量解析，不因单文件/目录输入或文件体积自动丢 sheet/字段。
大文档按块分页喂给模型，机械解析不按文件大小静默降采样；内存预算不足时明确失败并分文件重试。
旧 DOC/XLS/WPS 经本地 LibreOffice 转成 DOCX/XLSX/PPTX，保留原件并登记原件→转换件的 checksum。
扫描件通过已配置视觉/OCR 工具补采，记录工具/页码/置信度；脚本本身不实现 OCR。
转换和 OCR 的原文仍是证据，不作为已确认口径直接发布。

## 4. Git 仓库

使用用户授权的本地 checkout。未提供本地目录时先用已授权 Git 连接器获取；不在 URL 写 token。
`extract_repository.py <repo> --source-id <id> --revision <commit> --include 'sql/*.sql' --output <new.json>`。
可重复 --include；默认包括 SQL、DDL、Java/Python/Go/Rust/XML/YAML。仓库 URL 不保存到证据。
读取 git 对象中的指定 commit，排除 .env/凭证路径、生成目录、symlink/submodule；
未提交工作区不纳入且报告 worktree_changes_included=false。需纳入未提交内容时先显式说明来源差异。
单文件超过 5MB 标记遗漏，不静默截短；出现未解析模板或排除项必须看 coverage。

SQL/DDL 用 sqlglot 提取语法表列、连接、过滤、聚合。CTE 仅做基础过滤，未做完整作用域/血缘解析；
MySQL 语法回退不代表 Doris/GBase 方言验证通过。动态 SQL/MyBatis 模板解析失败时保留原块供评审。
ORM/XML/ETL/报表配置目前提供带行号源码块，由模型读取注解、映射、公式与单位并提出候选；
不宣称已有每种框架的 AST 适配。始终固定 commit/path/line，不运行项目、不执行迁移或 SQL。
代码可能过期；与在线 DDL 冲突时保留双方，分别标适用版本，不能把代码规则视为业务权威。

## 5. 语义抽取、对账与合成

模型按领域与任务问题分批阅读证据：稳定实体→核心；指标/维度/汇总口径→领域；
分析 SOP→场景；物理能力与连接→04；别名/缩写→SKOS。一个表不强制对应一个类。
术语归一化保留原词、候选 IRI、证据与合并理由；同名/共享别名不自动等同。
先写 `review/merge-ledger.json` 与 `review/issues.json`：记录被合并/排除对象、来源、理由、
状态、冲突和下一步；每个来源块的处理结果应能对账，不能因多源合并丢失业务定义。

来源权威按断言类型判断：在线目录支持物理对象；业务口径文档支持公式；已部署 ETL 可佐证计算。
命名相似、少量采样、模型常识只产生候选。文档与代码支持一致且不矛盾时可 inferred，
关键可执行指标/连接仍须 confirmed；冲突 unresolved，保留双方。
“只知道 ADS 指标列”允许确认该物理映射，单独记录上游血缘未知，不伪造 DWD 或核心实体。

组装输入 `decisions.json`：

```json
{
  "bundle_id": "chatbi-domain",
  "bundle_version": "2.0.0",
  "base_iri": "https://arcp.sxmcc.com/zb_data/chatbi/",
  "review_notes": "<来源覆盖与未决项>",
  "assertions": [{
    "file": "01-core.owl.ttl",
    "subject": "https://arcp.sxmcc.com/zb_data/chatbi/core/Customer",
    "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
    "object": {"iri": "http://www.w3.org/2002/07/owl#Class"},
    "status": "confirmed",
    "evidence": ["<真实block_id>"]
  }]
}
```

字面量对象为 `{"value":"客户","lang":"zh"}` 或 `{"value":"1","datatype":"http://www.w3.org/2001/XMLSchema#integer"}`。
所有 URI 使用完整 IRI，subject 在项目 base 内；不接受未经编码的空格/控制字符或 blank node。
SHACL property shape 可使用稳定命名 IRI，不要求 blank node。类/词汇声明也要有证据依据；
共享本体定义可以作为明确文档源采集，不能为通过门禁编造 block_id。

`assemble_bundle.py --decisions <json> --evidence <json> [--evidence <json> ...] --output <新目录>`
验证块引用和三元组唯一性，机械输出七文件、manifest、source inventory、assumptions、release plan。
同三元组多个来源先合并到同一 assertion 的 evidence 数组，不重复断言。
每个断言以 rdf:Statement 记录状态/版本/prov:wasDerivedFrom；04 保存来源定位 metadata ABox。
原始文档文本、代码、数据行不进入 RDF。新 bundle 四态计数为重化断言数，不是文件总三元组数。
非 confirmed 的逻辑公理与 R2RML 映射拒绝组装，保留 review 清单；reification 不会削弱实际公理。

组装器不自动判断行业语义，不自动发明分类或填满空层；词汇/映射/SHACL 都由证据支持的决策提供。
它也不自动完成所有业务门禁。交付前执行 RDF/SHACL、业务规则与已审核 CQ，写 validation 报告。

## 6. 增量维护

已有 bundle 是语义基线，不因“新任务”全部推倒重建。读取受影响资源与原断言，刷新对应证据，
输出三元组 add/delete/change 与稳定 IRI 对应表；未受影响文件保持原样。
组装器只用于新目录构建，不能直接覆盖既有 bundle；若用它生成候选新版本，先保留所有仍有效
基线断言和来源，并评审差异，再把批准增量应用到正式工作副本，重算哈希/计数/版本。
不能拿新来源的小集合重新组装后替换整套本体。断言退役、口径生效期、IRI 重基均需独立记录。
