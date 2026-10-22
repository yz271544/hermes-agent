# Ontology bundle 逐文件契约

## 命名空间与治理

业务、指标、场景、物理资源、映射命名空间相互独立（基域与路径格式见 SKILL 第 1 节）。
用稳定 IRI 作身份，路径组件安全编码，label 视为可变的 multilingual 展示数据。

每个生成资源应暴露：label、定义（如有）、断言状态（confirmed/inferred/candidate/unresolved）、
provenance/来源 ID、bundle 版本、owner/steward（如提供）。

## 01-core.owl.ttl

稳定业务类、datatype/object 属性、分类学、domain/range 声明。物理表名**不是**业务类。
避免在强证据缺失时写等价、同一、不相交、基数公理。
DWS/ADS 预聚合表的记录类不得出现在本文件（SKILL 第 3.1 边界一）。

## 02-domain-metric.ttl

指标是一等资源。表达：定义、公式、聚合、单位/量纲、粒度、维度、时间语义、过滤/排除、
比率构成、可加性、owner、版本、生效期、provenance。计算关键字段未知时不得确认指标
（保持 candidate）。DWS/ADS 记录类、值组类、汇总字段属性放在本文件。

## 03-scenario.ttl

表达分析方法，不是观测结果。链接：触发模式、目标指标、假设、必需维度、基线、时间窗、
下钻序列、证据/停止条件、支撑能力。步骤引用 02 的指标 URI，不内联口径。

## 04-data-capability.ttl

把 Doris/GBase 8a 的 source_id/engine/catalog/database/table/column 对象建模为元数据 ABox，链接到业务语义。
补充表粒度、时间列、分区、度量、维度、新鲜度、质量（有证据时）。
连接契约须写明两端、键列、基数、方向、证据、置信度、状态。
**这不是业务行的 dump**；禁止出现 DML 语句。证据 ABox 仅保存来源身份、checksum、块 ID、定位符，
原文和采样值留在 bundle 外的受控 evidence 工作区。不同来源同名物理对象必须有不同 IRI。
合并了旧 telecom 风格的 core_storage/domain_storage 职责：DWD 表映射与 DWS/ADS 表映射
都放在本文件，但业务概念仍按层归 01/02。

## 05-lexicon-skos.ttl

`skos:prefLabel` 首选术语，重复 `skos:altLabel` 别名，`skos:hiddenLabel` 仅用于非展示
检索变体，带语言标签。共享别名不证明概念等价；向量提出候选 IRI，图关系做决定。

## 06-mapping.r2rml.ttl

R2RML 是 relation-to-RDF 的**执行契约**，与 capability 图谱分离。
只从 confirmed 元数据生成映射；小型参考/主数据优先可执行映射，大型事实表映射默认不执行。
显式写明逻辑源、subject template、谓词、数据类型、目标图、连接。
candidate 排除出发布范围。

## 07-shapes.ttl

校验 label/provenance、指标完整性、全限定物理身份、列归属、连接、映射引用、项目 label 规则。
SHACL 校验的是 RDF 图；加载 shapes 不会自动扫描业务数据库。

## manifest.json

必需键：`bundle_id`、`bundle_version`、`generated_at`（UTC）、`base_iri`、`target_graph`、
`operating_mode`、`sources`、`artifacts`（每文件 sha256 + 三元组数）、`assertion_counts`
（四态全出现）、`publication`。发布候选附哈希。offline 模式 `publication.status` 必须为
`not_attempted` 或 `offline_validated`。`base_iri` 必须是 `arcp.sxmcc.com` URL；
`target_graph` 必须是租户图 IRI `https://arcp.sxmcc.com/tenant/chatbi`（见 SKILL.md §1.2）。

新 bundle `contract_version="1.1"`，`artifacts` 推荐对象形式：
`{"01-core.owl.ttl":{"sha256":"<hex>","triples":123}, ...}`。
兼容列表形式时每项必须含 `path`、`sha256`、`triples`；旧的其他字段名先显式迁移。
`assertion_count_basis="rdf_statements"` 表示四态计数统计 `rdf:Statement`，不统计标签三元组或资源数。
来源 `coverage.complete=false` 必须在 assumptions 解释影响，不能以重跑校验代替遗漏处理。
指标直接映射汇总列可确认相关断言；`sourceClass` 未知不是创建虚假核心类的理由。
