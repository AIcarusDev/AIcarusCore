# src/database/core/typedb_connection_manager.py
from __future__ import annotations

import asyncio

from src.common.custom_logging.logging_config import get_logger
from src.config import config

# ++ Correction Here: Changed TypeDBOptions to DriverOptions ++
from typedb.driver import Credentials, Driver, DriverOptions, TypeDB

logger = get_logger(__name__)

class TypeDBConnectionManager:
    """管理与 TypeDB 的连接和生命周期 (基于 gRPC Driver)."""

    _driver: Driver | None = None
    _database_name: str = ""
    _lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls) -> TypeDBConnectionManager:
        """获取连接管理器的单例实例，如果不存在则创建."""
        async with cls._lock:
            if cls._driver is None or not cls._driver.is_open():
                logger.info("TypeDB driver 实例不存在或已关闭，正在创建新实例...")
                db_config = config.database

                # TypeDB gRPC 默认端口是 1729
                address = db_config.host.replace("http://", "").replace("https://", "")
                if ":" not in address:
                    address = f"{address}:1729"

                credentials = Credentials(username=db_config.username, password=db_config.password)

                # 根据你的环境调整 tls_enabled。本地开发通常为 False。
                options = DriverOptions(tls_enabled=False)

                logger.info(f"正在连接到 TypeDB 服务器: {address}...")
                cls._driver = TypeDB.driver(address, credentials, options)
                cls._database_name = db_config.database_name

                if not await asyncio.to_thread(cls._driver.databases.contains, cls._database_name):
                    logger.info(f"数据库 '{cls._database_name}' 不存在，正在创建...")
                    await asyncio.to_thread(cls._driver.databases.create, cls._database_name)
                    logger.info(f"数据库 '{cls._database_name}' 创建成功。")

                logger.info(
                    f"TypeDBConnectionManager 初始化成功，目标数据库: '{cls._database_name}'。"
                    )

            return cls(cls._driver, cls._database_name)

    def __init__(self, driver: Driver, database_name: str) -> None:
        """初始化连接管理器."""
        self._driver = driver
        self._database_name = database_name

    def get_driver(self) -> Driver:
        """获取 TypeDB 驱动实例."""
        if not self._driver or not self._driver.is_open():
            raise ConnectionError("TypeDB driver 未连接或已关闭。")
        return self._driver

    def get_database_name(self) -> str:
        """获取数据库名称."""
        return self._database_name

    async def close(self) -> None:
        """关闭驱动连接."""
        async with self.__class__._lock:
            if self._driver and self._driver.is_open():
                await asyncio.to_thread(self._driver.close)
                logger.info("TypeDB driver 连接已关闭。")
                self.__class__._driver = None
