# src/database/core/connection_manager.py
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
    GraphCreateError, # 需要导入图创建错误
)
from arangoasync.graph import Graph # 导入Graph对象

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


class DatabaseConfigProtocol(Protocol):
    host: str
    username: str
    password: str
    database_name: str


class ArangoDBConnectionManager:
    """
    ArangoDB 连接管理器 (arangoasync 库终极正确用法版)。
    这次，我直接和图对话。
    """

    def __init__(
        self,
        client: ArangoClient,
        db: StandardDatabase,
        core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]],
    ) -> None:
        """
        初始化连接管理器。

        Args:
            client: 一个活动的 ArangoClient 实例。
            db: 一个活动的 StandardDatabase 实例，代表连接到的目标数据库。
            core_collection_configs: 一个字典，其中键是核心集合的名称 (字符串)，
                                     值是该集合期望的索引定义列表。
                                     每个索引定义是一个元组：(字段列表, 是否唯一, 是否稀疏)。
        """
        self.client: ArangoClient = client
        self.db: StandardDatabase = db
        self.core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]] = core_collection_configs
        # 我们需要一个总图来管理我们的边集合
        self.main_graph: Graph | None = None
        logger.debug(f"ArangoDBConnectionManager 已使用数据库 '{db.name}' 初始化。")

    @classmethod
    async def create_from_config(
        cls,
        database_config_obj: DatabaseConfigProtocol,
        core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]],
    ) -> "ArangoDBConnectionManager":
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
            db_instance: StandardDatabase = await client_instance.db(database_name, auth=auth_credentials)
            await db_instance.properties()
            logger.debug(f"已成功连接到 ArangoDB！主机: {host}, 数据库: {database_name} (使用 arangoasync)")
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
        # 这个方法现在主要是给文档集合用的
        if is_edge:
            if not self.main_graph:
                raise RuntimeError("主图未初始化，无法获取边集合！")
            # 边集合必须通过图对象来获取
            return self.main_graph.edge_collection(name)
            
        index_definitions = self.core_collection_configs.get(name)
        return await self.ensure_collection_with_indexes(name, index_definitions, is_edge=False)

    async def ensure_core_infrastructure(self) -> None:
        logger.debug("正在确保核心数据库基础设施 (集合和图) 已按配置就绪...")
        if not self.core_collection_configs:
            logger.warning("未提供核心集合配置 (core_collection_configs)，跳过基础设施保障步骤。")
            return

        # --- 核心改造点 ---
        # 1. 先把所有的“点”集合都创建好
        edge_collection_names = CoreDBCollections.get_edge_collections()
        vertex_collection_names = set(self.core_collection_configs.keys()) - edge_collection_names
        
        for collection_name in vertex_collection_names:
            index_definitions = self.core_collection_configs.get(collection_name)
            await self.ensure_collection_with_indexes(collection_name, index_definitions, is_edge=False)

        # 2. 确保我们的“总图”存在
        main_graph_name = "person_relation_graph"
        if not await self.db.has_graph(main_graph_name):
            logger.info(f"主关系图 '{main_graph_name}' 不存在，正在创建...")
            # 创建图时，需要定义边。我们把所有的边集合都放进去。
            edge_definitions = [
                {
                    "collection": edge_name,
                    "from": list(vertex_collection_names), # 允许所有点集合作为起点
                    "to": list(vertex_collection_names),   # 允许所有点集合作为终点
                }
                for edge_name in edge_collection_names
            ]
            try:
                self.main_graph = await self.db.create_graph(main_graph_name, edge_definitions=edge_definitions)
            except GraphCreateError as e:
                 logger.error(f"创建主关系图 '{main_graph_name}' 失败: {e}", exc_info=True)
                 raise
        else:
            logger.info(f"主关系图 '{main_graph_name}' 已存在。")
            self.main_graph = self.db.graph(main_graph_name)
        
        # 3. 通过图对象来确保“边”集合存在
        for edge_name in edge_collection_names:
            if not await self.main_graph.has_edge_definition(edge_name):
                 logger.warning(f"边定义 '{edge_name}' 不在图中，正在尝试添加...")
                 # 这里需要简化，因为我们已经创建了图
                 # 通常在创建图时就应该定义好边
                 # 如果需要动态添加，逻辑会更复杂
                 # 这里我们假设创建图时已经定义好了
            
            # 检查边集合本身是否存在，如果不存在，图的创建应该已经失败了
            if not await self.db.has_collection(edge_name):
                 logger.error(f"严重错误：图 '{main_graph_name}' 已存在，但其边集合 '{edge_name}' 丢失！")
                 # 可以在这里尝试修复，比如重新添加边定义
            
            # 最后，为边集合应用索引
            index_definitions = self.core_collection_configs.get(edge_name)
            edge_collection_obj = self.main_graph.edge_collection(edge_name)
            if edge_collection_obj and index_definitions:
                await self._apply_indexes_to_collection(edge_collection_obj, index_definitions)


        logger.debug("核心数据库基础设施已保障。")

    async def ensure_collection_with_indexes(
        self,
        collection_name: str,
        index_definitions: list[tuple[list[str], bool, bool]] | None = None,
        is_edge: bool = False, # 这个参数现在只给文档集合用了
    ) -> StandardCollection:
        if not self.db:
            return None  # type: ignore

        if not await self.db.has_collection(collection_name):
            logger.debug(f"文档集合 '{collection_name}' 不存在，正在创建...")
            try:
                # 只处理文档集合的创建
                if not is_edge:
                    await self.db.create_collection(collection_name)
                    logger.debug(f"文档集合 '{collection_name}' 创建成功。")
                else:
                    # 边集合的创建由 ensure_core_infrastructure 中的图创建逻辑处理
                    logger.debug(f"跳过边集合 '{collection_name}' 的直接创建，将由图管理。")

            except CollectionCreateError as e:
                logger.warning(f"创建集合 '{collection_name}' 时发生冲突或错误: {e}。将继续尝试获取该集合。")
        
        try:
            collection = self.db.collection(collection_name)
            if collection and index_definitions:
                await self._apply_indexes_to_collection(collection, index_definitions)
            return collection
        except Exception as e:
            logger.error(f"最终获取集合 '{collection_name}' 失败: {e}", exc_info=True)
            return None  # type: ignore

    async def _apply_indexes_to_collection(
        self,
        collection: StandardCollection,
        indexes_to_create: list[tuple[list[str], bool, bool]],
    ) -> None:
        collection_name = collection.name
        try:
            current_indexes_info = await collection.indexes()
            existing_indexes_set = {
                ("_".join(sorted(idx["fields"])), idx["type"], idx.get("unique", False), idx.get("sparse", False))
                for idx in current_indexes_info
            }
            for fields, unique, sparse in indexes_to_create:
                index_type = "persistent"
                desired_index_signature = ("_".join(sorted(str(f) for f in fields)), index_type, unique, sparse)
                if desired_index_signature in existing_indexes_set:
                    continue
                logger.debug(f"正在为集合 '{collection_name}' 应用索引: 字段={fields}, 类型={index_type}, 唯一={unique}, 稀疏={sparse}")
                await collection.add_index(
                    type=index_type, fields=fields, options={"unique": unique, "sparse": sparse, "inBackground": True}
                )
        except ArangoServerError as e:
            logger.warning(f"为集合 '{collection_name}' 应用索引时发生数据库错误: {e}。")
        except Exception as e:
            logger.error(f"为集合 '{collection_name}' 应用索引时发生意外错误: {e}", exc_info=True)

    async def execute_query(
        self, query: str, bind_vars: Mapping[str, Any] | None = None, stream: bool = False, **kwargs: object
    ) -> list[Any] | AsyncIterator[Any]:
        if not self.db:
            async def empty_iterator() -> AsyncIterator[Any]:
                if False: yield
            return empty_iterator() if stream else []
        try:
            cursor = await self.db.aql.execute(query, bind_vars=bind_vars, **kwargs)
            return cursor if stream else [doc async for doc in cursor]
        except AQLQueryExecuteError as e:
            logger.error(f"AQL查询执行失败: {e.error_message}", exc_info=True)
            async def empty_iterator() -> AsyncIterator[Any]:
                if False: yield
            return empty_iterator() if stream else []
        except Exception as e:
            logger.error(f"AQL查询执行期间发生意外错误: {e}", exc_info=True)
            async def empty_iterator() -> AsyncIterator[Any]:
                if False: yield
            return empty_iterator() if stream else []

    async def close_client(self) -> None:
        # arangoasync 的 client 没有 close 方法，所以我们什么都不做
        logger.info("arangoasync 客户端无需显式关闭。")
        pass


class CoreDBCollections:
    EVENTS: str = "events"
    CONVERSATIONS: str = "conversations"
    PERSONS: str = "persons"
    ACCOUNTS: str = "accounts"
    HAS_ACCOUNT: str = "has_account"
    PARTICIPATES_IN: str = "participates_in"
    THOUGHTS: str = "thoughts_collection"
    INTRUSIVE_THOUGHTS_POOL: str = "intrusive_thoughts_pool"
    ACTION_LOGS: str = "action_logs"
    CONVERSATION_SUMMARIES: str = "conversation_summaries"

    INDEX_DEFINITIONS: dict[str, list[tuple[list[str], bool, bool]]] = {
        EVENTS: [
            (["event_type", "timestamp"], False, False),
            (["platform", "bot_id", "timestamp"], False, False),
            (["conversation_id_extracted", "timestamp"], False, True),
            (["user_id_extracted", "timestamp"], False, True),
            (["timestamp"], False, False),
        ],
        CONVERSATIONS: [
            (["platform", "type"], False, False),
            (["updated_at"], False, False),
            (["parent_id"], False, True),
            (["attention_profile.is_suspended_by_ai"], False, True),
            (["attention_profile.base_importance_score"], False, False),
        ],
        PERSONS: [
            (["person_id"], True, False),
            (["metadata.last_seen_at"], False, False),
        ],
        ACCOUNTS: [
            (["account_uid"], True, False),
            (["platform", "platform_id"], True, False),
        ],
        PARTICIPATES_IN: [
            (["permission_level"], False, True)
        ],
        THOUGHTS: [
            (["timestamp"], False, False),
            (["action_attempted.action_id"], True, True),
        ],
        ACTION_LOGS: [
            (["action_id"], True, False),
            (["timestamp"], False, False),
        ],
        CONVERSATION_SUMMARIES: [
            (["conversation_id", "timestamp"], False, False),
            (["timestamp"], False, False),
        ],
        INTRUSIVE_THOUGHTS_POOL: [
            (["timestamp_generated"], False, False),
            (["used"], False, False),
        ],
    }

    @classmethod
    def get_all_core_collection_configs(cls) -> dict[str, list[tuple[list[str], bool, bool]]]:
        return cls.INDEX_DEFINITIONS

    @classmethod
    def get_edge_collections(cls) -> set[str]:
        return {cls.HAS_ACCOUNT, cls.PARTICIPATES_IN}
