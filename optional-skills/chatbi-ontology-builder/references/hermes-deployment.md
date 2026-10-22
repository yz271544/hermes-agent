# Hermes 部署、依赖与配置

## 1. 当前源码检查结论

本次检查路径 `/lyndon/iProject/pypath/hermes-agent`，未修改该源码项目。
所见 Dockerfile 先 `uv sync --frozen` 安装 Hermes extras，再安装 Hermes 与
`optional-mcps/sparql-mcp`；Python 位于 `/opt/hermes/.venv`，`HERMES_HOME=/opt/data`。
Compose 的 gateway/dashboard 共享 `/opt/data` 卷；配置示例支持 `terminal.backend: local`。
检查的 pyproject 未直接列出本 skill 的数据库、RDF/SHACL、Office/PDF、SQL 解析依赖。
即使某些包通过 MCP 间接装入，也不应把传递依赖当作本 skill 的稳定运行保证。

结论：**无需修改 Hermes 智能体核心；脚本直连方案需在 terminal 执行环境安装本 skill 依赖。**
使用远程 MCP 代采集时，对应数据库驱动装在 MCP 服务，不必重复装入 Hermes；本地组装与校验
仍需 RDF/SHACL 依赖，除非也交给已验证的远端服务。

## 2. 依赖放在哪

| 功能 | Python 依赖 | 额外要求 |
|---|---|---|
| Doris/GBase 8a 元数据/采样 | PyMySQL | 只读账户、网络、实际端口/认证/TLS 验收 |
| PostgreSQL 18 + pg-ripple | psycopg[binary] | PG 网络/CA、扩展安装与租户权限在 PG 侧完成 |
| RDF/Turtle/SHACL | rdflib、pyshacl | 本体与证据文件可读、工作区可写 |
| Git SQL 静态解析 | sqlglot | git 可执行程序、本地只读 checkout |
| DOCX/XLSX/PPTX | python-docx、openpyxl、python-pptx | 不执行宏、不依赖 Office 运行时 |
| PDF 文本 | PyMuPDF | 扫描件另需已有 OCR/视觉服务 |
| 旧 DOC/XLS/WPS 转换（可选） | 不额外依赖 Python 库 | LibreOffice；中文字体按实际渲染需求安装 |

全量 Python 清单：`deployment/requirements.txt`，均有版本上界。
这不是已锁定的完整传递依赖清单；生产构建验收后记录 Python/包版本并固定镜像 digest，
如需严格复现再用组织的软件源与制品流程生成含哈希的锁定依赖。
无需在 Hermes 容器安装 PostgreSQL 服务端或编译 pg-ripple；它们属于目标数据库服务器。
本实现面向 GBase **8a**，不能拿来直连 8s；实际 8a 认证不兼容 PyMySQL 时保留统一证据契约，
切换已验收的厂家 JDBC/MCP 适配，而不是盲目改认证或数据库设置。

## 3. 镜像构建（推荐派生镜像，保持 Hermes 锁文件独立）

先在 Hermes 源码目录构建原镜像：

```bash
cd /lyndon/iProject/pypath/hermes-agent
docker build -t hermes-agent:base .
```

再以本 skill 目录为构建上下文：

```bash
cd /ABS/chatbi-ontology-builder
docker build -f deployment/Dockerfile.chatbi \
  --build-arg HERMES_BASE_IMAGE=hermes-agent:base \
  -t hermes-agent:chatbi .
```

派生镜像创建独立 `/opt/chatbi-ontology` venv；保留原 entrypoint、PATH、HERMES_HOME 和服务降权机制。
使用绝对解释器 `/opt/chatbi-ontology/bin/python` 执行 skill 脚本，避免误用 Hermes venv。
不在运行时执行 uv sync 更新 Hermes 锁定环境；不把依赖仅装进临时容器后认为镜像已具备能力。
构建前检查原镜像中 git 已安装；如需旧格式转换，在派生 Dockerfile 增加 LibreOffice 安装层，
通过现有组织 apt 镜像安装，保留原件并记录转换证据。当前派生文件未默认安装 OCR/LibreOffice。

源码路径不在本次可写工作区，因此提供可直接使用的派生文件和配置；没有改写源项目 Dockerfile、
pyproject、uv.lock，也没有执行生产容器重建或重启。

## 4. 挂载与 Hermes 配置

在实际 Compose 中保留原 `/opt/data` 持久卷和 gateway 启动方式，gateway 镜像改为
`hermes-agent:chatbi`。删除 gateway 原先指向 Hermes 根 Dockerfile 的 `build` 段，使用已构建镜像，
否则后续 `docker compose up --build` 会用原 Dockerfile 覆盖派生镜像标签，丢失本 skill 依赖。
gateway/dashboard 使用同一镜像便于保持环境一致。
把下列条目合并到 **现有 gateway 的 volumes**，不要用不完整示例替换整份 Compose：

```yaml
volumes:
  - /ABS/HERMES_STATE:/opt/data
  - /ABS/chatbi-ontology-builder:/opt/data/skills/chatbi-ontology-builder:ro
  - /ABS/ontology-config:/opt/data/ontology-config:ro
  - /ABS/ontology-input:/opt/data/ontology-input:ro
  - /ABS/ontology-repositories:/opt/data/ontology-repositories:ro
  - /ABS/ontology-work:/opt/data/ontology-work:rw
```

`/ABS/...` 是宿主机绝对路径，必须替换；读写工作区由运行 Hermes 的 UID/GID 持有。
如现有 state 卷内已安装 skill，可直接更新该目录，无需重复 bind mount。
只读 skill 挂载用于固定生产版本；若 Hermes curator/skill_manage 需要自修改，改用受控更新流程。
该 venv 中的脚本独立于 Hermes 内部 Python，不覆盖模型或工具实现。

合并活动 profile 的 config.yaml（默认容器配置 `/opt/data/config.yaml`）：

```yaml
terminal:
  backend: local
  cwd: /opt/data/ontology-work
  timeout: 180
```

若 terminal 是 Docker/SSH 远程环境，依赖、skill、输入挂载和凭证都必须在那里可见，
仅在 gateway 容器安装不足。启用终端/文件/skills 能力按当前 Hermes `hermes tools` 配置，
新会话验证 skill 可发现与调用；不要假设把文件放进任意目录便已挂载到活动 profile。
profile 场景按该 profile 实际 HERMES_HOME 安装和配置，避免误用默认 profile 凭证。

### 凭证与数据库配置

复制两个 example.json 到 `/ABS/ontology-config` 并修改 host、实际协调节点端口、database、
用户、表名、采样列和过滤。例子端口仅示例，不自动发现生产集群入口。
可加入 `"tls":{"ca":"/opt/data/ontology-config/warehouse-ca.pem"}` 启用 CA 与主机名验证；
不设置此项不会请求 TLS，必须按实际数据库接入策略选择，不能宣称默认加密。
配置 JSON 不写密码，`password_env` 只保存环境变量名称。

凭证变量：`CHATBI_DORIS_PASSWORD`、`CHATBI_GBASE8A_PASSWORD`、`CHATBI_PG_DSN`。
经现有 secret 管理注入 **terminal 子进程实际环境**；可使用容器专用 env_file（宿主文件仅部署用户可读），
不要把 secret 文件提交到 skill/Git。PG DSN 可使用 `sslmode=verify-full sslrootcert=<容器CA路径>`。
活动 profile 的 .env 是否导出到 terminal 需按版本验证；不要仅凭 Hermes 能读取 .env 就假定脚本能读。
验证时仅检查变量存在，不打印值：

```bash
/opt/chatbi-ontology/bin/python -c 'import os; print({k: bool(os.environ.get(k)) for k in ("CHATBI_DORIS_PASSWORD", "CHATBI_GBASE8A_PASSWORD", "CHATBI_PG_DSN")})'
```

采样默认关闭。GBase 开启前由 DBA 验收服务端预算，再设置 server_budget_enforced；
expose_values 默认空，只输出无原始值的样本统计。无需为本 skill 扩大数据库账号写权限。
源数据库账户只读；PG 发现与发布最好使用不同角色/secret，发布仅对预建目标图授予所需权限。

## 5. 实际使用

在 Hermes `terminal` 中（用不同输出名避免覆盖）：

```bash
/opt/chatbi-ontology/bin/python /opt/data/skills/chatbi-ontology-builder/scripts/extract_database.py \
  --config /opt/data/ontology-config/doris.json --output /opt/data/ontology-work/evidence/doris.json

/opt/chatbi-ontology/bin/python /opt/data/skills/chatbi-ontology-builder/scripts/extract_database.py \
  --config /opt/data/ontology-config/gbase8a.json --output /opt/data/ontology-work/evidence/gbase8a.json

/opt/chatbi-ontology/bin/python /opt/data/skills/chatbi-ontology-builder/scripts/extract_documents.py \
  /opt/data/ontology-input/指标口径.xlsx --output /opt/data/ontology-work/evidence/documents

/opt/chatbi-ontology/bin/python /opt/data/skills/chatbi-ontology-builder/scripts/extract_repository.py \
  /opt/data/ontology-repositories/reporting --source-id reporting-code --revision HEAD \
  --include 'sql/*.sql' --include 'src/*.xml' --output /opt/data/ontology-work/evidence/code.json
```

让 Hermes 读取证据并按多源契约生成 decisions、冲突清单和 merge ledger；
再通过 assemble_bundle、validate_rdf 和业务 CQ 验证生成本体。组装器不会替模型做行业语义判断。
pg_ripple.py 的发现/空图加载方法见 [validation-and-publish.md](validation-and-publish.md)。
已有生产图升级需经目标构建验证的备份/晋升/恢复接口；脚本不覆盖已有图。

可直接给 Hermes 的任务：

> 使用 chatbi-ontology-builder。配置为 ontology-config 下的 doris.json 与 gbase8a.json，
> 文档为 ontology-input 下指定指标口径文件，代码为 ontology-repositories/reporting 的指定 commit。
> 所有脚本用 /opt/chatbi-ontology/bin/python。先采集统一证据并报告覆盖与冲突，再按现有五类本体
> 合成七文件 bundle，执行 RDF/SHACL 和业务 CQ。此次先交付离线结果，不执行发布。

## 6. 可选 MCP 路线

有现成 Doris/GBase/PG MCP 时可替代直连脚本的对应步骤；工具返回必须能重建相同来源/定位/覆盖证据。
不要假设 SPARQL MCP 自动提供数据库元数据、PG 管理或发布工具；本源码镜像预装它不等于已配置
pg-ripple endpoint。按具体服务工具清单设置 include 白名单，发布工具单独授权。
本次未新增 MCP 服务，不提供指向不存在服务的 command。

HTTP MCP 配置形状取自当前源码 `website/docs/reference/mcp-config-reference.md`：

```yaml
mcp_servers:
  ontology_reader:
    url: "https://REPLACE_WITH_EXISTING_MCP_HOST/mcp"
    headers:
      Authorization: "Bearer ${CHATBI_MCP_TOKEN}"
    enabled: true
    timeout: 120
    tools:
      include: ["REPLACE_WITH_ACTUAL_READONLY_TOOL"]
```

这段仅在已有对应 MCP 时配置；工具名必须替换为服务原生名称，不直接照抄占位符。

## 7. 上线验收

1. 在真正 terminal 环境运行依赖 import 检查，确认输入只读、工作区可写与新会话 skill 可发现。
   然后执行 `/opt/chatbi-ontology/bin/python -m unittest discover -s /opt/data/skills/chatbi-ontology-builder/tests -v`，
   完整依赖环境不应因缺包跳过 RDF/SHACL/SQL 测试；该套测试不连接生产数据库。
2. Doris/GBase 各用 1 张已知表核对列数/类型/注释/DDL，检查实际账户、TLS、引擎与连接端口。
3. 各用一个允许的时间范围与少量非敏感列采样；验证服务端超时/资源预算与无原始值默认输出。
4. 已知文档与 Git commit 抽取，核对所有 sheet/字段、公式文本、定位与 SQL 表列。
5. 合成小 bundle，运行结构+RDF+SHACL；再验证中文别名、指标口径、场景路径与物理映射 CQ。
6. 对测试 PG18 图发现签名、事务加载、失败回滚、重复加载拒绝、角色隔离；运行在线 CQ。
7. 保存镜像 digest、依赖版本、来源快照、bundle/release 哈希及验收结果，再安排生产升级。

本地语法/单元测试不替代这些连接测试；没有生产连接凭证时不得写“Doris/GBase/PG 已联调通过”。
