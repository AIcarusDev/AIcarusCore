# 文件路径: src/database/core/connection_manager.py
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

# 我们用的是 arangoasync，所有的导入都要是它的！
from arangoasync import ArangoClient
from arangoasync.auth import Auth
from arangoasync.collection import StandardCollection
from arangoasync.database import StandardDatabase
from arangoasync.exceptions import (
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    CollectionCreateError,
    GraphCreateError,
)
from arangoasync.graph import Graph

# 哼，从你项目里导入你自己的东西
from src.common.custom_logging.logging_config import get_logger
from src.database.models import CoreDBCollections  # <-- 看！只保留这一句导入！

logger = get_logger(__name__)


class DatabaseConfigProtocol(Protocol):
    """数据库配置协议，定义了连接 ArangoDB 所需的基本属性.

    这个协议确保任何实现它的类都包含连接 ArangoDB 所需的基本信息，如主机、用户名、密码和数据库名称.

    Attributes:
        host (str): ArangoDB 的主机地址.
        username (str): 用于连接 ArangoDB 的用户名.
        password (str): 用于连接 ArangoDB 的密码.
        database_name (str): 要连接的 ArangoDB 数据库名称.
    """

    host: str
    username: str
    password: str
    database_name: str


class ArangoDBConnectionManager:
    """ArangoDB 连接管理器，用于管理与 ArangoDB 的连接和操作.

    这个类负责创建和维护与 ArangoDB 的连接，确保核心集合和图的存在，并提供查询执行功能.

    Attributes:
        client (ArangoClient): ArangoDB 客户端实例，用于连接和操作 ArangoDB.
        db (StandardDatabase): ArangoDB 数据库实例，表示当前连接的数据库.
        core_collection_configs (dict[str, list[tuple[list[str], bool, bool]]]): 核心集合配置，
            包含集合名称和索引定义的映射.
        main_graph (Graph | None): 主关系图实例，如果未创建则为 None.
        thought_graph (Graph | None): 思想图实例，如果未创建则为 None.
    """

    def __init__(
        self,
        client: ArangoClient,
        db: StandardDatabase,
        core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]],
    ) -> None:
        self.client: ArangoClient = client
        self.db: StandardDatabase = db
        self.core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]] = (
            core_collection_configs
        )
        self.main_graph: Graph | None = None
        self.thought_graph: Graph | None = None
        logger.debug(f"ArangoDBConnectionManager 已使用数据库 '{db.name}' 初始化。")

    @classmethod
    async def create_from_config(
        cls,
        database_config_obj: DatabaseConfigProtocol,
        core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]],
    ) -> "ArangoDBConnectionManager":
        """从配置对象创建 ArangoDB 连接管理器实例.

        Args:
            database_config_obj (DatabaseConfigProtocol): 包含连接信息的配置对象.
            core_collection_configs (dict[str, list[tuple[list[str], bool, bool]]]): 核心集合配置.

        Returns:
            ArangoDBConnectionManager: 已连接到 ArangoDB 的管理器实例.
        """
        host = (
            getattr(database_config_obj, "host", None)
            or getattr(database_config_obj, "url", None)
            or getattr(database_config_obj, "arangodb_host", None)
            or os.getenv("ARANGODB_HOST")
        )
        username = (
            getattr(database_config_obj, "username", None)
            or getattr(database_config_obj, "user", None)
            or getattr(database_config_obj, "arangodb_user", None)
            or os.getenv("ARANGODB_USER")
        )
        password = (
            getattr(database_config_obj, "password", None)
            or getattr(database_config_obj, "arangodb_password", None)
            or os.getenv("ARANGODB_PASSWORD")
        )
        database_name = (
            getattr(database_config_obj, "name", None)
            or getattr(database_config_obj, "database_name", None)
            or getattr(database_config_obj, "arangodb_database", None)
            or os.getenv("ARANGODB_DATABASE")
        )

        if not all([host, database_name]):
            message = "错误：ArangoDB 连接所需的必要参数未完全设置。"
            logger.critical(message)
            raise ValueError(message)

        try:
            logger.debug(f"正在尝试使用 arangoasync 连接到 ArangoDB 主机: {host}")
            client_instance = ArangoClient(hosts=host)
            auth_credentials = Auth(username=username, password=password)
            sys_db: StandardDatabase = await client_instance.db("_system", auth=auth_credentials)
            if not await sys_db.has_database(database_name):
                await sys_db.create_database(database_name)
            db_instance: StandardDatabase = await client_instance.db(
                database_name, auth=auth_credentials
            )
            await db_instance.properties()
            logger.debug(
                f"已成功连接到 ArangoDB！主机: {host}, 数据库: {database_name} (使用 arangoasync)"
            )
            manager_instance = cls(client_instance, db_instance, core_collection_configs)
            await manager_instance.ensure_core_infrastructure()
            return manager_instance
        except (ArangoServerError, ArangoClientError) as e:
            message = f"建立 ArangoDB (arangoasync) 连接时出错: {e}"
            logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:
            message = f"连接 ArangoDB (arangoasync) 期间发生未知错误: {e}"
            logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e

    async def get_collection(self, name: str, is_edge: bool = False) -> StandardCollection:
        """获取指定名称的集合，如果不存在则创建它.

        Args:
            name (str): 集合的名称.
            is_edge (bool): 如果为 True，则表示这是一个边集合，否则为文档集合.

        Returns:
            StandardCollection: 确保存在的集合实例.
        """
        if is_edge:
            if name in {CoreDBCollections.HAS_ACCOUNT, CoreDBCollections.PARTICIPATES_IN}:
                if not self.main_graph:
                    raise RuntimeError("主关系图未初始化，无法获取边集合！")
                return self.main_graph.edge_collection(name)
            elif name in {CoreDBCollections.PRECEDES_THOUGHT, CoreDBCollections.LEADS_TO_ACTION}:
                if not self.thought_graph:
                    raise RuntimeError("思想图未初始化，无法获取边集合！")
                return self.thought_graph.edge_collection(name)
            else:
                raise ValueError(f"未知的边集合 '{name}' 或其所属图未初始化。")

        index_definitions = self.core_collection_configs.get(name)
        return await self.ensure_collection_with_indexes(name, index_definitions, is_edge=False)

    async def ensure_core_infrastructure(self) -> None:
        """确保核心基础设施存在，包括集合和图."""
        logger.debug("正在检查数据库基础设施 (集合与图)...")
        if not self.core_collection_configs:
            logger.warning("未提供核心集合配置，跳过基础设施检查。")
            return

        all_collections_to_ensure = CoreDBCollections.get_all_collection_names()

        for collection_name in all_collections_to_ensure:
            is_edge = collection_name in CoreDBCollections.get_edge_collection_names()
            index_definitions = self.core_collection_configs.get(collection_name)
            await self.ensure_collection_with_indexes(
                collection_name, index_definitions, is_edge=is_edge
            )

        # ---- 创建主关系图 (person_relation_graph) ----
        main_graph_name = CoreDBCollections.MAIN_GRAPH_NAME
        if not await self.db.has_graph(main_graph_name):
            logger.info(f"主关系图 '{main_graph_name}' 不存在，正在创建...")
            main_edge_definitions = [
                {
                    "collection": CoreDBCollections.HAS_ACCOUNT,
                    "from": [CoreDBCollections.PERSONS],
                    "to": [CoreDBCollections.ACCOUNTS],
                },
                {
                    "collection": CoreDBCollections.PARTICIPATES_IN,
                    "from": [CoreDBCollections.ACCOUNTS],
                    "to": [CoreDBCollections.CONVERSATIONS],
                },
            ]
            try:
                self.main_graph = await self.db.create_graph(
                    main_graph_name, edge_definitions=main_edge_definitions
                )
            except GraphCreateError as e:
                logger.error(f"创建主关系图 '{main_graph_name}' 失败: {e}", exc_info=True)
                raise
        else:
            logger.debug(f"主关系图 '{main_graph_name}' 已存在。")
            self.main_graph = self.db.graph(main_graph_name)

        # ---- 创建思想图 (consciousness_graph) ----
        thought_graph_name = CoreDBCollections.THOUGHT_GRAPH_NAME
        if not await self.db.has_graph(thought_graph_name):
            logger.info(f"思想图 '{thought_graph_name}' 不存在，正在创建...")
            thought_edge_definitions = [
                {
                    "collection": CoreDBCollections.PRECEDES_THOUGHT,
                    "from": [CoreDBCollections.THOUGHT_CHAIN],
                    "to": [CoreDBCollections.THOUGHT_CHAIN],
                },
                {
                    "collection": CoreDBCollections.LEADS_TO_ACTION,
                    "from": [CoreDBCollections.THOUGHT_CHAIN],
                    "to": [CoreDBCollections.ACTION_LOGS],
                },
            ]
            try:
                self.thought_graph = await self.db.create_graph(
                    thought_graph_name, edge_definitions=thought_edge_definitions
                )
            except GraphCreateError as e:
                logger.error(f"创建思想图 '{thought_graph_name}' 失败: {e}", exc_info=True)
                raise
        else:
            logger.debug(f"思想图 '{thought_graph_name}' 已存在。")
            self.thought_graph = self.db.graph(thought_graph_name)

        logger.info("核心数据库基础设施已确认就绪。")

    async def ensure_collection_with_indexes(
        self,
        collection_name: str,
        index_definitions: list[tuple[list[str], bool, bool]] | None = None,
        is_edge: bool = False,
    ) -> StandardCollection:
        """确保集合存在并应用必要的索引.

        Args:
            collection_name (str): 要确保的集合名称.
            index_definitions (list[tuple[list[str], bool, bool]] | None): 索引定义列表，
                每个定义是一个元组，包含字段列表、唯一性和稀疏性.
            is_edge (bool): 如果为 True，则表示这是一个边集合，否则为文档集合.

        Returns:
            StandardCollection: 确保存在的集合实例.
        """
        if not await self.db.has_collection(collection_name):
            logger.debug(
                f"集合 '{collection_name}' 不存在，正在创建 "
                f"(类型: {'edge' if is_edge else 'document'})..."
            )
            try:
                await self.db.create_collection(collection_name, col_type=3 if is_edge else 2)
                logger.debug(f"集合 '{collection_name}' 创建成功。")
            except CollectionCreateError as e:
                logger.warning(
                    f"创建集合 '{collection_name}' 时发生冲突或错误: {e}。将继续尝试获取该集合。"
                )

        try:
            collection = self.db.collection(collection_name)
            if collection and index_definitions:
                await self._apply_indexes_to_collection(collection, index_definitions)
            return collection
        except Exception as e:
            logger.error(f"最终获取集合 '{collection_name}' 失败: {e}", exc_info=True)
            raise

    async def _apply_indexes_to_collection(
        self,
        collection: StandardCollection,
        indexes_to_create: list[tuple[list[str], bool, bool]],
    ) -> None:
        collection_name = collection.name
        try:
            current_indexes_info = await collection.indexes()
            existing_indexes_set = {
                (
                    "_".join(sorted(str(f) for f in idx.fields)),
                    idx.type,
                    idx.unique or False,
                    idx.sparse or False,
                )
                for idx in current_indexes_info
            }
            for fields, unique, sparse in indexes_to_create:
                index_type = "persistent"
                desired_index_signature = (
                    "_".join(sorted(str(f) for f in fields)),
                    index_type,
                    unique,
                    sparse,
                )
                if desired_index_signature in existing_indexes_set:
                    continue
                logger.debug(
                    f"正在为集合 '{collection_name}' 应用索引: 字段={fields}, 类型={index_type}, "
                    f"唯一={unique}, 稀疏={sparse}"
                )
                await collection.add_index(
                    type=index_type,
                    fields=fields,
                    options={"unique": unique, "sparse": sparse, "inBackground": True},
                )
        except ArangoServerError as e:
            logger.warning(f"为集合 '{collection_name}' 应用索引时发生数据库错误: {e}。")
        except Exception as e:
            logger.error(f"为集合 '{collection_name}' 应用索引时发生意外错误: {e}", exc_info=True)

    async def execute_query(
        self,
        query: str,
        bind_vars: Mapping[str, Any] | None = None,
        stream: bool = False,
        **kwargs: object,
    ) -> list[Any] | AsyncIterator[Any]:
        """执行 AQL 查询并返回结果.

        Args:
            query (str): 要执行的 AQL 查询语句。
            bind_vars (Mapping[str, Any] | None): 可选的绑定变量，用于参数化查询。
            stream (bool): 如果为 True，则返回一个异步迭代器，否则返回结果列表。
            **kwargs: 其他可选参数，传递给 arangoasync 的查询执行方法。
        Returns:
            list[Any] | AsyncIterator[Any]: 如果 stream 为 False，则返回查询结果列表；
                如果为 True，则返回异步迭代器。
        """
        if not self.db:

            async def empty_iterator() -> AsyncIterator[Any]:
                if False:
                    yield

            return empty_iterator() if stream else []
        try:
            cursor = await self.db.aql.execute(query, bind_vars=bind_vars, **kwargs)
            return cursor if stream else [doc async for doc in cursor]
        except AQLQueryExecuteError as e:
            logger.error(f"AQL查询执行失败: {e.error_message}", exc_info=True)
            logger.debug(f"失败的AQL查询: {query}")
            logger.debug(f"失败的绑定参数: {bind_vars}")

            async def empty_iterator() -> AsyncIterator[Any]:
                if False:
                    yield

            return empty_iterator() if stream else []
        except Exception as e:
            logger.error(f"AQL查询执行期间发生意外错误: {e}", exc_info=True)
            logger.debug(f"失败的AQL查询: {query}")
            logger.debug(f"失败的绑定参数: {bind_vars}")

            async def empty_iterator() -> AsyncIterator[Any]:
                if False:
                    yield

            return empty_iterator() if stream else []

    async def close_client(self) -> None:
        """关闭 ArangoDB 客户端连接."""
        if self.client:
            await self.client.close()
            logger.info("arangoasync 客户端连接已关闭。")
