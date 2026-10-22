# 工作流：证据 → 对账 → 建模 → 评审 → 交付

## 1. 选工作模式

用最不依赖连接的方式产生诚实结果（SKILL 第 7 节三模式表）。离线起草不得因 pg-ripple
不可达而阻塞；不得把离线草稿说成已部署。

## 2. 证据盘点（建模前必做）

写 `reports/source-inventory.md`，每个来源记录：

- 稳定来源 ID、标题/路径、类型、版本或修改时间、owner（如知）；
- 抽取状态与相关范围；
- 权威性：authoritative / supporting / stale / conflicting；
- checksum（如有）；
- 该来源支持的概念、指标、场景、表、策略。

**上传文档**：提取标题、术语表、指标定义、公式、维度规则、时间窗、排除项、分析套路、
ownership；保留页/节/段定位符；不整段导入 RDF。扫描版公式在核对前按 candidate 处理。

**Doris / GBase 8a**：只收集建模所需元数据——source_id、engine、catalog（若适用）、database、表、表注释、引擎/模型、键、分区、
分布、新鲜度标记；列名、类型、可空性、注释、键/聚合角色、序号；视图定义（MCP 安全暴露时）；
约束与已文档化连接；**有界 profile 按需**：空值率、近似基数、min/max、少量脱敏样本。
不请求全量事实行；样本值不作为业务规则的证据。

**Git**：先明确仓库、commit、路径范围。读取已提交代码，解析 SQL 与映射/ETL 定义，
记录路径/行号；不运行仓库代码。具体采集命令与文档分块格式见
[multisource-extraction.md](multisource-extraction.md)。数据库/文档/代码各自保留覆盖报告。

## 3. 来源对账

工作矩阵，一行一条拟议断言：

```
assertion | source IDs | confidence | status | conflict | owner decision
```

状态四态一致使用：confirmed / inferred / candidate / unresolved（定义见 SKILL 第 5 节）。
指标目录可管辖公式、Doris 目录管辖物理名；权威来源冲突时**保留双方证据**，断言标
unresolved，写进 `reports/assumptions.md` 等 owner 裁决。

## 4. 按依赖顺序建模

1. 核心业务类、属性、关系（01）；
2. 指标、维度、粒度、计算语义（02）；
3. 分析场景、假设、所需指标、下钻路径（03）；
4. Doris/GBase 8a 物理元数据、来源定位与语义能力链接、连接契约（04）；
5. label、别名、缩写、短语变体（05）；
6. R2RML 契约（06，生成但默认不执行）；
7. SHACL 约束（07）。

**只在身份已确立时复用既有 IRI**；否则在配置命名空间下铸 candidate IRI。
仅文档支持的类可缺映射；预聚合指标可直接链接汇总列，未知明细血缘不得补造。
模型负责语义决策，`assemble_bundle.py` 只按决策机械组装，不能替代对账与业务评审。

## 5. 评审门

以下任一项未决时停在发布之前：指标计算语义、连接行为、歧义概念、缺失物理对象、
场景依赖、安全/租户边界。candidate 可以留在草稿 bundle，但必须清晰标记且排除出可执行映射。

## 6. 校验与交接

- 跑离线校验（`scripts/validate_bundle.py` + rdflib 真解析 + health-check 体检）；
- 连接模式补跑 references/validation-and-publish.md 的有界检查；
- 按 artifact 与断言状态汇总计数；
- 列出所有未决发布阻塞项；
- 把精确的下一步写进 `reports/release-plan.md`；
- 附 SKILL 第 6 节变更报告。
