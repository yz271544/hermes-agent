# 建模细节：词汇表、模板、要素提取

## 1. 统一前缀（所有文件头部一致，按 bundle 基域实例化）

```turtle
@prefix core:  <https://arcp.sxmcc.com/zb_data/chatbi/core/> .
@prefix dom:   <https://arcp.sxmcc.com/zb_data/chatbi/metric/> .
@prefix sce:   <https://arcp.sxmcc.com/zb_data/chatbi/scenario/> .
@prefix cap:   <https://arcp.sxmcc.com/zb_data/chatbi/capability/> .
@prefix lex:   <https://arcp.sxmcc.com/zb_data/chatbi/lexicon/> .
@prefix src:   <https://arcp.sxmcc.com/zb_data/chatbi/source/> .
@prefix gov:   <https://arcp.sxmcc.com/zb_data/chatbi/governance/> .
@prefix dw:    <https://arcp.sxmcc.com/zb_data/chatbi/dw/> .
@prefix owl:   <http://www.w3.org/2002/07/owl#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos:  <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .
@prefix sh:    <http://www.w3.org/ns/shacl#> .
```

`dw:` 是共享“映射词汇”，表达“概念 ↔ 物理表/列”的语义连接，属于本体建模的一部分
（让指标能回溯到字段、推理出 SQL），保留在主干。每文件前缀名可不同，但命名空间必须一致。
以上为项目示例；其他项目替换 database/domain 路径。使用 `/` 命名空间，不与旧 `#` 风格混用；
已有 `#` IRI 的 bundle 不静默改名，单独评审迁移。租户图 IRI（`target_graph`）见
SKILL.md §1.2，不进前缀。

## 2. dw: 映射本体（共享词汇）

```turtle
dw:PhysicalTable  a owl:Class .
dw:PhysicalColumn a owl:Class .
dw:Measure        a owl:Class .
dw:Dimension      a owl:Class .
dw:Join           a owl:Class .
dw:mapsToTable     a owl:ObjectProperty .   # 概念类 → 物理表
dw:mapsToColumn    a owl:ObjectProperty .   # 概念属性 → 物理列
dw:hasColumn       a owl:ObjectProperty .
dw:belongsToTable  a owl:ObjectProperty .
dw:joinsTo         a owl:ObjectProperty .
dw:leftColumn      a owl:ObjectProperty .
dw:rightColumn     a owl:ObjectProperty .
dw:primaryKey      a owl:ObjectProperty .
dw:aggregation     a owl:DatatypeProperty .
dw:grain           a owl:DatatypeProperty .  # 一行代表什么
dw:unit            a owl:DatatypeProperty .
dw:dataType        a owl:DatatypeProperty .
dw:catalogName     a owl:DatatypeProperty .
dw:schemaName      a owl:DatatypeProperty .
dw:tableName       a owl:DatatypeProperty .
dw:columnName      a owl:DatatypeProperty .
dw:sourceId        a owl:DatatypeProperty .
dw:engine          a owl:DatatypeProperty .
dw:timeGrain       a owl:DatatypeProperty .
dw:definedBy       a owl:DatatypeProperty .
dw:sourceClass     a owl:ObjectProperty .   # 指标 → 核心层来源类
dw:sourceColumn    a owl:ObjectProperty .   # 指标 → 来源物理列
dw:hasMetric       a owl:ObjectProperty .
dw:hasDimension    a owl:ObjectProperty .
dw:sendTo          a owl:DatatypeProperty .
dw:cycle           a owl:DatatypeProperty .
```

场景层专用：`sce:AnalysisScenario`、`sce:AnalysisStep`、`sce:OutputSpec`、
`sce:goal`、`sce:order`、`sce:stepDesc`、`sce:usesMetric`、`sce:dependsOn`、
`sce:chartType`、`sce:requiredDim`、`sce:conclusionTemplate`、`sce:drillDown`。

## 3. Turtle 模板

### 3.1 核心层类（明细实体）
```turtle
core:User
    a owl:Class ;
    rdfs:label "用户"@zh ;
    rdfs:comment "使用移动业务并归属某客户的自然人/号码主体"@zh ;
    rdfs:subClassOf core:Party ;
    skos:altLabel "客户"@zh, "号码"@zh, "subscriber"@en ;
    dw:mapsToTable cap:dwd_user .

core:userId
    a owl:DatatypeProperty ;
    rdfs:label "用户标识"@zh ;
    rdfs:domain core:User ;
    rdfs:range xsd:string ;
    dw:mapsToColumn cap:dwd_user_user_id .

cap:dwd_user
    a dw:PhysicalTable ;
    dw:catalogName "doris" ; dw:schemaName "dwd" ; dw:tableName "dwd_user" ;
    dw:grain "一行代表一个用户(号码)"@zh ;
    dw:primaryKey cap:dwd_user_user_id .
```

### 3.2 领域层指标（汇总口径）
```turtle
dom:UpFlow                       # 上行流量
    a dw:Measure ;
    rdfs:label "上行流量"@zh ;
    skos:altLabel "上行业务流量"@zh ;
    dw:mapsToColumn cap:dws_user_flow_month_up_flow ;
    dw:aggregation "SUM" ;
    dw:unit "MB" ;
    dw:sourceClass core:User ;
    dw:sourceColumn cap:dwd_flow_usage_up_flow ;
    dw:timeGrain "月"@zh ;
    dw:definedBy "用户当月产生上行流量之和，排除套餐赠送部分"@zh .
```

指标完整字段（公式、排除项、可加性、比率构成、owner、版本、生效期、provenance、断言状态）
按 references/ontology-contract.md 第 02 文件节补齐；口径关键要素未知时保持 candidate。

### 3.3 场景层（分析思路 + 输出要求）
```turtle
sce:FlowStructureAnalysis
    a sce:AnalysisScenario ;
    rdfs:label "流量结构分析"@zh ;
    sce:goal "分析用户流量在套内/套外、4G/5G、上下行等维度的构成与变化"@zh .

sce:Step1_IdentifyComposition
    a sce:AnalysisStep ;
    sce:order 1 ;
    sce:stepDesc "按套内/套外拆分总流量"@zh ;
    sce:usesMetric dom:FlowInPlan, dom:FlowOutPlan .

sce:FlowReport_Output
    a sce:OutputSpec ;
    sce:chartType "堆叠柱状图"@zh ;
    sce:requiredDim core:Region, core:TimeMonth ;
    sce:conclusionTemplate "本月{region}流量总量{x}，其中套外占比{y}%"@zh ;
    sce:drillDown "下钻至小区级基站流量"@zh .
```

### 3.4 SHACL 约束（07-shapes.ttl，发布前审核）
```turtle
core:UserShape
    a sh:NodeShape ;
    sh:targetNode core:User ;
    sh:property [ sh:path dw:mapsToTable ; sh:minCount 1 ] .

dom:MetricShape
    a sh:NodeShape ;
    sh:targetClass dw:Measure ;
    sh:property [ sh:path dw:aggregation ; sh:minCount 1 ] ;
    sh:property [ sh:path dw:sourceColumn ; sh:minCount 1 ] .
```

这是已映射 User 类和可执行指标的示例，不强加给未映射纯概念类或候选指标。
`sh:targetNode core:User` 检查类资源本身；`sh:targetClass core:User` 检查用户实例。
shape 应表达当前 bundle 实际约束；空图 SHACL 通过不代表业务验证完成。

## 4. 要素提取细则（工作流 A 第 2 步的展开）

- **主键（候选）**：样例唯一性 + NOT NULL/唯一约束 + 业务粒度三信号合议。样本里唯一
  ≠ 业务唯一：复合键（如 用户+月份）要整体判断。**先标“候选主键”由用户确认，不写死。**
- **外键/关联**：`user_id`、`cell_id`、`*_code` 或跨表同名字段 → 生成 `owl:ObjectProperty`
  + `rdfs:range`；用样例值能否在关联表命中做抽查验证。仅命名相似的连接标 candidate，
  显式记录两端、键列、基数、方向、置信度。
- **字段语义类型**：
  - 数值型且名含 `_cnt`/`_amt`/`_flow`/`_duration` → 指标/度量候选（`dw:Measure` +
    `dw:aggregation`/`dw:unit`）；
  - 名含 `code`/`type`/`status`/`kind` 或低基数 → 维度或枚举；
  - 时间字段（`stat_month`/`day_id`/`create_time`）→ `dw:timeGrain`。
- **枚举/码表 → 维度成员**：从样例枚举取值（`net_type ∈ {4G,5G}`、`roam_type ∈ {本地,省内,国际}`），
  仅产生观测值候选；权威字典确认后才断言完整枚举并附 `skos:altLabel`。
- **同义词**：行业知识 + 字段名/注释 → 中文别名、英文别名、口语/业务别名
  （“上行流量”=“上行业务流量”）。共享别名**不证明**概念等价。
- **数据质量约束**：NOT NULL → 必填（可进 SHACL `sh:minCount 1`）；默认值、取值范围、
  正则格式从样例只能形成候选；SHACL 业务约束需来源或 owner 明确支持。
- **单位与聚合**：命名/量级仅提出候选，不能凭 `_cnt` 或 `_rate` 确认可执行口径。
- 以上落到哪层：核心层取主键/外键/语义类型/时间粒度/质量约束；领域层取度量候选/维度/枚举/单位；
  场景层可由高频维度组合提示分析角度。

## 5. 通信行业建模经验（实例）

- 上网流量 = 套内 + 套外；5G 流量 = SA + NSA：父子指标用 `hasSubMetric` 父→子方向，
  并声明 `isSubMetricOf` 为 `hasSubMetric` 的逆关系（v0 曾方向反转，v2 修正）。
- 周期累计（日/月/年累计）用**独立预累计列**建模，**禁止对日值求和**替代预累计列。
- 地域层级：`province_code_sx` 的 `parent_id` 1=全省、2=地市、3=区县、4=网格。
- 主题域码（KH=客户、ZW=网络、LL=流量、SY=收入…）若无权威字典支撑，标 candidate/inferred，
  不冒充 confirmed。

## 6. .owl（RDF/XML）可选导出

默认交付物是 `.ttl`（下游 `pg_ripple.load_turtle` 直接加载）。仅当下游明确要求 RDF/XML 时导出：

1. 用 rdflib 解析各 `.ttl`，`g.serialize(format='xml')` 写同名 `.owl`。
   **rdflib 的 RDF/XML 序列化器注册名是 `"xml"`，不是 `"rdf/xml"`**（后者报 PluginException）。
2. 导出后按 `format='xml'` 回读，断言三元组数与源 `.ttl` 一致，防序列化丢三元组。
3. 序列化顺序受 Python 哈希随机化影响（跨进程字节不一致）：构建脚本固定
   `PYTHONHASHSEED=0`（必要时 os.execve 自重置），保证交付物可复现、可 diff。
4. SHACL 形状文件例外，保留 `.ttl`，不转 `.owl`。
5. `.owl` 哈希与三元组数一并记入 manifest `artifacts`。
