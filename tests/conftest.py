import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.services import (
    ActionLogStorageService,
    EntityGraphService,
    EventStorageService,
    GoalStorageService,
    ImageAnalysisCacheService,
    StickerStorageService,
    SummaryStorageService,
    ThoughtStorageService,
)
from typedb.driver import (
    Credentials,
    Driver,
    DriverOptions,
    TransactionType,
    TypeDB,
)

# --- 数据库配置 ---
ADDRESS = "localhost:1729"
DATABASE_NAME = "aicarus_test_db"
USERNAME = "admin"
PASSWORD = "password"
SCHEMA_PATH = Path("src/database/schema.tql")

# --- Pytest Fixtures ---

# [FIXED] 移除了已废弃的自定义 event_loop fixture。
# pytest-asyncio 会自动为 session 范围的 async fixture 提供事件循环。


@pytest.fixture(scope="session")
def db_driver() -> Generator[Driver, None, None]:
    """一个会话级的 fixture，负责建立和销毁数据库连接."""
    print(f"\nConnecting to TypeDB at {ADDRESS}...")
    driver = TypeDB.driver(
        ADDRESS, Credentials(USERNAME, PASSWORD), DriverOptions(is_tls_enabled=False)
    )
    yield driver
    print("\nClosing TypeDB driver...")
    driver.close()


@pytest.fixture(scope="function")
async def db_connection(db_driver: Driver) -> AsyncGenerator[Driver, None]:
    """一个函数级的 fixture，确保每个测试函数都有一个干净的数据库."""
    print(f"Preparing clean database '{DATABASE_NAME}'...")
    if await asyncio.to_thread(db_driver.databases.contains, DATABASE_NAME):
        await asyncio.to_thread(db_driver.databases.get(DATABASE_NAME).delete)

    await asyncio.to_thread(db_driver.databases.create, DATABASE_NAME)

    # 加载 Schema
    schema_content = SCHEMA_PATH.read_text(encoding="utf-8")
    with db_driver.transaction(DATABASE_NAME, TransactionType.SCHEMA) as tx:
        tx.query(schema_content).resolve()
        tx.commit()

    yield db_driver

    # 测试函数执行后清理
    if db_driver.is_open() and await asyncio.to_thread(db_driver.databases.contains, DATABASE_NAME):
        await asyncio.to_thread(db_driver.databases.get(DATABASE_NAME).delete)
    print(f"Cleaned up database '{DATABASE_NAME}'.")


@pytest.fixture(scope="function")
async def conn_manager(db_connection: Driver) -> TypeDBConnectionManager:
    """提供一个已连接的 TypeDBConnectionManager 实例."""
    # 模拟 get_instance 的行为，直接使用已有的连接
    manager = TypeDBConnectionManager(db_connection, DATABASE_NAME)
    return manager


# --- Service Fixtures ---


@pytest.fixture(scope="function")
def event_storage_service(conn_manager: TypeDBConnectionManager) -> EventStorageService:
    """提供一个 EventStorageService 实例."""
    return EventStorageService(conn_manager)


@pytest.fixture(scope="function")
def entity_graph_service(
    conn_manager: TypeDBConnectionManager, event_storage_service: EventStorageService
) -> EntityGraphService:
    """提供一个 EntityGraphService 实例."""
    service = EntityGraphService(conn_manager, event_storage_service)
    event_storage_service.set_entity_graph_service(service)  # 处理循环依赖注入
    return service


@pytest.fixture(scope="function")
def action_log_storage_service(conn_manager: TypeDBConnectionManager) -> ActionLogStorageService:
    """提供一个 ActionLogStorageService 实例."""
    return ActionLogStorageService(conn_manager)


@pytest.fixture(scope="function")
def goal_storage_service(conn_manager: TypeDBConnectionManager) -> GoalStorageService:
    """提供一个 GoalStorageService 实例."""
    return GoalStorageService(conn_manager)


@pytest.fixture(scope="function")
def image_analysis_cache_service(
    conn_manager: TypeDBConnectionManager,
) -> ImageAnalysisCacheService:
    """提供一个 ImageAnalysisCacheService 实例."""
    return ImageAnalysisCacheService(conn_manager)


@pytest.fixture(scope="function")
def sticker_storage_service(conn_manager: TypeDBConnectionManager) -> StickerStorageService:
    """提供一个 StickerStorageService 实例."""
    return StickerStorageService(conn_manager)


@pytest.fixture(scope="function")
def summary_storage_service(conn_manager: TypeDBConnectionManager) -> SummaryStorageService:
    """提供一个 SummaryStorageService 实例."""
    return SummaryStorageService(conn_manager)


@pytest.fixture(scope="function")
def thought_storage_service(conn_manager: TypeDBConnectionManager) -> ThoughtStorageService:
    """提供一个 ThoughtStorageService 实例."""
    return ThoughtStorageService(conn_manager)
