# 本体体检（工作流 B）

用户说“检查本体”“体检”“有没有不一致”时执行，覆盖三类检查。

## 1. 结构检查（通用 SPARQL）

标准 SPARQL，可在任意支持 SPARQL 的环境运行（本地 rdflib、Jena、pg-ripple、Fuseki）。
建议存为 bundle 旁 `sparql/` 下的 `.rq` 文件复用。**每个查询必须显式声明用到的前缀。**

```sparql
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

# 所有类
SELECT ?class ?label WHERE { ?class a owl:Class . OPTIONAL{?class rdfs:label ?label} }

# 继承关系
SELECT ?child ?parent WHERE { ?child rdfs:subClassOf ?parent }

# 对象属性 domain/range（画关系图用）
SELECT ?p ?d ?r WHERE { ?p a owl:ObjectProperty ; rdfs:domain ?d ; rdfs:range ?r }

# 数据属性
SELECT ?p ?d ?r WHERE { ?p a owl:DatatypeProperty ; rdfs:domain ?d ; rdfs:range ?r }

# 类型分布诊断
SELECT ?type (COUNT(?s) AS ?c) WHERE { ?s a ?type } GROUP BY ?type ORDER BY DESC(?c)

# 旧域名残留检查（重基后必跑）
SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "https://chatbi.sxmcc.com/")) }
```

## 2. 业务一致性规则（每条违例都要报）

- 已声明物理映射的业务类，其 `dw:mapsToTable` 必须指向真实 `dw:PhysicalTable`；纯概念类允许未映射。
- 已映射的数据属性须有 xsd range 与真实物理列；治理、证据、抽象属性不强制物理映射。
- 可执行指标须有 aggregation、物理来源与公式/单位/粒度等完整计算语义；
  仅汇总列已知时允许缺上游 sourceClass，记录未知明细血缘；关键计算项未知不得执行。
- 每个 `sce:AnalysisStep` 的 `sce:usesMetric` 必须指向真实存在的指标 URI。
- 同义词歧义应有上下文：同一别名可对应不同概念，记录适用域/来源，不能强制等同或静默合并。
- 无悬空引用：所有 `dw:mapsToColumn` / `dw:sourceClass` / `dw:mapsToTable` 目标 URI 都存在。
- 无跨层污染：DWS/ADS 记录类不出现在 01-core；仅服务某汇总表的码表与服务对象同层。
- candidate 资源不出现在 06-mapping.r2rml.ttl 的可执行映射中。
- 断言状态与 manifest `assertion_counts` 对得上。

## 3. 可选 SHACL 校验

- 有 SHACL 引擎时载入 `07-shapes.ttl` 跑校验；每条 violation 报：哪个类/属性、
  违反哪条 shape、建议修复。
- 没有引擎时仅报告已执行的业务规则核查，不声称完成或等价替代全部 SHACL 校验。

自动化入口为 `scripts/validate_rdf.py <bundle> --shacl`。自动检查包括 RDF 语法、文件哈希与计数、
重复三元组、指定引用、局部类型声明、断言计数与声明的 SHACL；第 2 节的所有业务规则和
经业务审核的 CQ 仍需明确执行并记录结果，不能把该脚本描述成完整 OWL 推理器。

## 4. 体检报告格式

按层给出：类数/属性数/指标数/场景数/三元组数、发现的问题清单（按严重度：
blocker / major / minor）、修复建议。若体检导致改动，改动仍须走 SKILL 第 6 节变更报告。
