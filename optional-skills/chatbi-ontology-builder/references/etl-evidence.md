# 大型 ETL 证据 → 多源本体

## 入口与范围

`scripts/etl.py`、`scripts/etl_evidence/` 随 skill 安装，Python 标准库 SQLite + sqlglot 支持离线抽取；
只有中央元数据库采集需要 PyMySQL。没有原血缘项目路径、账号、库连接或 Config 依赖。
SQLite 与源快照保存在工作区，不写 skill 安装目录，不执行 ETL/仓库代码。
语法成功、物理目录绑定与业务口径确认是三个不同状态。

优先由用户的目标指标、报表或业务域确定表范围。没有表名时先结合业务定义、文档和表注释召回候选表，
再确认/缩小相关范围。缺少某一种来源不阻塞其他来源的草稿，不默认扫描整库事实数据。

## 1. 索引 ETL

以下命令在 skill 目录运行，工作区以 `/work` 举例；解释器使用实际 terminal 已安装依赖的 Python。

```bash
python3 scripts/etl.py scan /inputs/etl-repo --revision HEAD \
  --include 'jobs/*.sql' --source-id etl-gbase100 --engine gbase8a --dialect teradata \
  --index /work/etl.sqlite
```

- Git 模式固定 commit，批量读取对象，不纳入未提交工作区，不跟随 symlink/submodule。
- Git 快照放在 index 同级 git-snapshots，按来源、范围、预算、commit 隔离；只读取本次选中 blob，
  校验物化内容哈希。使用 Git commit/path/blob/行号溯源，不运行工作树脚本。
- 不同 commit 的未变化文件复用 AST 结果，同时刷新 commit 定位；变化或删除文件更新/清理索引。
- 无 Git 的授权目录可省略 --revision；此时明确为 worktree_file_snapshot，不冒称已提交版本。
- 可选 --max-files 默认 50000；每文件默认 5 MB、每语句 500 KB，超过预算登记遗漏。
  工作区模式按目录限定范围；--include 当前仅用于 Git 模式。
- `--engine` 明确来源身份，`--dialect` 是解析语法路径。Teradata 路径可用于兼容既有 GBase ETL，
  不代表 GBase 引擎已验证；实际语法按来源选择，不按文件夹名字猜平台。

INSERT/CTAS 的目标列、源列、转换表达式、聚合/CASE/窗口、JOIN、过滤、分组、CTE/UNION 分支
分别保存。引号内 `${TxDate}` 等变量保留表达式，不执行日期推算；字符串分号不误切。
动态/过程体、歧义或不支持项保留源文、error/partial/unsupported 和原因，不以空列表冒充成功。

## 2. 可选：中央 MySQL 元数据目录

沿用用户已有元数据服务时，显式配置 host/port/database/user/password_env，示例见
`deployment/metadata-catalog.example.json`。密码只从环境读取，不把项目原有明文配置拷进 skill。
账号须只读；不从平台 URL 获取凭证或连接仓库，也不修改元数据表。

```bash
python3 scripts/etl.py metadata-snapshot --config /config/metadata.json \
  --index /work/etl.sqlite --source-id etl-gbase100 --platform gbase100 \
  --output /work/metadata-gbase100-v1.json

python3 scripts/etl.py scan /inputs/etl-repo --revision HEAD \
  --include 'jobs/*.sql' --source-id etl-gbase100 --engine gbase8a --dialect teradata \
  --index /work/etl.sqlite --metadata /work/metadata-gbase100-v1.json
```

此适配器针对已知 `audit_source_plat/tb_meta_tables/tb_meta_columns` 模式。
其他元数据服务通过已有数据库/MCP 采集路径进入统一 evidence，不假设同名表模式通用。
按明确 platform ID、table ID 和索引引用范围查询，默认最多 1000 张表，可显式调整 --max-tables。
同名跨平台不合并；多个 catalog/schema 候选不取第一条。
ordinal 必须连续、无重复、可转为 1..N 数值，才补 INSERT 列序、展开通配符；不拿字段 ID 排序。
JOIN USING/NATURAL 的未限定星号、未知列序/字段/来源仍保留待评审。
目录中没有原生 DDL 时标 native_ddl_not_collected，不拼接字段冒充原生 DDL。
类型、长度、精度、键和可空标志保留原目录值，业务唯一性/必填性另行确认。
元数据内容指纹进入缓存，变化重新绑定；只有采集时间变化不会重做语义提取。

## 3. 按目标表检索并导出

```bash
python3 scripts/etl.py query --index /work/etl.sqlite --source-id etl-gbase100 \
  --table ADS.USER_DAY --role write --limit 10

python3 scripts/etl.py export --index /work/etl.sqlite --source-id etl-gbase100 \
  --table ADS.USER_DAY --table ADS.REGION_DAY --role write --limit 20 \
  --max-chars 200000 --output /work/evidence/etl-selected.json
```

--table 是 SQL 中精确的 `[catalog.]database.table`，可重复，去重后采用全局 limit。
源文件原文与完整 AST 结果留在索引；不要把整个 SQLite 或所有行交给模型。
超过字符预算的完整块登记 omitted_statement_ids，不截短 SQL 冒充完整。
需要更大单条语句时显式提高预算或缩小表范围；当前未实现列级检索/自动上游递归，不假定已有这些功能。

统一 envelope 中 source_type=etl；block_type=etl_sql_evidence。定位包含文件 SHA256、字符/行范围、
语句 ID；Git 模式还含 commit、blob ID、repository。元数据快照/平台/表列 ID 和字段注释随证据保留。
导出辅助脱敏，但原始索引仍是受控源码证据，不作为公开本体交付物。
脚本不覆盖已有导出/快照。返回 2 是部分成功/遗漏/待评审，不能报告全部成功。

## 4. 结合数据库、文档、提示词归纳

```bash
python3 scripts/extract_prompt.py /work/input/user-definitions.txt \
  --source-id owner-notes --authority business_definition \
  --output /work/evidence/owner-notes.json

python3 scripts/prepare_review.py \
  --evidence /work/evidence/etl-selected.json \
  --evidence /work/evidence/warehouse.json \
  --evidence /work/evidence/document.json \
  --evidence /work/evidence/owner-notes.json --output /work/review-v1
```

文档采集器可能输出多个 evidence 文件；按实际 source_id 显式传入每个相关文件，不把输出目录当 JSON。
prepare_review 输出 source-inventory.json、candidates.json、merge-ledger.json、issues.json；
每个块都记为 pending_semantic_review，不代表已经完成语义归纳。
默认 5000 块、10000 候选、输入总大小 50 MB；超预算明确失败，保留原证据并缩小评审范围。

Hermes 继续执行：

1. 按物理身份及术语候选归组，阅读原始证据，区分明细实体、预聚合记录、指标与场景。
   一个物理字段/表不直接等于一个业务概念，文件夹 DWD/DWS 名字只是线索。
2. 对每个指标整理名称、定义、来源、表达式、聚合、过滤、维度、粒度、周期、单位、可加性、
   排除项、owner、版本/生效期；SQL 只证明代码表达式，不证明正式业务口径或部署状态。
3. 将数据库目录的物理身份、文档的业务定义、用户陈述与 ETL 计算逐项对账。
   不同 UNION 分支的表达式先作为分支证据，不自动当作冲突或合成一个公式。
   JOIN 条件不证明 1:N 基数，COUNT(*) 不证明去重用户数，变量名不证明时间粒度。
4. 在 merge ledger 写明每个来源块被复用、合并、排除或待核实的理由；保留冲突双方。
   提示词的任务要求不当作事实；owner_decision 标签本身也不是全局授权/已确认口径。
5. 按已有契约写 decisions.json，每条断言引用真实 block_id；缺关键项保持 candidate/unresolved。
   不发明未提供的来源/单位/主键/实体分类，不把未确认逻辑公理或映射放进可执行层。
6. 用 assemble_bundle.py 组装七文件，执行 validate_bundle.py、validate_rdf.py --shacl 和业务 CQ。
   更新既有本体时先保留仍有效基线断言，增量合并，不用新来源小集合覆盖整套本体。

“已生成证据/候选”与“已交付通过校验的本体”分开报告。仅授权本体整理不等于发布、执行 R2RML
或运行分析 SQL；发布沿用既有独立流程。

交付前还需检查指标、维度、物理表/列是否有适当类型与引用关系，指标与维度是否连接，
SHACL 是否包含实际生效的约束。空 Shapes 的 conforms=true 只说明未发现约束违例；
若这些业务建模项缺失，交付状态必须保持草稿并列明缺项，不能仅据 RDF 校验返回 0 宣称完整。

## 离线基准

tests/test_etl_integration.py 覆盖独立目录运行、固定 commit、缓存来源刷新、预算遗漏、
物化 Git 快照篡改、平台/列序绑定、多目标表去重导出及提示词陈述状态。
tests/fixtures/sql_evidence 保留两段真实 SQL 和原始来源哈希/位置。
四源离线集成测试将模拟数据库目录、业务文档、提示词、ETL 的证据对账后组装七文件，
执行 RDF 真解析和一条实际生效的 SHACL 形状；它不代替生产平台和业务口径验收。
