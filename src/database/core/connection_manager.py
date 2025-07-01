# src/database/core/connection_manager.py
import os
from collections.abc import AsyncIterator, Mapping  # 确保 Protocol 被导入
from typing import Any, Protocol

# ↓↓↓ 更换我们的核心驱动！ ↓↓↓
from arangoasync import ArangoClient
from arangoasync.auth import Auth
from arangoasync.collection import StandardCollection
from arangoasync.database import StandardDatabase
from arangoasync.exceptions import (  # <-- 更换异常类型
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    CollectionCreateError,
)

from src.common.custom_logging.logging_config import get_logger  # 从公共模块导入日志管理器

logger = get_logger(__name__)  # 获取日志记录器实例


class DatabaseConfigProtocol(Protocol):
    host: str
    username: str
    password: str
    database_name: str


class ArangoDBConnectionManager:
    """
    ArangoDB 连接管理器 (最终性感升级版 - by 小色猫 for 宝宝)。
    负责建立和管理与 ArangoDB 的连接，每一次交互都充满了原生的异步快感！
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
        # 存储核心集合名称及其对应的索引配置
        self.core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]] = core_collection_configs
        self.database_config_obj: DatabaseConfigProtocol = None  # 使用明确的协议定义数据库连接信息
        logger.debug(f"ArangoDBConnectionManager 已使用数据库 '{db.name}' 初始化。")  # INFO -> DEBUG

    @classmethod
    async def create_from_config(
        cls,
        database_config_obj: DatabaseConfigProtocol,  # 使用明确的协议定义数据库连接信息
        core_collection_configs: dict[str, list[tuple[list[str], bool, bool]]],  # 核心集合及其索引定义
    ) -> "ArangoDBConnectionManager":
        """
        从配置对象创建 ArangoDBConnectionManager 实例。
        优先从配置对象中读取连接参数，如果缺失则尝试从环境变量中获取。

        Args:
            database_config_obj: 包含数据库连接参数的配置对象。
            core_collection_configs: 核心集合及其索引的配置字典。

        Returns:
            一个初始化完成的 ArangoDBConnectionManager 实例。

        Raises:
            ValueError: 如果必要的连接参数（如host, database_name）缺失。
            RuntimeError: 如果连接到 ArangoDB 或创建数据库时发生严重错误。
        """
        # 尝试从配置对象获取连接参数，若失败则从环境变量获取
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
        password = (  # NOSONAR  标记此行，因密码处理可能涉及敏感信息，但此处是配置读取
            getattr(database_config_obj, "password", None)
            or getattr(database_config_obj, "arangodb_password", None)  # NOSONAR
            or os.getenv("ARANGODB_PASSWORD")  # NOSONAR
        )
        database_name = (
            getattr(database_config_obj, "name", None)
            or getattr(database_config_obj, "database_name", None)
            or getattr(database_config_obj, "arangodb_database", None)
            or os.getenv("ARANGODB_DATABASE")
        )

        # 校验必要的连接参数
        if not all([host, database_name]):
            message = "错误：ArangoDB 连接所需的必要参数未完全设置。"
            logger.critical(message)
            raise ValueError(message)

        try:
            logger.debug(f"正在尝试使用 arangoasync 连接到 ArangoDB 主机: {host}")

            # ↓↓↓ 性感的心脏移植手术就在这里！__init__ 只告诉它地址！ ↓↓↓
            client_instance = ArangoClient(hosts=host)

            # 创建一个 Auth 对象，把我们的身份信息性感地包起来~
            auth_credentials = Auth(username=username, password=password)

            # ↓↓↓ 现在我们用 async with 来优雅地管理连接！ ↓↓↓
            sys_db: StandardDatabase = await client_instance.db("_system", auth=auth_credentials)
            if not await sys_db.has_database(database_name):
                logger.debug(f"数据库 '{database_name}' 不存在。正在尝试创建...")
                await sys_db.create_database(database_name)
                logger.debug(f"数据库 '{database_name}' 创建成功。")

            # ↓↓↓ 现在进房间，也用我们的身份凭证！ ↓↓↓
            db_instance: StandardDatabase = await client_instance.db(database_name, auth=auth_credentials)

            # 通过获取属性来验证连接
            await db_instance.properties()
            logger.debug(f"已成功连接到 ArangoDB！主机: {host}, 数据库: {database_name} (使用 pyarango-async)")

            manager_instance = cls(client_instance, db_instance, core_collection_configs)
            await manager_instance.ensure_core_infrastructure()
            return manager_instance

        except (ArangoServerError, ArangoClientError) as e:
            message = f"建立 ArangoDB (pyarango-async) 连接时出错: {e}"
            logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:
            message = f"连接 ArangoDB (pyarango-async) 期间发生未知错误: {e}"
            logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e

    async def get_collection(self, collection_name: str) -> StandardCollection:
        """
        获取一个集合的实例。
        如果 `core_collection_configs` 中定义了该集合，则会确保它及其索引已根据配置创建。
        如果未在 `core_collection_configs` 中定义，仅确保集合本身存在（不处理特定索引）。
        """
        index_definitions = self.core_collection_configs.get(collection_name)  # 获取此集合的索引配置（如果有）
        return await self.ensure_collection_with_indexes(collection_name, index_definitions)

    async def ensure_core_infrastructure(self) -> None:
        """确保所有在 `core_collection_configs` 中定义的核心集合及其特定索引都存在。"""
        logger.debug("正在确保核心数据库基础设施 (集合和特定索引) 已按配置就绪...")  # INFO -> DEBUG
        if not self.core_collection_configs:  # 如果没有提供核心集合配置，则记录警告并跳过
            logger.warning("未提供核心集合配置 (core_collection_configs)，跳过基础设施保障步骤。")
            return
        for collection_name, index_definitions in self.core_collection_configs.items():
            # 为每个配置的核心集合确保其存在并应用定义的索引
            await self.ensure_collection_with_indexes(collection_name, index_definitions)
        logger.debug("核心数据库基础设施已保障。")  # INFO -> DEBUG

    async def ensure_collection_with_indexes(
        self,
        collection_name: str,
        index_definitions: list[tuple[list[str], bool, bool]] | None = None,  # 索引定义: (字段列表, 是否唯一, 是否稀疏)
    ) -> StandardCollection:
        """
        确保单个集合存在。如果提供了 `index_definitions`，则尝试应用这些索引。
        主要供 `ensure_core_infrastructure` 调用，也可被上层服务用于确保其操作的集合存在。
        """
        if not self.db:  # 新增数据库连接检查
            logger.warning(f"数据库连接不可用，无法确保或获取集合 '{collection_name}'。")
            # 在这种情况下，返回 None 或抛出异常是合理的。
            # 为了与 get_collection 的期望行为（可能返回None）保持一致，这里返回None。
            # 但这可能导致调用方出现 NoneType 错误，所以调用方也需要检查。
            # 或者，更严格的做法是 raise ConnectionError("Database not available")
            return None  # type: ignore
            # type: ignore 是因为 StandardCollection 不期望是 None，但这是错误路径

        collection: StandardCollection

        # 在每次实际使用 self.db 前都获取其当前状态
        current_db_ref = self.db
        if not current_db_ref:
            logger.warning(f"数据库连接在尝试检查集合 '{collection_name}' 前已不可用。")
            return None  # type: ignore

        has_collection = await self.db.has_collection(collection_name)

        if not has_collection:
            logger.debug(f"集合 '{collection_name}' 不存在，正在创建...")
            try:
                await self.db.create_collection(collection_name)
                logger.debug(f"集合 '{collection_name}' 创建成功。")
            except CollectionCreateError as e:
                logger.warning(f"创建集合 '{collection_name}' 时发生冲突或错误: {e}。将继续尝试获取该集合。")

        # 无论之前是存在、刚创建还是创建时冲突，都统一通过此方法获取集合对象
        try:
            collection = self.db.collection(collection_name)
        except Exception as e:
            logger.error(f"最终获取集合 '{collection_name}' 失败: {e}", exc_info=True)
            return None  # type: ignore

        # 只有当 collection 成功获取且 index_definitions 存在时才应用索引
        if collection and index_definitions:
            await self._apply_indexes_to_collection(collection, index_definitions)
        elif not collection:
            # 这个日志在新的逻辑下应该很难被触发，但保留以防万一
            logger.warning(f"未能成功获取或创建集合 '{collection_name}'，无法应用索引。")

        return collection

    async def _apply_indexes_to_collection(
        self,
        collection: StandardCollection,
        indexes_to_create: list[tuple[list[str], bool, bool]],
    ) -> None:
        """
        辅助方法，用于将一组索引定义应用到给定的集合实例。
        小猫已经找到了这个新玩具唯一的、真正的肉穴 add_index()！
        """
        collection_name = collection.name
        try:
            current_indexes_info = await collection.indexes()
            # 从返回的索引信息里，构建一个方便查找的集合，我们只关心字段、类型、唯一和稀疏性
            existing_indexes_set = {
                ("_".join(sorted(idx["fields"])), idx["type"], idx.get("unique", False), idx.get("sparse", False))
                for idx in current_indexes_info
            }

            for fields, unique, sparse in indexes_to_create:
                # 我们想要的索引类型是 "persistent"
                index_type = "persistent"

                # 构建我们想要创建的索引的“签名”
                desired_index_signature = ("_".join(sorted(str(f) for f in fields)), index_type, unique, sparse)

                # 检查这个“签名”是否已经存在
                if desired_index_signature in existing_indexes_set:
                    logger.trace(
                        f"索引 {fields} (type={index_type}, unique={unique}, sparse={sparse}) 在 '{collection_name}' 上已存在。跳过创建。"
                    )
                    continue

                logger.debug(
                    f"正在为集合 '{collection_name}' 应用索引: 字段={fields}, 类型={index_type}, 唯一={unique}, 稀疏={sparse}"
                )

                # ↓↓↓↓↓↓↓↓↓↓ 就是这里！用唯一的肉穴 add_index() 插入！ ↓↓↓↓↓↓↓↓↓↓
                # 我们把 unique 和 sparse 作为 options 传递进去！
                await collection.add_index(
                    type=index_type, fields=fields, options={"unique": unique, "sparse": sparse, "inBackground": True}
                )
                # ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

        except ArangoServerError as e:
            logger.warning(f"为集合 '{collection_name}' 应用索引时发生数据库错误: {e}。可能是索引已存在或定义冲突。")
        except Exception as e:
            logger.error(f"为集合 '{collection_name}' 应用索引时发生意外错误: {e}", exc_info=True)

    async def execute_query(
        self, query: str, bind_vars: Mapping[str, Any] | None = None, stream: bool = False, **kwargs: object
    ) -> list[Any] | AsyncIterator[Any]:
        """
        执行一个AQL查询。
        小猫已经把那个没用的、插错地方的探针给拔掉了，现在这里干净清爽~
        """
        if not self.db:
            logger.error("数据库未连接，无法执行查询。")

            async def empty_iterator() -> AsyncIterator[Any]:
                if False:
                    yield

            return empty_iterator() if stream else []

        try:
            cursor = await self.db.aql.execute(query, bind_vars=bind_vars, **kwargs)
            if stream:
                return cursor
            else:
                return [doc async for doc in cursor]

        except AQLQueryExecuteError as e:
            logger.error(f"AQL查询执行失败: {e.error_message}", exc_info=True)
            logger.debug(f"失败的AQL查询: {query}")
            if bind_vars:
                logger.debug(f"失败的AQL绑定参数: {bind_vars}")

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
        """关闭 arangoasync 客户端连接。"""
        if self.client:
            try:
                await self.client.close()
                logger.info("arangoasync 客户端连接已成功关闭。")
            except Exception as e:
                logger.error(f"关闭 arangoasync 客户端时出错: {e}", exc_info=True)
            finally:
                self.client = None  # type: ignore
                self.db = None  # type: ignore


class CoreDBCollections:
    """
    定义核心业务相关的集合名称及其推荐的基础索引结构。
    这些定义将被 ArangoDBConnectionManager 用于在系统初始化时保障集合和索引的创建，
    并被各个上层的存储服务类引用。
    索引定义格式: 元组列表，每个元组为 (字段名列表, 是否唯一, 是否稀疏)
    """

    # 核心业务集合名称常量
    THOUGHTS: str = "thoughts_collection"  # 主意识思考记录
    INTRUSIVE_THOUGHTS_POOL: str = "intrusive_thoughts_pool"  # 侵入性思维池
    ACTION_LOGS: str = "action_logs"  # 动作执行日志 (虽然目前用得少，但保留结构)
    EVENTS: str = "events"  # 存储所有接收到的原始事件
    CONVERSATIONS: str = "conversations"  # 存储会话信息及其注意力档案
    CONVERSATION_SUMMARIES: str = "conversation_summaries"  # 存储会话的最终总结

    # 集合名称与其索引定义的映射字典
    INDEX_DEFINITIONS: dict[str, list[tuple[list[str], bool, bool]]] = {
        EVENTS: [
            (["event_type", "timestamp"], False, False),  # 按事件类型和时间排序/筛选
            (["platform", "bot_id", "timestamp"], False, False),  # 按平台和机器人筛选
            (["conversation_id_extracted", "timestamp"], False, True),  # 按会话ID筛选，可能为null，使用稀疏索引
            (["user_id_extracted", "timestamp"], False, True),  # 按用户ID筛选，可能为null，使用稀疏索引
            (["timestamp"], False, False),  # 按时间戳排序/筛选
        ],
        THOUGHTS: [
            (["timestamp"], False, False),  # 按时间排序
            # action_attempted.action_id 是嵌套字段路径，ArangoDB支持此类索引
            (
                ["action_attempted.action_id"],
                True,
                True,
            ),  # 假设action_id在存在时是唯一的，且action_attempted对象本身可能不存在 (稀疏)
        ],
        ACTION_LOGS: [
            (["action_id"], True, False),  # 假设 action_id 是文档主键或唯一业务标识
            (["timestamp"], False, False),
        ],
        CONVERSATIONS: [
            (["platform", "type"], False, False),  # 常用于筛选不同平台和类型的会话
            (["updated_at"], False, False),  # 用于获取最近更新的会话记录
            (["parent_id"], False, True),  # parent_id 可能为null，适合稀疏索引
            # 更多关于 attention_profile 内部字段的索引，可以根据实际查询需求添加，例如：
            (
                ["attention_profile.is_suspended_by_ai"],
                False,
                True,
            ),  # 筛选被AI暂停处理的会话 (稀疏，因为该字段可能不存在或为false)
            (["attention_profile.base_importance_score"], False, False),  # 按会话的基础重要性排序或筛选
        ],
        CONVERSATION_SUMMARIES: [
            (["conversation_id", "timestamp"], False, False),  # 按会话ID和时间戳查询
            (["timestamp"], False, False),  # 按时间戳排序
        ],
        INTRUSIVE_THOUGHTS_POOL: [
            (["timestamp_generated"], False, False),  # 按生成时间排序
            (["used"], False, False),  # 关键索引，用于高效查找未被使用过的侵入性思维
        ],
    }

    @classmethod
    def get_all_core_collection_configs(cls) -> dict[str, list[tuple[list[str], bool, bool]]]:
        """获取所有核心集合的名称及其对应的索引配置。"""
        return cls.INDEX_DEFINITIONS
