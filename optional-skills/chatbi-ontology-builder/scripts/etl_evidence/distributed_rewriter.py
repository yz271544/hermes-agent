#!/usr/bin/env python3
"""DISTRIBUTED BY 重写器：剥离 sqlglot teradata 方言不支持的分布子句

sqlglot 遇到 `... ) DISTRIBUTED BY ('COL') COMMENT '...'` 会把整条 CREATE
降级为 Command（无表节点），walk() 返回 [] 且静默——目标表血缘丢失。
实测 39k 脚本中约 61% 的 CREATE 语句命中此问题。
"""
import re

# 表名/表定义之后的 DISTRIBUTED BY (...) 子句及其后的全部表级选项，
# 直到 AS / SELECT / ( / 语句结尾。覆盖（实测 39k 脚本中的真实形态）：
#   CREATE TABLE db.t (...) DISTRIBUTED BY ('col') COMMENT '...'
#   CREATE TABLE db.t (...)DISTRIBUTED BY('col') COMMENT='...'
#   CREATE TABLE db.t (...) ENGINE=EXPRESS ... TABLESPACE='x'COMMENT='y'   （表选项粘连，无空格）
#   CREATE TABLE db.t (...) DISTRIBUTED BY ('col') AS( SELECT ...          （AS( 无空格 CTAS）
#   CREATE TABLE db.t (...) DISTRIBUTED BY ('col') ( SELECT ...           （直接 ( 开头 CTAS）
#
# 关键点（修复前会漏的三类）：
#   1) 表选项之间的分隔符用 \s*（允许 TABLESPACE='x'COMMENT='y' 粘连），
#      且 AS/SELECT 负向断言只拦"选项名"，不拦选项值里的 AS/SELECT。
#   2) 结尾前瞻用 \b（AS( 无空格时 AS 后紧跟 ( 也能断词）并加入 (\(，
#      覆盖 AS( select / ( SELECT 这类 CTAS。
# 用前瞻（AS/(/SELECT/结尾）界定范围，避免误伤列定义内的同名字样。
RE_DISTRIBUTED = re.compile(
    r"\s*DISTRIBUTED\s+BY\s*\([^()]*\)",
    re.IGNORECASE,
)
RE_OPTION = re.compile(
    r"\s*(?:'(?:[^']|'')*'|(?!AS\b|SELECT\b)\w[\w.]*|=)",
    re.IGNORECASE,
)
RE_BOUNDARY = re.compile(r"\s*(?:AS\b|\(|SELECT\b|$)", re.IGNORECASE)


def strip_distributed(sql: str) -> str:
    """若 CREATE 带 DISTRIBUTED BY 子句，剥离后返回；否则原样返回。

    仅剥离紧跟表名/表定义后的分布子句（后接 AS/SELECT/结尾），
    不动 DDL 列定义内部文本，也不动 `DISTRIBUTE ON ROW` / `PARTITION BY` 等其他子句。
    """
    s = sql.strip().rstrip(";").strip()
    if not s.upper().startswith("CREATE"):
        return sql
    # 逐个读取表选项。旧版把含 \w+ 的选项再套 *，遇到 *# 等非法尾部时
    # 会反复拆分单词组合，产生指数级回溯；单步匹配不回溯已读选项。
    chunks = []
    previous = 0
    for match in RE_DISTRIBUTED.finditer(s):
        if match.start() < previous:
            continue
        end = match.end()
        while not RE_BOUNDARY.match(s, end):
            option = RE_OPTION.match(s, end)
            if option is None:
                break
            end = option.end()
        else:
            chunks.append(s[previous:match.start()])
            previous = end
    chunks.append(s[previous:])
    return "".join(chunks)
