"""
SQL 安全防护

业务 MySQL 是公司共享数据，只允许「读」，且只允许读白名单里的表。四道闸门：

1. **只读**：仅放行 SELECT / SHOW / DESCRIBE / EXPLAIN（含 CTE 的 SELECT），
   拦截 INSERT/UPDATE/DELETE/DROP/ALTER/... 以及多语句；
2. **白名单**：用 sqlparse 解析出 SQL 涉及的每张表，逐一比对
   `sql_table_allowlist`（空表时按需用业务库现有表名播种）；
3. **超时**：连接级 MAX_EXECUTION_TIME，避免慢查询拖垮业务库；
4. **行数上限**：连接级 sql_select_limit，结果集不会无限大。

所有拦截都返回中文原因，直接回给模型，让它自己换一条合法 SQL 重试。
"""

import time
from typing import Optional

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis, TokenList
from sqlparse.tokens import DML, Keyword, Name

from app import config

# 只读语句允许的起始关键字；WITH 用于 CTE，需要额外确认内部是 SELECT
_ALLOWED_STATEMENT_TYPES = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}

# 即使藏在子查询里也必须拦截的高危关键字
_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "GRANT", "REVOKE", "RENAME", "MERGE", "CALL", "EXECUTE",
    "PREPARE", "DEALLOCATE", "LOCK", "UNLOCK", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "SET", "USE", "LOAD", "HANDLER", "INTO",
}

# 这些是函数名而非关键字（sqlparse 会把它们解析成 Name），需要单独扫描：
# SLEEP/BENCHMARK 可被用来做时间盲注或拖慢数据库，LOAD_FILE/OUTFILE 涉及文件读写
_FORBIDDEN_FUNCTION_NAMES = {
    "SLEEP", "BENCHMARK", "LOAD_FILE", "OUTFILE", "DUMPFILE", "GET_LOCK",
}

# FROM 之后出现的这些关键字表示表名列表已经结束
_TABLE_LIST_TERMINATORS = {
    "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "UNION", "EXCEPT",
    "INTERSECT", "WINDOW", "FOR", "PROCEDURE", "INTO", "SET", "VALUES",
    "ON", "USING", "AS",
}

# 收集表名时关注的前置关键字
_TABLE_INTRODUCERS = {"FROM", "JOIN", "INTO", "UPDATE", "TABLE"}


class SqlGuardError(Exception):
    """SQL 未通过安全校验；消息会原样回给模型作为纠正提示。"""


def _normalize(token) -> str:
    return str(token.value).strip().upper()


def _is_keyword(ttype) -> bool:
    """
    判断 sqlparse 的 token 类型是不是关键字（含子类型）。

    sqlparse 的类型是树状结构：`Keyword.DDL`、`Keyword.DML`、`Keyword.CTE` 都是
    `Keyword` 的子类型，但 `ttype in (DML, Keyword)` / `ttype == Keyword` 走的是
    **等值**比较，匹配不到子类型。必须用 `in` 运算符——sqlparse 的 `_TokenType`
    实现了 `__contains__` 专门做子类型判断。

    这个细节直接决定安全性：`DROP`/`ALTER`/`CREATE`/`TRUNCATE` 的 ttype 都是
    `Keyword.DDL`，用等值比较会同时漏掉两处扫描：
    1. 语句类型判断取不到真正的首个关键字（`DROP TABLE drugs` 会被误报成
       「检测到：TABLE」，而 `EXPLAIN DROP TABLE drugs` 更会因首个关键字是
       允许的 EXPLAIN 而**直接通过只读校验**）；
    2. `_FORBIDDEN_KEYWORDS` 兜底扫描扫不到 DROP，尽管 DROP 就在集合里。
    """
    return ttype is not None and ttype in Keyword


def validate_readonly(sql: str) -> None:
    """
    校验 SQL 是单条只读语句。

    :raises SqlGuardError: 多语句、非只读语句或包含高危关键字
    """
    if not sql or not sql.strip():
        raise SqlGuardError("SQL 为空，请提供一条查询语句")

    statements = [s for s in sqlparse.split(sql) if s.strip()]
    if len(statements) > 1:
        raise SqlGuardError("禁止一次提交多条 SQL 语句，请拆分成单条查询")
    if not statements:
        raise SqlGuardError("SQL 为空，请提供一条查询语句")

    statement = sqlparse.parse(statements[0])[0]

    # 语句类型判断：取第一个有意义的 DML/DDL 关键字
    first_keyword = None
    for token in statement.tokens:
        if token.is_whitespace:
            continue
        if _is_keyword(token.ttype):
            first_keyword = _normalize(token)
            break

    if first_keyword not in _ALLOWED_STATEMENT_TYPES:
        raise SqlGuardError(
            f"仅允许只读查询（SELECT/SHOW/DESCRIBE/EXPLAIN），检测到：{first_keyword or '未知语句'}"
        )

    # 关键字级别的兜底扫描：拦截藏在子查询/CTE 里的写操作
    for token in statement.flatten():
        if _is_keyword(token.ttype):
            value = _normalize(token)
            if value in _FORBIDDEN_KEYWORDS:
                raise SqlGuardError(f"SQL 中包含禁止的关键字：{value}")
        # 函数名（如 SLEEP、LOAD_FILE）在 sqlparse 里是 Name 而非 Keyword，
        # 这里只扫 Name 类型，避免把字符串字面量里的同名词误判成函数调用
        elif token.ttype in Name and _normalize(token) in _FORBIDDEN_FUNCTION_NAMES:
            raise SqlGuardError(f"SQL 中包含禁止的函数：{_normalize(token)}")

    # 注释里常被用来藏第二个语句，这里直接拒绝注释，保持规则简单可解释
    if "--" in sql or "/*" in sql:
        raise SqlGuardError("SQL 中不允许包含注释")


def _identifier_table_name(identifier) -> Optional[str]:
    """
    从 sqlparse 的 Identifier 节点中取出真实表名。

    会剥掉别名（`sales_records s` -> `sales_records`），但保留库名前缀
    （`pharma_db.drugs` -> `pharma_db.drugs`），因为 `information_schema.tables`
    这类跨库访问必须能被识别出来单独拒绝。
    """
    if not isinstance(identifier, Identifier):
        return None
    # 函数调用（如 COUNT(*)）不是表名
    if identifier.get_real_name() is None:
        return None
    if identifier.token_first(skip_cm=True).ttype is Keyword:
        return None

    real_name = identifier.get_real_name()
    if not real_name:
        return None

    parent_name = identifier.get_parent_name()
    if parent_name:
        return f"{parent_name.lower()}.{real_name.lower()}"
    return real_name.lower()


def _handle_table_node(node, tables: set[str]) -> bool:
    """
    处理 FROM/JOIN 后面的一个表表达式。

    :return: True 表示已把该节点处理完（调用方可以结束本轮 expecting_table）
    """
    if isinstance(node, IdentifierList):
        for identifier in node.get_identifiers():
            _handle_table_node(identifier, tables)
        return True

    if isinstance(node, Identifier):
        first = node.token_first(skip_cm=True)
        # `FROM (SELECT ...) t` 会被解析成一个 Identifier（别名是 t），
        # 真实来源在被括号包住的子查询里，必须下钻，且不能把别名 t 当成表名
        if isinstance(first, Parenthesis):
            _collect_tables(first, tables)
            return True
        name = _identifier_table_name(node)
        if name:
            tables.add(name)
        # 单表后面可能直接跟 JOIN，继续等待下一个表名
        return False

    if isinstance(node, Parenthesis):
        _collect_tables(node, tables)
        return True

    return True


def _collect_tables(parsed: TokenList, tables: set[str]) -> None:
    """递归遍历语法树，收集 FROM/JOIN/UPDATE/INTO 后面的表名。"""
    expecting_table = False

    for token in parsed.tokens:
        if token.is_whitespace:
            continue

        if expecting_table:
            if token.ttype in Keyword and _normalize(token) in _TABLE_LIST_TERMINATORS:
                expecting_table = False
            elif token.ttype in Keyword:
                # 形如 `FROM a JOIN b`：JOIN 通过下面的 _TABLE_INTRODUCERS 重新置位
                expecting_table = False
            else:
                if _handle_table_node(token, tables):
                    expecting_table = False

        if token.ttype in Keyword:
            value = _normalize(token)
            # LEFT JOIN / RIGHT JOIN / INNER JOIN 会被 sqlparse 合成一个关键字，
            # 因此用 endswith 判断，避免漏掉带修饰词的连接
            if value in _TABLE_INTRODUCERS or value.endswith("JOIN"):
                expecting_table = True
            continue

        # 子查询出现在任意位置都要继续下钻
        if isinstance(token, Parenthesis):
            _collect_tables(token, tables)


def _recurse_parentheses(node, tables: set[str]) -> None:
    """
    在任意语法节点中下钻所有括号子查询。

    `WITH stats AS (SELECT ...)` 里，括号被包在 Identifier 内部而不是顶层，
    所以必须递归遍历而不是只看当前层。
    """
    if isinstance(node, Parenthesis):
        _collect_tables(node, tables)
        return
    if isinstance(node, TokenList):
        for child in node.tokens:
            _recurse_parentheses(child, tables)


def _collect_cte_names(parsed: TokenList, tables: set[str]) -> set[str]:
    """
    处理 WITH 子句：返回 CTE 别名集合，并顺带收集 CTE 内部引用的真实表。

    CTE 名是查询内部临时视图，不是真实表，不能拿去做白名单校验，
    否则 `WITH stats AS (...) SELECT * FROM stats` 里的 stats 会被误拦。
    """
    names: set[str] = set()
    tokens = list(parsed.tokens)

    start = None
    for index, token in enumerate(tokens):
        if token.ttype in Keyword and _normalize(token) == "WITH":
            start = index
            break
    if start is None:
        return names

    # WITH 段从 WITH 开始，到主查询的第一个 DML 关键字结束
    for follow in tokens[start + 1 :]:
        if follow.ttype in Keyword and _normalize(follow) in {
            "SELECT", "INSERT", "UPDATE", "DELETE",
        }:
            break
        if isinstance(follow, IdentifierList):
            for identifier in follow.get_identifiers():
                name = identifier.get_real_name()
                if name:
                    names.add(name.lower())
                _recurse_parentheses(identifier, tables)
        elif isinstance(follow, Identifier):
            name = follow.get_real_name()
            if name:
                names.add(name.lower())
            _recurse_parentheses(follow, tables)

    return names


def extract_tables(sql: str) -> set[str]:
    """解析 SQL 涉及的物理表名（已排除 CTE 别名与库名前缀）。"""
    tables: set[str] = set()
    for statement in sqlparse.parse(sql):
        cte_names = _collect_cte_names(statement, tables)
        _collect_tables(statement, tables)
        tables -= cte_names
    return tables


# --------------------------------------------------------------------------- #
# 表白名单（PostgreSQL 侧维护，进程内缓存）
# --------------------------------------------------------------------------- #
_allowlist_cache: set[str] = set()
_allowlist_cached_at: float = 0.0


def _load_allowlist_from_pg() -> set[str]:
    """从 PostgreSQL 读取白名单（同步 psycopg，供同步工具直接调用）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(
        config.PG_SYNC_DSN, autocommit=True, row_factory=dict_row
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT table_name FROM sql_table_allowlist")
            return {row["table_name"].lower() for row in cursor.fetchall()}


def _seed_allowlist_from_mysql(tables: set[str]) -> None:
    """白名单为空时，用业务库现有表名播种，保证首次部署开箱可用。"""
    if not tables:
        return
    import psycopg

    with psycopg.connect(config.PG_SYNC_DSN, autocommit=True) as conn:
        with conn.cursor() as cursor:
            for table in sorted(tables):
                cursor.execute(
                    "INSERT INTO sql_table_allowlist (table_name, note) "
                    "VALUES (%s, %s) ON CONFLICT (table_name) DO NOTHING",
                    (table, "自动播种"),
                )


def refresh_allowlist(force: bool = False) -> set[str]:
    """
    获取白名单（带进程内缓存）。

    缓存避免每次 SQL 查询都访问 PostgreSQL；管理员改动白名单后最多
    SQL_ALLOWLIST_CACHE_SECONDS 秒生效。
    """
    global _allowlist_cache, _allowlist_cached_at

    now = time.time()
    if (
        not force
        and _allowlist_cache
        and now - _allowlist_cached_at < config.SQL_ALLOWLIST_CACHE_SECONDS
    ):
        return _allowlist_cache

    try:
        tables = _load_allowlist_from_pg()
        if not tables and config.SQL_ALLOWLIST_AUTO_SEED:
            tables = _discover_mysql_tables()
            _seed_allowlist_from_mysql(tables)
        _allowlist_cache = tables
        _allowlist_cached_at = now
        return tables
    except Exception as exc:
        print(f"[SqlGuard] 白名单加载失败，沿用上次缓存：{exc}")
        return _allowlist_cache


def _discover_mysql_tables() -> set[str]:
    """直连业务 MySQL 取当前库的全部表名，仅用于白名单播种。"""
    from app.tools.db_tools import get_db_config

    from mysql.connector import connect

    with connect(**get_db_config()) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            return {row[0].lower() for row in cursor.fetchall()}


def _allowed_schema() -> str:
    """业务库名；只有该库下的表允许被直接访问。"""
    import os

    return os.getenv("MYSQL_DATABASE", "").strip().lower()


def validate_tables(sql: str) -> None:
    """
    校验 SQL 涉及的表都在白名单内，且没有跨库访问。

    :raises SqlGuardError: 出现未授权的表或跨库引用
    """
    tables = extract_tables(sql)
    if not tables:
        # SHOW / DESCRIBE / EXPLAIN 等语句解析不出表名，交给只读校验兜底
        return

    allowed = refresh_allowlist()
    if not allowed:
        raise SqlGuardError("SQL 表白名单为空，请联系管理员配置后重试")

    business_schema = _allowed_schema()
    unauthorized: list[str] = []

    for table in sorted(tables):
        if "." in table:
            schema, bare = table.rsplit(".", 1)
            # 显式写库名时只允许业务库本身，information_schema 等一律拒绝
            if not business_schema or schema != business_schema:
                unauthorized.append(table)
                continue
            if bare not in allowed:
                unauthorized.append(table)
            continue
        if table not in allowed:
            unauthorized.append(table)

    if unauthorized:
        raise SqlGuardError(
            f"以下表不在允许访问的白名单内：{', '.join(unauthorized)}。"
            f"可用表请通过 list_sql_tables 查询。"
        )


def validate_sql(sql: str) -> None:
    """只读 + 白名单的完整校验入口，工具层统一调用它。"""
    validate_readonly(sql)
    validate_tables(sql)


def apply_session_guards(cursor) -> None:
    """
    在连接上施加超时与行数上限。

    这两项是连接级设置，工具每次调用都会新建连接（见 db_tools），
    因此不会残留到其它请求。设置失败不致命，只打印告警。
    """
    try:
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={config.SQL_QUERY_TIMEOUT_MS}")
    except Exception as exc:
        print(f"[SqlGuard] 查询超时设置失败：{exc}")

    try:
        cursor.execute(f"SET SESSION sql_select_limit={config.SQL_MAX_ROWS}")
    except Exception as exc:
        print(f"[SqlGuard] 结果行数上限设置失败：{exc}")


def truncate_notice(row_count: int) -> str:
    """结果行数触顶时给模型的提示，提醒它收窄查询条件。"""
    if row_count >= config.SQL_MAX_ROWS:
        return (
            f"\n[提示] 结果已触达 {config.SQL_MAX_ROWS} 行上限，可能有数据被截断，"
            f"建议增加筛选条件或使用聚合后再查询。"
        )
    return ""
