"""
MySQL 数据库查询工具模块

封装数据库查询助手使用的三个 LangChain 工具：
list_sql_tables 用于发现真实表名，get_table_data 用于预览字段和样例数据，
execute_sql_query 用于在确认结构后执行自定义查询。

与旧版本的差别：三个工具全部接入 `app/services/sql_guard.py` 的四道闸门
（只读限制、表白名单、查询超时、结果行数上限），并且每次查询都会写审计日志。
业务 MySQL 应配合只读账号使用，形成「应用层 + 数据库层」双重保险。
"""

import os
import uuid

from dotenv import load_dotenv
from langchain_core.tools import tool
from mysql.connector import Error, connect

from app import config
from app.api.context import get_user_context
from app.api.monitor import monitor
from app.services.audit import AuditAction, record_audit_sync
from app.services.sql_guard import (
    SqlGuardError,
    apply_session_guards,
    refresh_allowlist,
    truncate_notice,
    validate_readonly,
    validate_sql,
    validate_tables,
)

load_dotenv()


# 集中读取数据库配置，后续三个工具都复用这份连接参数
def get_db_config():
    """
    从环境变量读取 MySQL 连接配置

    所有数据库工具都通过此函数拿到同一份连接参数，避免每个工具重复读取环境变量
    :return: mysql.connector.connect 可直接使用的连接参数
    """
    config_map = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "collation": os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci"),
        "autocommit": True,
        "sql_mode": os.getenv("MYSQL_SQL_MODE", "TRADITIONAL"),
    }

    # 去掉未配置的可选项，避免把 None 传给 mysql.connector 造成连接参数异常
    config_map = {k: v for k, v in config_map.items() if v is not None}

    # user/password/database 是本工具能正常查询业务库的最小必要配置
    required_keys = ["user", "password", "database"]
    missing_keys = [k for k in required_keys if k not in config_map]
    if missing_keys:
        raise ValueError(f"缺失数据库核心配置：{', '.join(missing_keys)}")

    return config_map


def _audit_sql(sql: str, row_count: int, error: str = "") -> None:
    """
    记录一次 SQL 执行。

    审计必须带上完整 SQL 原文：事后追溯「谁查了哪些数据」是这套系统上线的硬要求。
    """
    raw_user_id = get_user_context()
    user_id = None
    if raw_user_id:
        try:
            user_id = uuid.UUID(str(raw_user_id))
        except (ValueError, TypeError):
            user_id = None

    record_audit_sync(
        action=AuditAction.SQL_QUERY,
        user_id=user_id,
        resource_type="sql",
        detail={
            "sql": sql,
            "row_count": row_count,
            "error": error,
            "database": os.getenv("MYSQL_DATABASE", ""),
        },
    )


def _rows_to_csv(columns: list[str], rows: list[tuple]) -> str:
    """把查询结果拼成 CSV 文本：首行列名，其后每行一条记录。"""
    header = ",".join(columns)
    body = "\n".join(",".join(map(str, row)) for row in rows)
    return f"{header}\n{body}" if body else header


@tool
def list_sql_tables() -> str:
    """
    查询当前数据库中所有可用表

    作用：让模型先识别真实可用的表名，方便后续预览表结构和编写自定义 SQL。
    只会返回配置在白名单中的表，不在白名单的表对模型不可见。
    :return: 有表：可用的表有：表1,表2,表3...
             没有表：没有可用的表
             出现异常：查询出现异常：异常信息
    """
    # 埋点：工具一被调用，前端可以展示当前正在查询数据库表名
    monitor.report_tool(tool_name="数据库表名查询工具：list_sql_tables", args={})

    try:
        allowed = refresh_allowlist()
    except Exception as e:
        return f"查询出现异常：读取表白名单失败 {str(e)}"

    if not allowed:
        return "没有可用的表"

    # MySQL 查询的固定步骤：
    # 1. 创建连接
    # 2. 创建 cursor
    # 3. 执行 SQL
    # 4. 获取返回结果
    # 5. 释放连接和 cursor 资源
    # 这里捕获异常并返回中文提示，避免工具报错直接中断 Agent 执行链路
    try:
        with connect(**get_db_config()) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                # SHOW TABLES 返回形如：[("drugs",), ("inventory",), ("sales_records",)]
                tables = cursor.fetchall()
                if not tables:
                    return "没有可用的表"

                # 取每个元组的第一个元素，与白名单取交集后返回
                visible = sorted(
                    table[0] for table in tables if str(table[0]).lower() in allowed
                )
                if not visible:
                    return "没有可用的表"
                return f"可用的表有：{', '.join(visible)}"
    except Error as e:
        return f"查询出现异常：{str(e)}"


@tool
def get_table_data(table_name) -> str:
    """
    查询指定表的前 100 行数据

    当前工具调用之前，应先调用 list_sql_tables 完成表名校验。
    此工具的作用：
    1. 完成单表样例数据查询
    2. 为多表查询提供表结构信息和数据格式参考
    :param table_name: 表名（必须在白名单内）
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             4. 至多查询 100 条表数据
    """
    # 埋点：工具二被调用，前端可以展示当前正在预览哪张表
    monitor.report_tool(
        tool_name="数据库表数据查询工具：get_table_data",
        args={"table_name": table_name},
    )

    # 白名单校验放在拼接 SQL 之前，避免任何形式的表名注入
    safe_table_name = str(table_name).replace("`", "").replace(";", "").split()[0]
    sql = f"SELECT * FROM `{safe_table_name}` LIMIT 100"

    try:
        validate_tables(sql)
    except SqlGuardError as e:
        _audit_sql(sql, 0, error=str(e))
        return f"查询被拒绝：{e}"

    # 查询流程同样是：连接 -> cursor -> 执行 SQL -> 获取列信息和数据 -> 自动释放资源
    try:
        with connect(**get_db_config()) as conn:
            with conn.cursor() as cursor:
                # 连接级超时与行数上限：即使 SQL 写得很糟也不会拖垮业务库
                apply_session_guards(cursor)
                cursor.execute(sql)

                # cursor.description 保存查询结果的列元信息
                # 例如：[("id", ...), ("name", ...), ("age", ...)]
                # 如果 SQL 没有结果集，description 可能为 None
                description = cursor.description
                if not description:
                    _audit_sql(sql, 0)
                    return f"数据表 {table_name} 暂无数据。"

                columns = [desc[0] for desc in description]
                # fetchall 返回表数据，形如：[(1, "张三", 18), (2, "李四", 20)]
                rows = cursor.fetchall()
                _audit_sql(sql, len(rows))
                return _rows_to_csv(columns, rows)
    except Error as e:
        _audit_sql(sql, 0, error=str(e))
        return f"查询出现异常：{str(e)}"


@tool
def execute_sql_query(query) -> str:
    """
    执行自定义 SQL 查询

    切记：执行之前，需要通过 list_sql_tables 明确真实表名，
    再通过 get_table_data 明确表结构和数据格式。
    适合多表关联、筛选、聚合、排序等复杂查询。
    只允许只读查询（SELECT/SHOW/DESCRIBE/EXPLAIN），且只能访问白名单内的表。
    :param query: 要执行的自定义 SQL 语句
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
    """
    # 埋点：记录模型最终生成的 SQL，便于观察是否真的落到了正确表字段上
    monitor.report_tool(
        tool_name="数据库表数据查询工具：execute_sql_query", args={"query": query}
    )

    # 四道闸门的第一、二道：只读校验 + 表白名单校验，都在执行前完成
    try:
        validate_sql(query)
    except SqlGuardError as e:
        _audit_sql(query, 0, error=str(e))
        return f"查询被拒绝：{e}"

    # 自定义查询和 get_table_data 的结果处理逻辑一致：
    # 执行 SQL -> 读取 description 得到列名 -> fetchall 得到数据 -> 拼成 CSV 返回
    try:
        with connect(**get_db_config()) as conn:
            with conn.cursor() as cursor:
                # 第三、四道闸门：查询超时 + 结果行数上限
                apply_session_guards(cursor)
                cursor.execute(query)

                # 非查询类 SQL 没有结果集描述，这里统一返回提示，避免工具调用直接抛错给模型
                description = cursor.description
                if not description:
                    _audit_sql(query, 0)
                    return f"执行自定义 SQL 语句没有查询结果，SQL 为：{query}"

                columns = [desc[0] for desc in description]
                rows = cursor.fetchall()
                _audit_sql(query, len(rows))

                # 触顶时明确告知模型「数据可能不全」，避免它把截断结果当成全量结论
                return _rows_to_csv(columns, rows) + truncate_notice(len(rows))
    except Error as e:
        _audit_sql(query, 0, error=str(e))
        return f"查询出现异常：{str(e)}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证 .env 中的 MySQL 连接配置与安全闸门
    print(list_sql_tables.invoke({}))
    print("----")
    print(
        execute_sql_query.invoke(
            {
                "query": "SELECT * FROM `drugs` dgs join sales_records srd on dgs.drug_id = srd.drug_id"
            }
        )[:400]
    )
    print("---- 应被拒绝 ----")
    print(execute_sql_query.invoke({"query": "DROP TABLE drugs"}))
    print(f"SQL 行数上限配置：{config.SQL_MAX_ROWS}，超时：{config.SQL_QUERY_TIMEOUT_MS}ms")
    print(f"只读校验示例：{validate_readonly.__name__}")
