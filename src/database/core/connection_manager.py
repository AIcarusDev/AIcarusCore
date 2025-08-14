# src/database/core/connection_manager.py
from __future__ import annotations

import asyncio
from pathlib import Path

from src.common.custom_logging.logging_config import get_logger
from typedb.driver import (
    Credentials,
    Driver,
    DriverOptions,
    TransactionType,
    TypeDB,
)

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent / "schema.tql"


class TypeDBConnectionManager:
    """管理与 TypeDB 的连接和生命周期 (基于 gRPC Driver v3+)."""

    _driver: Driver | None = None
    _database_name: str = ""
    _lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls, db_config: dict) -> TypeDBConnectionManager:
        """获取连接管理器的单例实例，如果不存在则创建并初始化."""
        async with cls._lock:
            if cls._driver is None or not cls._driver.is_open():
                logger.info("TypeDB driver 实例不存在或已关闭，正在创建新实例...")
                cls._database_name = db_config["database_name"]
                address = db_config["host"]
                if ":" not in address:
                    address = f"{address}:1729"

                options = DriverOptions(is_tls_enabled=False)
                credentials = Credentials(
                    username=db_config.get("username", "admin"),
                    password=db_config.get("password", "password"),
                )

                logger.info(f"正在连接到 TypeDB 服务器: {address}...")
                cls._driver = await asyncio.to_thread(TypeDB.driver, address, credentials, options)

                db_exists = await asyncio.to_thread(
                    cls._driver.databases.contains, cls._database_name
                )
                if not db_exists:
                    logger.info(f"数据库 '{cls._database_name}' 不存在，正在创建...")
                    await asyncio.to_thread(cls._driver.databases.create, cls._database_name)
                    logger.info(f"数据库 '{cls._database_name}' 创建成功。")

                await cls._define_schema_if_needed(cls._driver, cls._database_name)
                logger.info(
                    f"TypeDBConnectionManager 初始化成功，目标数据库: '{cls._database_name}'。"
                )

        return cls(cls._driver, cls._database_name)

    @classmethod
    async def _define_schema_if_needed(cls, driver: Driver, db_name: str) -> None:
        """读取 schema.tql 文件并将其加载到数据库中."""
        if not SCHEMA_PATH.exists():
            raise FileNotFoundError(f"Schema 文件未找到: {SCHEMA_PATH}")

        logger.info(f"正在为数据库 '{db_name}' 加载/验证 Schema...")
        schema_content = await asyncio.to_thread(SCHEMA_PATH.read_text, encoding="utf-8")

        def sync_define_schema() -> None:
            with driver.transaction(db_name, TransactionType.SCHEMA) as tx:
                # [修正] tx.query 是一个方法，直接接收完整的查询字符串
                tx.query(schema_content).resolve()
                tx.commit()

        await asyncio.to_thread(sync_define_schema)
        logger.info(f"Schema 已成功加载到 '{db_name}'。")

    def __init__(self, driver: Driver, database_name: str) -> None:
        self._driver = driver
        self._database_name = database_name

    def get_driver(self) -> Driver:
        """获取 TypeDB 驱动实例."""
        if not self._driver or not self._driver.is_open():
            raise ConnectionError("TypeDB driver 未连接或已关闭。")
        return self._driver

    @property
    def database_name(self) -> str:
        """获取数据库名称."""
        return self._database_name

    async def close_client(self) -> None:
        """关闭驱动连接."""
        async with self.__class__._lock:
            if self._driver and self._driver.is_open():
                await asyncio.to_thread(self._driver.close)
                logger.info("TypeDB driver 连接已关闭。")
                self.__class__._driver = None