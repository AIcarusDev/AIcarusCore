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
    GraphCreateError,  # 把这个也请进来，免得它哭
)
from arangoasync.graph import Graph  # 这可是主角！

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


class DatabaseConfigProtocol(Protocol):
    host: str
    username: str
    password: str
    database_name: str


class CoreDBCollections:
    """一个中央管家，负责记下所有核心集合的名字和它们的类型。"""

    # 点集合 (Vertex Collections)
    PERSONS = "persons"
    ACCOUNTS = "accounts"
    CONVERSATIONS = "conversations"  # 这个也是点

    # 边集合 (Edge Collections)
    HAS_ACCOUNT = "has_account"
    PARTICIPATES_IN = "participates_in"

    # 其他集合...
    EVENTS = "events"
    THOUGHTS = "thoughts_collection"
    INTRUSIVE_THOUGHTS_POOL = "intrusive_thoughts_pool"
    ACTION_LOGS = "action_logs"
    CONVERSATION_SUMMARIES = "conversation_summaries"

    # 图的名字，就叫这个吧，懒得想了
    MAIN_GRAPH_NAME = "person_relation_graph"

    # 所有集合的索引定义，放这里统一管理
    # 【注意】没有索引定义的集合（比如我们的边集合）也需要被创建！
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
            (["person_id"], True, False),  # person_id 应该是唯一的
        ],
        ACCOUNTS: [
            (["account_uid"], True, False),  # account_uid 也必须唯一
            (["platform", "platform_id"], True, False),
        ],
        # 我们的边集合在这里没有定义索引，所以之前的逻辑会跳过它们！
        # PARTICIPATES_IN: [
        #     (["permission_level"], False, True)
        # ],
        THOUGHTS: [
            (["timestamp"], False, False),
            (["action.action_id"], True, True),
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
            (["timestamp"], False, False),
            (["used"], False, False),
        ],
    }

    @classmethod
    def get_all_collection_names(cls) -> set[str]:
        """
        【小懒猫的新技能】返回所有需要确保存在的集合名称，一个都不能少！
        """
        return {
            # 点集合
            cls.PERSONS,
            cls.ACCOUNTS,
            cls.CONVERSATIONS,
            # 边集合
            cls.HAS_ACCOUNT,
            cls.PARTICIPATES_IN,
            # 其他文档集合
            cls.EVENTS,
            cls.THOUGHTS,
            cls.INTRUSIVE_THOUGHTS_POOL,
            cls.ACTION_LOGS,
            cls.CONVERSATION_SUMMARIES,
        }

    @classmethod
    def get_all_core_collection_configs(cls) -> dict[str, list[tuple[list[str], bool, bool]]]:
        """获取所有集合的索引配置。"""
        return cls.INDEX_DEFINITIONS

    @classmethod
    def get_edge_collection_names(cls) -> set[str]:
        """返回所有边集合的名字。"""
        return {cls.HAS_ACCOUNT, cls.PARTICIPATES_IN}

    @classmethod
    def get_vertex_collection_names(cls) -> set[str]:
        """返回所有点集合的名字。"""
        return {cls.PERSONS, cls.ACCOUNTS, cls.CONVERSATIONS}


class ArangoDBConnectionManager:
    """
    ArangoDB 连接管理器 (小懒猫尊严修正版)。
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
                raise RuntimeError("主图未初始化，无法获取边集合！这不科学！")
            return self.main_graph.edge_collection(name)

        index_definitions = self.core_collection_configs.get(name)
        return await self.ensure_collection_with_indexes(name, index_definitions, is_edge=False)

    async def ensure_core_infrastructure(self) -> None:
        """
        确保核心的数据库基础设施（集合和图）都准备好了。
        这是小懒猫的终极忏悔修正版，这次我再也不相信任何人了！
        """
        logger.debug("正在检查数据库基础设施 (集合与图)...")
        if not self.core_collection_configs:
            logger.warning("未提供核心集合配置，跳过基础设施检查。")
            return

        # 1. 【关键修复】获取所有需要创建的集合名称，一个都不能漏！
        all_collections_to_ensure = CoreDBCollections.get_all_collection_names()
        graph_edge_names = CoreDBCollections.get_edge_collection_names()

        # 2. 像个老妈子一样，一个一个地检查，不存在就给它创建好！
        #    这次我明确告诉它哪个是边集合，免得又搞错。
        for collection_name in all_collections_to_ensure:
            is_edge = collection_name in graph_edge_names
            index_definitions = self.core_collection_configs.get(collection_name)
            # ensure_collection_with_indexes 这个方法现在要能正确处理 is_edge 参数了
            await self.ensure_collection_with_indexes(collection_name, index_definitions, is_edge=is_edge)

        # 3. 现在，所有的“砖块”（集合）都准备好了，可以开始盖“房子”（图）了
        graph_name = CoreDBCollections.MAIN_GRAPH_NAME
        graph_vertex_names = CoreDBCollections.get_vertex_collection_names()

        if not await self.db.has_graph(graph_name):
            logger.info(f"主关系图 '{graph_name}' 不存在，正在创建...")

            edge_definitions = [
                {
                    "collection": edge_name,
                    "from": list(graph_vertex_names),
                    "to": list(graph_vertex_names),
                }
                for edge_name in graph_edge_names
            ]

            try:
                self.main_graph = await self.db.create_graph(graph_name, edge_definitions=edge_definitions)
            except GraphCreateError as e:
                logger.error(f"创建主关系图 '{graph_name}' 失败: {e}", exc_info=True)
                raise
        else:
            logger.debug(f"主关系图 '{graph_name}' 已存在。")
            self.main_graph = self.db.graph(graph_name)

        # 4. 检查图定义是否完整
        for edge_name in graph_edge_names:
            if not await self.main_graph.has_edge_definition(edge_name):
                logger.warning(f"图 '{graph_name}' 中缺少边定义 '{edge_name}'，正在尝试补上...")
                try:
                    await self.main_graph.create_edge_definition(
                        edge_collection=edge_name,
                        from_vertex_collections=list(graph_vertex_names),
                        to_vertex_collections=list(graph_vertex_names),
                    )
                except Exception as e:
                    logger.error(f"为图 '{graph_name}' 补全边定义 '{edge_name}' 失败: {e}", exc_info=True)

        logger.info("核心数据库基础设施已确认就绪。")

    async def ensure_collection_with_indexes(
        self,
        collection_name: str,
        index_definitions: list[tuple[list[str], bool, bool]] | None = None,
        is_edge: bool = False,
    ) -> StandardCollection:
        if not await self.db.has_collection(collection_name):
            logger.debug(f"集合 '{collection_name}' 不存在，正在创建 (类型: {'edge' if is_edge else 'document'})...")
            try:
                await self.db.create_collection(collection_name, col_type=3 if is_edge else 2)
                logger.debug(f"集合 '{collection_name}' 创建成功。")
            except CollectionCreateError as e:
                logger.warning(f"创建集合 '{collection_name}' 时发生冲突或错误: {e}。将继续尝试获取该集合。")

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
                ("_".join(sorted(idx.fields)), idx.type, idx.unique or False, idx.sparse or False)
                for idx in current_indexes_info
            }
            for fields, unique, sparse in indexes_to_create:
                index_type = "persistent"
                desired_index_signature = ("_".join(sorted(str(f) for f in fields)), index_type, unique, sparse)
                if desired_index_signature in existing_indexes_set:
                    continue
                logger.debug(
                    f"正在为集合 '{collection_name}' 应用索引: 字段={fields}, 类型={index_type}, 唯一={unique}, 稀疏={sparse}"
                )
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
                if False:
                    yield

            return empty_iterator() if stream else []
        try:
            cursor = await self.db.aql.execute(query, bind_vars=bind_vars, **kwargs)
            return cursor if stream else [doc async for doc in cursor]
        except AQLQueryExecuteError as e:
            logger.error(f"AQL查询执行失败: {e.error_message}", exc_info=True)

            async def empty_iterator() -> AsyncIterator[Any]:
                if False:
                    yield

            return empty_iterator() if stream else []
        except Exception as e:
            logger.error(f"AQL查询执行期间发生意外错误: {e}", exc_info=True)

            async def empty_iterator() -> AsyncIterator[Any]:
                if False:
                    yield

            return empty_iterator() if stream else []

    async def close_client(self) -> None:
        if self.client:
            await self.client.close()
            logger.info("arangoasync 客户端连接已关闭。")
