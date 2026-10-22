# 校验与 PostgreSQL 18 + pg-ripple 发布

## 离线与来源门禁

1. `scripts/validate_bundle.py <bundle>` 检查结构、manifest 哈希、目标 IRI 和凭证线索。
2. `scripts/validate_rdf.py <bundle> --shacl --output <bundle>/reports/validation.json`
   真解析 RDF，检查计数、重复、指定引用、本地类型声明、断言计数与声明的 SHACL。
3. 按 health-check 执行业务规则与已审核 CQ，记录来源覆盖、预期值/实际值。
   缺依赖或未执行的规则一律报告未验证。SHACL 空图通过不等于业务完整。
4. 刷新 Doris/GBase 8a 被引用对象，检测改名/删除/类型变化。LIMIT 不保证限制扫描，
   客户端超时不保证服务端终止；GBase 服务端预算由 DBA/网关配置并验证。

### 目标发现

`scripts/pg_ripple.py discover --output <new-capabilities.json>` 从 `CHATBI_PG_DSN` 读取凭证，
只读检查 PG 版本、扩展版本与公开函数。禁止直接修改 `_pg_ripple.*`。
旧 0.136.0 环境的函数、RLS、HA 与 UPDATE 行为仅是历史证据，必须在当前目标重验。
官方 [Bulk Load](https://trickle-labs.github.io/pg-ripple/user-guide/sql-reference/bulk-load.html)
列出 `load_turtle_into_graph(data text, graph_iri text)`；脚本先核对安装签名再调用。
内联加载不依赖 PG 服务器看到 Hermes 容器路径；大包文件加载需另行安排各服务器路径与权限。

### 初次加载 / 独立 staging

DBA 预建目标租户/图并授予最小权限；staging 不会按父 IRI 路径自动继承权限。
完成评审后写 bundle 绑定记录（不可伪造用户批准或 CQ 结果）：

```json
{"manifest_sha256":"<实际manifest哈希>","approved":true,"cq_passed":true,"source_references_refreshed":true,"authorization_reference":"<实际授权记录>"}
```

用户授权覆盖具体目标后，使用 `terminal` 执行：

```bash
python3 scripts/pg_ripple.py load-empty --bundle <bundle> \
  --graph https://arcp.sxmcc.com/tenant/chatbi/staging/<release-id> \
  --review-record <review.json> --execute --output <new-load-report.json>
```

首次正式加载可指定固定租户图，仍必须为空。脚本重新校验 RDF/SHACL、精确加载字节的哈希、
公开签名、空图条件；事务内加载七个 TTL 并比对显式三元组数，异常回滚。
扩展事务性、权限与计数口径须在目标版本验收。已有数据拒绝覆盖。
加载 shapes TTL 只保存 RDF 元数据，不调用全局 load_shacl；不执行推理或 R2RML。
加载结果写独立 receipt，不静默改 manifest。在线业务 CQ 另行运行保存，才完成上线验收。
自动推理/附加治理写入开启时，先核实显式/推导/治理数据的计数方式，再比较 manifest。

### 已有生产图升级

提供的加载脚本不清空/覆盖旧图。通过已发现并验证的 MCP/HTTP/SQL 公共接口完成：
备份与恢复演练 → 独立 staging 加载与业务/CQ 验证 → 授权 → 已验证的原子切换或事务增量
→ 在线回归 → 保存版本/哈希/操作者/备份引用。安排写入隔离；advisory lock 只协调同锁客户端。
没有可靠晋升或回滚接口时停在 staging，明确报告能力缺口，不猜测 DROP/CLEAR 或 UPDATE。
离线 IRI 重基是本地构建；更新在线图才属于发布。R2RML 执行需单独授权和行量评审。

## 冒烟测试意图

类/label、属性/domain/range、同义词解析、指标语义、场景下钻路径、语义→Doris/GBase 链接、
连接状态/provenance、candidate 映射的排除性。重基后另跑“旧域名残留 = 0”检查。

## 发布状态词表（六选一）

`offline_validated` / `metadata_validated` / `staged` / `published` / `failed` / `not_attempted`

## 旧域名 bundle 重基（rebase）

全量替换 IRI（TTL 前缀、字面量、manifest `base_iri`/`target_graph`）→ 重算全部 artifact
哈希 → bump `bundle_version` → 重跑完整校验（含 rdflib 真解析与旧域名残留检查）→ 发布。
重基可先在本地完成；在线旧 IRI 的替换须走已验证且授权的生产升级流程，不能自动清图。
