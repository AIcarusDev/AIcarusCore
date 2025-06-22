# src/database/core/connection_manager.py
import asyncio
import os
from typing import Any, Protocol  # 确保 Protocol 被导入

from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import (
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    CollectionCreateError,
    IndexCreateError,
)

from src.common.custom_logging.logger_manager import get_logger  # 从公共模块导入日志管理器

logger = get_logger("AIcarusCore.DB.ConnectionManager")  # 获取日志记录器实例


class DatabaseConfigProtocol(Protocol):
    host: str
    username: str
    password: str
    database_name: str


class ArangoDBConnectionManager:
    """
    ArangoDB 连接管理器。
    负责建立和管理与 ArangoDB 数据库的连接，
    提供基础的集合和索引保障功能，以及通用的AQL查询执行方法。
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
        if not all([host, database_name]):  # host 和 database_name 是必需的
            missing_params = []
            if not host:
                missing_params.append("host (或 ARANGODB_HOST 环境变量)")
            if not database_name:
                missing_params.append("database_name (或 ARANGODB_DATABASE 环境变量)")
            message = f"错误：ArangoDB 连接所需的必要参数未完全设置。缺失: {', '.join(missing_params)}"
            logger.critical(message)
            raise ValueError(message)

        try:
            logger.debug(f"正在尝试连接到 ArangoDB 主机: {host}")  # INFO -> DEBUG
            # 在单独的线程中执行同步的 ArangoClient 初始化
            client_instance: ArangoClient = await asyncio.to_thread(ArangoClient, hosts=host)

            logger.debug(
                f"正在连接到 _system 数据库以管理目标数据库 '{database_name}' (用户: {username or '默认'})..."
            )  # INFO -> DEBUG
            # 连接到 _system 数据库以检查或创建目标数据库
            sys_db: StandardDatabase = await asyncio.to_thread(
                client_instance.db, "_system", username=username, password=password
            )
            # 如果目标数据库不存在，则创建它
            if not await asyncio.to_thread(sys_db.has_database, database_name):
                logger.debug(f"数据库 '{database_name}' 不存在。正在尝试创建...")  # INFO -> DEBUG
                await asyncio.to_thread(sys_db.create_database, database_name)
                logger.debug(f"数据库 '{database_name}' 创建成功。")  # INFO -> DEBUG (这条也改了)

            logger.debug(f"正在连接到目标数据库: '{database_name}' (用户: {username or '默认'})...")  # INFO -> DEBUG
            # 连接到目标数据库
            db_instance: StandardDatabase = await asyncio.to_thread(
                client_instance.db, database_name, username=username, password=password
            )
            await asyncio.to_thread(db_instance.properties)  # 通过获取数据库属性来验证连接是否成功
            logger.debug(f"已成功连接到 ArangoDB！主机: {host}, 数据库: {database_name}")  # INFO -> DEBUG

            # 创建 ConnectionManager 实例并初始化核心数据库结构（集合和索引）
            manager_instance = cls(client_instance, db_instance, core_collection_configs)
            await manager_instance.ensure_core_infrastructure()  # 确保所有核心集合和索引都已按配置创建
            return manager_instance

        except (ArangoServerError, ArangoClientError) as e:  # 特定于 ArangoDB 的客户端或服务器错误
            message = f"建立 ArangoDB 连接时出错 (主机: {host}, 数据库: {database_name}): {e}"
            logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:  # 捕获其他所有在连接过程中可能发生的意外错误
            message = f"连接 ArangoDB 期间发生未知错误或权限问题 (主机: {host}, 数据库: {database_name}, 用户: {username}): {e}"
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

        has_collection = await asyncio.to_thread(current_db_ref.has_collection, collection_name)

        if not has_collection:
            logger.debug(f"集合 '{collection_name}' 不存在，正在创建...")
            try:
                # 创建集合
                await asyncio.to_thread(current_db_ref.create_collection, collection_name)
                logger.debug(f"集合 '{collection_name}' 创建成功。")
            except CollectionCreateError as e:
                # 捕获并发创建错误，这种情况下集合可能已经被另一个进程创建
                logger.warning(f"创建集合 '{collection_name}' 时发生冲突或错误: {e}。将继续尝试获取该集合。")

        # 无论之前是存在、刚创建还是创建时冲突，都统一通过此方法获取集合对象
        try:
            collection = await asyncio.to_thread(current_db_ref.collection, collection_name)
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
        collection: StandardCollection,  # 要操作的集合对象
        indexes_to_create: list[tuple[list[str], bool, bool]],  # 索引定义列表: (字段列表, 是否唯一, 是否稀疏)
        recreate_if_different: bool = False,  # 高级选项：如果为True，则检查现有索引定义，若不同则先删除旧的再创建新的
    ) -> None:
        """
        辅助方法，用于将一组索引定义应用到给定的集合实例。
        此方法会检查索引是否已存在，以避免重复创建。
        """
        collection_name = collection.name
        try:
            # 获取当前集合已有的索引信息，用于比较
            current_indexes_info = await asyncio.to_thread(collection.indexes)
            # 将现有索引按其字段（排序后拼接成字符串）组织，方便快速查找是否已存在相同字段的索引
            existing_indexes_by_fields_str = {"_".join(sorted(idx["fields"])): idx for idx in current_indexes_info}

            for fields, unique, sparse in indexes_to_create:
                # 为当前要创建的索引的字段列表生成一个规范化的键字符串
                field_key_str = "_".join(sorted(str(f) for f in fields))  # 确保字段名是字符串

                if field_key_str in existing_indexes_by_fields_str:
                    existing_idx = existing_indexes_by_fields_str[field_key_str]
                    # 检查现有索引的定义（unique, sparse等属性）是否与期望的一致
                    if recreate_if_different and (
                        existing_idx.get("unique", False) != unique or existing_idx.get("sparse", False) != sparse
                    ):
                        # 如果启用了 recreate_if_different 且定义不一致，则尝试删除并重建
                        logger.info(f"索引 {fields} 在 '{collection_name}' 上已存在但定义不同。正在尝试重建。")
                        try:
                            await asyncio.to_thread(collection.delete_index, existing_idx["id"])
                            logger.info(f"已删除旧索引 {existing_idx['id']}，准备创建新索引。")
                        except Exception as e_del_idx:
                            logger.warning(
                                f"无法删除现有索引 {existing_idx['id']} 以进行重建: {e_del_idx}。将跳过此索引的重建。"
                            )
                            continue  # 如果删除失败，则跳过此索引的重建，继续处理下一个
                    else:
                        # 如果索引已存在且不需要重建（或定义一致），则跳过创建
                        logger.debug(f"索引 {fields} 在 '{collection_name}' 上已存在或定义匹配。跳过创建。")
                        continue

                # 如果索引不存在或已被删除以待重建，则创建新索引
                logger.debug(f"正在为集合 '{collection_name}' 应用索引: 字段={fields}, 唯一={unique}, 稀疏={sparse}")
                await asyncio.to_thread(
                    collection.add_persistent_index, fields=fields, unique=unique, sparse=sparse, in_background=True
                )
        except IndexCreateError as e:  # 捕获索引创建失败的特定异常
            # 这可能发生在索引名称冲突，或字段不支持某种类型的索引等情况
            logger.warning(
                f"无法为集合 '{collection_name}' 完全确保所有索引。错误: {e}。尝试的索引定义: {indexes_to_create}"
            )
        except Exception as e:  # 捕获其他在应用索引过程中发生的意外错误
            logger.error(f"为集合 '{collection_name}' 应用索引时发生意外错误: {e}", exc_info=True)

    async def execute_query(self, query: str, bind_vars: dict[str, Any] | None = None) -> list[dict[str, Any]] | None:
        """
        执行一个AQL（ArangoDB Query Language）查询。

        Args:
            query: 要执行的AQL查询语句。
            bind_vars: 查询中使用的绑定参数字典。

        Returns:
            包含查询结果文档（字典）的列表，如果发生错误则返回 None。
        """
        try:
            final_bind_vars = bind_vars or {}  # 确保 bind_vars 是一个字典
            # logger.debug(f"正在执行AQL (前100字符): {query[:100]}{'...' if len(query) > 100 else ''}") # 日志过于频繁时可注释掉
            # AQL的 execute 方法是同步的，因此用 to_thread 包装
            cursor = await asyncio.to_thread(
                self.db.aql.execute, query, bind_vars=final_bind_vars, count=False
            )  # count=False表示不获取总匹配数，提高性能
            results = await asyncio.to_thread(list, cursor)  # 将游标结果转换为列表
            return results
        except AQLQueryExecuteError as e:  # ArangoDB 特定的查询执行错误
            logger.error(f"AQL查询执行失败。错误详情: {e.errors()}")  # e.errors() 提供更详细的错误信息
            logger.error(f"失败的AQL查询: {query}")
            if bind_vars:
                logger.error(f"失败的AQL绑定参数: {bind_vars}")
            return None  # 查询失败时返回 None
        except Exception as e:  # 捕获其他所有可能的意外错误
            logger.error(f"AQL查询执行期间发生意外错误: {e}", exc_info=True)
            return None  # 查询失败时返回 None

    async def close_client(self) -> None:
        """关闭ArangoDB客户端连接。"""
        if self.client:  # 仅当客户端实例存在时才尝试关闭
            try:
                logger.info("正在关闭ArangoDB客户端连接...")
                await asyncio.to_thread(self.client.close)  # ArangoClient.close() 是同步方法
                logger.info("ArangoDB客户端连接已成功关闭。")
            except Exception as e:  # 捕获关闭连接时可能发生的任何错误
                logger.error(f"关闭ArangoDB客户端时出错: {e}", exc_info=True)
            finally:
                # 无论成功与否，都将客户端和数据库实例置为None，表示连接已关闭
                self.client = None  # type: ignore
                self.db = None  # type: ignore
        else:
            logger.info("ArangoDB客户端未初始化或已关闭，无需再次关闭。")


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
    EPISODIC_MEMORIES: str = "episodic_memories"  # 新增：体验类记忆主表
    MEMORY_METADATA: str = "memory_metadata"  # 新增：体验类记忆的元数据（标签）

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
        EPISODIC_MEMORIES: [
            (["memory_id"], True, False), # 记忆ID，唯一
            (["conversation_id", "created_at"], False, False), # 按会话和时间查询
            (["importance_score"], False, False), # 按重要性排序
            (["last_accessed_at"], False, False), # 按访问时间排序
        ],
        MEMORY_METADATA: [
            (["memory_id", "meta_key"], False, False), # 快速查找一个记忆的所有同类标签
            (["meta_key", "meta_value"], False, False), # 核心索引：通过标签的键值对反向查找记忆
        ],
    }

    @classmethod
    def get_all_core_collection_configs(cls) -> dict[str, list[tuple[list[str], bool, bool]]]:
        """获取所有核心集合的名称及其对应的索引配置。"""
        return cls.INDEX_DEFINITIONS
