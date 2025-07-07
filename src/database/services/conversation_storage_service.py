# src/database/services/conversation_storage_service.py
import time
from contextlib import suppress
from typing import Any

from arangoasync.exceptions import (
    DocumentInsertError,
    DocumentRevisionError,
    DocumentUpdateError,
)

from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
)

# from src.database import AttentionProfile # 将从 models 导入

logger = get_logger(__name__)


class ConversationStorageService:
    """
    该服务负责所有与会话（Conversations）及其关联的注意力档案（Attention Profiles）
    相关的存储操作。
    """

    COLLECTION_NAME = CoreDBCollections.CONVERSATIONS  # 使用 CoreDBCollections 定义的常量

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager

    async def initialize_infrastructure(self) -> None:
        """确保会话集合及其特定索引已创建。应在系统启动时调用。"""
        # 从 CoreDBCollections 获取索引定义
        index_definitions = CoreDBCollections.INDEX_DEFINITIONS.get(self.COLLECTION_NAME, [])
        await self.conn_manager.ensure_collection_with_indexes(self.COLLECTION_NAME, index_definitions)
        logger.info(f"'{self.COLLECTION_NAME}' 集合及其特定索引已初始化。")

    async def upsert_conversation_document(self, conversation_doc_data: dict[str, Any]) -> str | None:
        """
        插入或更新一个会话文档。
        期望 `conversation_doc_data` 中包含 'conversation_id'，它将被用作文档的 '_key'。
        此方法会自动管理 'created_at', 'updated_at' 时间戳，并在新创建文档时
        初始化 'attention_profile'（如果输入数据中未提供）。
        输入 `conversation_doc_data` 应该是一个已准备好用于数据库插入/更新的字典。
        """
        if not conversation_doc_data or not isinstance(conversation_doc_data, dict):
            logger.warning("无效的 'conversation_doc_data' (空或非字典类型)。无法执行 upsert 操作。")
            return None

        conversation_id = conversation_doc_data.get("conversation_id")
        if not conversation_id:
            logger.warning("'conversation_doc_data' 中缺少 'conversation_id'。无法执行 upsert 操作。")
            return None

        # 获取集合实例 (内部会确保集合存在，但索引应由 initialize_infrastructure 处理)
        collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
        doc_key = str(conversation_id)  # ArangoDB 的 _key 必须是字符串
        current_time_ms = int(time.time() * 1000)

        # 准备要写入数据库的文档数据
        doc_for_db = conversation_doc_data.copy()  # 复制以避免修改原始输入
        doc_for_db["_key"] = doc_key  # 确保 _key 被设置
        doc_for_db["updated_at"] = current_time_ms  # 总是更新 'updated_at'

        existing_doc: dict[str, Any] | None = None

        with suppress(Exception):  # 例如 DocumentNotFoundError
            existing_doc = await collection.get(doc_key)

        if existing_doc:  # 文档已存在，执行更新逻辑
            logger.debug(f"会话 '{doc_key}' 已存在。正在合并并更新其档案。")
            doc_for_db["created_at"] = existing_doc.get("created_at", current_time_ms)  # 保留原始的创建时间

            # 合并 attention_profile: 新数据优先，但如果新数据中没有，则保留旧的
            existing_profile = existing_doc.get("attention_profile", {})  # 如果旧文档没有profile，则为空字典
            new_profile_in_data = doc_for_db.get("attention_profile")
            if isinstance(new_profile_in_data, dict):
                # 使用新数据覆盖旧数据中的相应字段
                doc_for_db["attention_profile"] = {**existing_profile, **new_profile_in_data}
            elif isinstance(existing_profile, dict) and existing_profile:
                # 如果新数据中没有profile，但旧数据中有，则保留旧的
                doc_for_db["attention_profile"] = existing_profile
            else:  # 如果两边都没有，或者新的是无效类型
                # 确保它至少是一个空字典
                from src.database import AttentionProfile  # 延迟导入，避免循环依赖

                doc_for_db["attention_profile"] = AttentionProfile.get_default_profile().to_dict()

            # 类似地合并 'extra' 字段
            existing_extra = existing_doc.get("extra", {})
            new_extra_in_data = doc_for_db.get("extra")
            if isinstance(new_extra_in_data, dict):
                doc_for_db["extra"] = {**existing_extra, **new_extra_in_data}
            elif isinstance(existing_extra, dict) and existing_extra:
                doc_for_db["extra"] = existing_extra
            else:  # 如果两边都没有，或者新的是无效类型
                doc_for_db["extra"] = {}

            try:
                # collection.update 使用文档中的 _key 匹配并合并更新
                await collection.update(doc_for_db)
                logger.info(f"会话 '{doc_key}' 的档案已成功更新。")
                return {"_key": doc_key, "_id": f"{self.COLLECTION_NAME}/{doc_key}"}
            except DocumentUpdateError as e:
                logger.error(f"更新会话 '{doc_key}' 的档案失败: {e}", exc_info=True)
                return None
            except DocumentRevisionError as e_rev:  # 并发更新冲突
                logger.warning(f"更新会话 '{doc_key}' 档案时遇到版本冲突: {e_rev}。可能需要重试或乐观锁策略。")
                return None  # 或者根据策略决定是否重试
        else:  # 文档不存在，作为新文档插入
            logger.debug(f"会话 '{doc_key}' 是新的。正在创建其档案。")
            doc_for_db["created_at"] = current_time_ms  # 设置创建时间

            # 如果 attention_profile 未在输入数据中提供，则初始化为默认值
            if "attention_profile" not in doc_for_db or not isinstance(doc_for_db.get("attention_profile"), dict):
                from src.database import AttentionProfile  # 同上

                doc_for_db["attention_profile"] = AttentionProfile.get_default_profile().to_dict()

            # 确保 extra 字段存在，至少为空字典
            if "extra" not in doc_for_db or not isinstance(doc_for_db.get("extra"), dict):
                doc_for_db["extra"] = {}

            try:
                # insert 操作，如果 _key 已存在将会失败（除非 overwrite=True，但我们已经用 get 检查过了）
                result = await collection.insert(doc_for_db, overwrite=False)
                if result and result.get("_key"):
                    logger.info(f"新的会话档案 '{doc_key}' 已成功创建，ID: {result['_key']}")
                    return result["_key"]
                else:
                    # 这种情况理论上不应该发生，如果insert调用没有抛异常
                    logger.error(f"为新会话 '{doc_key}' 插入档案后未能获取 _key。返回结果: {result}")
                    return None
            except DocumentInsertError as e:
                # 如果由于并发原因，文档在此期间被创建了
                logger.error(f"尝试插入新会话 '{doc_key}' 失败（可能已由并发操作创建）: {e}", exc_info=True)
                # 可以考虑再次尝试 get 并 update，或者直接返回失败
                return None
        return None  # 确保所有路径都有返回值

    async def get_conversation_document_by_id(self, conversation_id: str) -> dict[str, Any] | None:
        """根据 conversation_id (即文档的 _key) 获取完整的会话文档。"""
        if not conversation_id:
            logger.warning("尝试获取会话文档但未提供 conversation_id。")
            return None
        try:
            collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
            doc = await collection.get(str(conversation_id))
            return doc  # collection.get 在找不到时返回 None
        except Exception as e:
            logger.error(f"获取会话文档失败，ID '{conversation_id}': {e}", exc_info=True)
            return None

    async def update_conversation_field(
        self, conversation_id: str, field_path_to_update: str, new_value: str | int | float | dict | list | bool | None
    ) -> bool:
        """
        更新会话文档中的特定字段或嵌套字段。
        例如: field_path_to_update = "attention_profile.base_importance_score"
        """
        if not conversation_id or not field_path_to_update:
            logger.warning("更新会话字段需要 conversation_id 和 field_path_to_update。")
            return False
        try:
            collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)

            # 构建用于部分更新的补丁文档
            # 例如，如果 field_path_to_update 是 "attention_profile.base_importance_score"
            # 我们需要构建 {"attention_profile": {"base_importance_score": new_value}}
            # 注意：ArangoDB 的 UPDATE ... WITH ... IN ... 语法支持直接更新嵌套路径，
            # 但 python-arango 的 collection.update() 方法通常期望你提供包含顶层键的文档。
            # 一个简单的方法是获取整个文档，修改，然后替换，但这有并发风险。
            # 或者，如果只更新顶层字段或简单嵌套，可以构造补丁。

            # 简化的补丁构造 (只支持一级嵌套的 attention_profile 内字段)
            patch_doc: dict[str, Any] = {}
            parts = field_path_to_update.split(".")
            if len(parts) == 1:
                patch_doc[parts[0]] = new_value
            elif len(parts) == 2 and parts[0] == "attention_profile":  # 特殊处理 attention_profile
                patch_doc["attention_profile"] = {parts[1]: new_value}
            # 更通用的嵌套更新可能需要更复杂的补丁构造或AQL UPDATE语句
            else:
                logger.error(
                    f"此方法目前仅支持更新顶层字段或 'attention_profile' 内的直接字段。路径: '{field_path_to_update}'"
                )
                return False

            doc_key = str(conversation_id)
            # 使用 update 方法，它会合并传入的 patch_doc
            await collection.update({"_key": doc_key, **patch_doc})
            logger.info(f"会话 '{conversation_id}' 中的字段 '{field_path_to_update}' 已更新为 '{new_value}'.")
            return True
        except Exception as e:
            logger.error(f"更新会话 '{conversation_id}' 的字段 '{field_path_to_update}' 失败: {e}", exc_info=True)
            return False

    async def get_all_active_conversations(self) -> list[dict[str, Any]]:
        """
        获取所有被认为是“活跃”的会话文档。
        目前的实现是获取所有会话，未来可以根据 attention_profile 过滤。
        """
        try:
            query = "FOR doc IN @@collection RETURN doc"
            bind_vars = {"@collection": self.COLLECTION_NAME}
            results = await self.conn_manager.execute_query(query, bind_vars)
            logger.info(f"成功获取到 {len(results) if results else 0} 个会话。")
            return results if results is not None else []
        except Exception as e:
            logger.error(f"获取所有活跃会话失败: {e}", exc_info=True)
            return []

    async def update_conversation_processed_timestamp(self, conversation_id: str, timestamp: int) -> bool:
        """
        更新指定会话的 last_processed_timestamp。
        """
        if not conversation_id:
            logger.warning("更新会话处理时间戳需要 conversation_id。")
            return False
        try:
            collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
            doc_key = str(conversation_id)
            patch = {"last_processed_timestamp": timestamp, "updated_at": int(time.time() * 1000)}
            await collection.update({"_key": doc_key, **patch})
            logger.debug(f"会话 '{conversation_id}' 的 last_processed_timestamp 已更新为 {timestamp}.")
            return True
        except Exception as e:
            logger.error(f"更新会话 '{conversation_id}' 的 last_processed_timestamp 失败: {e}", exc_info=True)
            return False
