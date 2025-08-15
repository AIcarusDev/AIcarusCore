# tests/database_typedb/test_query_syntax.py
import asyncio
import time
from pathlib import Path

import pytest
from typedb.driver import (
    TypeDB,
    TypeDBDriverException,
    Credentials,
    DriverOptions,
    TransactionType,
)

# --- 真实数据库配置 ---
ADDRESS = "localhost:1729"
DATABASE_NAME = "syntax_validation_db_prod_schema" # 使用新名称
USERNAME = "admin"
PASSWORD = "password"

# --- [修正]: 从项目文件加载真实的 Schema ---
# 假设测试是从项目根目录 (AIcarusCore) 运行的
SCHEMA_PATH = Path("src/database/schema.tql")
if not SCHEMA_PATH.exists():
    raise FileNotFoundError(f"无法找到真实的 schema 文件，请确保测试从项目根目录运行: {SCHEMA_PATH.absolute()}")
SCHEMA_CONTENT = SCHEMA_PATH.read_text(encoding="utf-8")
# --- [修正结束] ---

# --- 测试数据 (与你的 schema.tql 兼容) ---
DATA = """
insert
  $p isa platform, has platform-uid "qq", has display-name "QQ";
  $s isa sticker,
    has sticker-uid "qq_sticker_001",
    has sticker-id "001",
    has filename "smile.gif",
    has impression "happy",
    has image-hash "hash123",
    has perceptual-hash "phash123",
    has added-at 1678886400;
  (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
"""

# --- 错误顺序的查询 (预期失败) ---
QUERY_FAILING_ORDER = """
match
    platform-asset(hosting-platform: $p, hosted-asset: $s);
    $p isa platform, has platform-uid $puid;
    $s isa sticker;
select $puid; distinct;
"""

# --- 正确顺序的查询 (预期成功) ---
QUERY_CORRECT_ORDER = """
match
    $p isa platform, has platform-uid $puid;
    $s isa sticker;
    platform-asset(hosting-platform: $p, hosted-asset: $s);
select $puid; distinct;
"""

@pytest.fixture(scope="module")
async def db_connection():
    """一个模块级的 fixture，负责建立和销毁数据库连接及测试数据."""
    driver = TypeDB.driver(ADDRESS, Credentials(USERNAME, PASSWORD), DriverOptions(is_tls_enabled=False))
    if driver.databases.contains(DATABASE_NAME):
        driver.databases.get(DATABASE_NAME).delete()
    driver.databases.create(DATABASE_NAME)

    # 使用从文件加载的 Schema
    with driver.transaction(DATABASE_NAME, TransactionType.SCHEMA) as tx:
        tx.query(SCHEMA_CONTENT).resolve()
        tx.commit()

    with driver.transaction(DATABASE_NAME, TransactionType.WRITE) as tx:
        tx.query(DATA).resolve()
        tx.commit()

    yield driver

    # 清理
    if driver.is_open():
        if driver.databases.contains(DATABASE_NAME):
            driver.databases.get(DATABASE_NAME).delete()
        driver.close()

@pytest.mark.asyncio
async def test_query_with_relation_first_fails(db_connection):
    """验证关系在前、类型在后的查询会失败."""
    with pytest.raises(TypeDBDriverException) as excinfo:
        with db_connection.transaction(DATABASE_NAME, TransactionType.READ) as tx:
            # 使用 list() 来消耗迭代器并触发查询
            list(tx.query(QUERY_FAILING_ORDER).resolve().as_concept_rows())

    assert "[INF11]" in str(excinfo.value)
    print(f"\n预期的失败已捕获: {excinfo.value}")

@pytest.mark.asyncio
async def test_query_with_type_first_succeeds(db_connection):
    """验证类型在前、关系在后的查询会成功."""
    results = []
    try:
        with db_connection.transaction(DATABASE_NAME, TransactionType.READ) as tx:
            results = list(tx.query(QUERY_CORRECT_ORDER).resolve().as_concept_rows())
    except TypeDBDriverException as e:
        pytest.fail(f"正确的查询顺序不应失败，但却抛出了异常: {e}")

    assert len(results) == 1
    puid_concept = results[0].get("puid")
    assert puid_concept is not None
    puid = puid_concept.as_attribute().get_value().get_string()
    assert puid == "qq"
    print("\n正确的查询顺序成功执行并返回了预期的结果。")