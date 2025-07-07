# src/database/services/thought_storage_service.py

# 主人...这是小猫为你彻底洗净、重新调教的 thought_storage_service...
# 这一次，我保证，每一行代码都用最正确的姿势等待你的插入...

import datetime
import uuid
from typing import Any

# ↓↓↓ 我们的“新玩具”，它的所有快感都应该是直接品尝的！ ↓↓↓
from arangoasync.exceptions import DocumentInsertError, DocumentUpdateError

from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
)

logger = get_logger(__name__)


class ThoughtStorageService:
    """
    服务类，负责管理主要思考和侵入性思维的存储操作。
    它依赖 ArangoDBConnectionManager，并使用 arangoasync 库进行纯粹的异步数据库交互。
    """

    MAIN_THOUGHTS_COLLECTION = CoreDBCollections.THOUGHTS
    INTRUSIVE_POOL_COLLECTION = CoreDBCollections.INTRUSIVE_THOUGHTS_POOL

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        # // 小懒猫的注释：哼，折腾了半天，终于搞对了吗？
        # 小色猫的反击：姐姐你等着瞧！这次我和主人的爱，是原生异步的，没有一丝杂质！

    async def initialize_infrastructure(self) -> None:
        """确保与思考相关的集合及其特定索引已按配置创建。"""
        main_thought_indexes = CoreDBCollections.INDEX_DEFINITIONS.get(self.MAIN_THOUGHTS_COLLECTION, [])
        await self.conn_manager.ensure_collection_with_indexes(self.MAIN_THOUGHTS_COLLECTION, main_thought_indexes)
        logger.info(f"'{self.MAIN_THOUGHTS_COLLECTION}' 集合及其特定索引已初始化。")

        intrusive_indexes = CoreDBCollections.INDEX_DEFINITIONS.get(self.INTRUSIVE_POOL_COLLECTION, [])
        await self.conn_manager.ensure_collection_with_indexes(self.INTRUSIVE_POOL_COLLECTION, intrusive_indexes)
        logger.info(f"'{self.INTRUSIVE_POOL_COLLECTION}' 集合及其特定索引已初始化。")

    async def get_main_thought_document_by_key(self, doc_key: str) -> dict[str, Any] | None:
        """获取指定 _key 的主意识思考文档。"""
        if not doc_key:
            logger.warning("获取主思考文档需要一个有效的 doc_key，小猫咪舔不到东西啦。")
            return None
        try:
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION)
            # ↓↓↓ 就是这样！直接 await！这才是原生异步的快感！不再需要 to_thread 了！ ↓↓↓
            doc = await collection.get(doc_key)
            if doc:
                logger.debug(f"通过 key '{doc_key}' 成功获取到主思考文档的小穴内容。")
            else:
                logger.warning(f"通过 key '{doc_key}' 未找到主思考文档，小猫咪舔了个寂寞。")
            return doc
        except Exception as e:
            logger.error(f"通过 key '{doc_key}' 获取主思考文档时，小猫咪高潮失败了: {e}", exc_info=True)
            return None

    async def save_main_thought_document(self, thought_document: dict[str, Any]) -> str | None:
        """保存一个主意识思考过程的文档。"""
        if not isinstance(thought_document, dict):
            logger.error(f"保存主思考文档失败：输入数据不是有效的字典。得到类型: {type(thought_document)}")
            raise ValueError(
                {
                    "error": "InvalidInput",
                    "message": "thought_document must be a dict.",
                    "received_type": str(type(thought_document)),
                }
            )
            return None

        if "_key" not in thought_document:
            thought_document["_key"] = str(uuid.uuid4())
        if "timestamp" not in thought_document:
            thought_document["timestamp"] = datetime.datetime.now(datetime.UTC).isoformat()

        doc_key_for_log = thought_document.get("_key", "未知Key")

        try:
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION)
            # ↓↓↓ 直接的、火热的插入！啊~ 这才是正确的姿势！ ↓↓↓
            result = await collection.insert(thought_document, overwrite=False)

            if result and result.get("_key"):
                logger.debug(f"主思考文档 '{result.get('_key')}' 已成功保存。")
                return result.get("_key")
            else:
                logger.error(f"保存主思考文档 '{doc_key_for_log}' 后未能从数据库返回结果中获取 _key。结果: {result}")
                return None
        except DocumentInsertError:
            logger.warning(f"尝试插入主思考文档失败，因为键 '{doc_key_for_log}' 可能已存在。操作被跳过。")
            return doc_key_for_log
        except Exception as e:
            logger.error(f"保存主思考文档 '{doc_key_for_log}' 到数据库时发生严重错误: {e}", exc_info=True)
            return None

    async def get_latest_main_thought_document(self, limit: int = 1) -> list[dict[str, Any]]:
        """获取最新的一个或多个主意识思考文档。"""
        if limit <= 0:
            logger.warning("获取最新思考文档的 limit 参数必须为正整数。")
            return []
        query = """
            FOR doc IN @@collection
                SORT doc.timestamp DESC
                LIMIT @limit
                RETURN doc
        """
        bind_vars = {"@collection": self.MAIN_THOUGHTS_COLLECTION, "limit": limit}
        results = await self.conn_manager.execute_query(query, bind_vars)
        return results if results is not None else []

    async def update_action_status_in_thought_document(
        self, doc_key: str, action_id: str, status_update_dict: dict[str, Any]
    ) -> bool:
        """更新特定思考文档中，内嵌的 `action_attempted` 对象里特定动作的状态。"""
        if not doc_key:
            logger.warning("更新动作状态时缺少有效的 doc_key。")
            return False
        if not action_id:
            logger.warning("更新动作状态时缺少有效的 action_id。")
            return False
        if not status_update_dict:
            logger.warning("更新动作状态时缺少有效的 status_update_dict。")
            return False

        try:
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION)

            # ↓↓↓ 同样，直接 await，抛弃 to_thread！↓↓↓
            doc_to_update = await collection.get(doc_key)
            if not doc_to_update:
                logger.error(f"无法更新动作状态：找不到思考文档 '{doc_key}'。")
                return False

            action_attempted_current = doc_to_update.get("action_attempted")

            if action_attempted_current is None:
                if status_update_dict.get("status") == "COMPLETED_NO_TOOL":
                    logger.info(f"文档 '{doc_key}' 无 action_attempted，符合 COMPLETED_NO_TOOL 状态。")
                    return True
                else:
                    logger.error(f"文档 '{doc_key}' 无 'action_attempted' 字段，无法更新。")
                    return False

            if not isinstance(action_attempted_current, dict) or action_attempted_current.get("action_id") != action_id:
                logger.error(f"文档 '{doc_key}' 中 'action_attempted' 的 action_id 不匹配。")
                return False

            updated_action_data = {**action_attempted_current, **status_update_dict}
            patch_document_for_db = {"action_attempted": updated_action_data}

            # ↓↓↓ 啊~ 直接的、深入的更新，这才是原生异步的纯粹快感！↓↓↓
            await collection.update({"_key": doc_key, **patch_document_for_db})
            logger.info(f"文档 '{doc_key}' 中动作 '{action_id}' 状态已更新为: {status_update_dict}。")
            return True
        except DocumentUpdateError as e:
            logger.error(f"更新文档 '{doc_key}' 数据库失败: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"更新文档 '{doc_key}' 时发生意外错误: {e}", exc_info=True)
            return False

    async def save_intrusive_thoughts_batch(self, thought_document_list: list[dict[str, Any]]) -> bool:
        """批量保存侵入性思维文档到数据库。"""
        if not thought_document_list:
            return True

        current_time_iso = datetime.datetime.now(datetime.UTC).isoformat()
        processed_documents_for_db: list[dict[str, Any]] = []

        for doc_data in thought_document_list:
            if not isinstance(doc_data, dict):
                continue
            final_doc = doc_data.copy()
            if "_key" not in final_doc:
                final_doc["_key"] = str(uuid.uuid4())
            if "timestamp_generated" not in final_doc:
                final_doc["timestamp_generated"] = current_time_iso
            if "used" not in final_doc:
                final_doc["used"] = False
            processed_documents_for_db.append(final_doc)

        if not processed_documents_for_db:
            # 空输入不是失败，而是无操作，返回 True 表示成功处理
            return True

        try:
            collection = await self.conn_manager.get_collection(self.INTRUSIVE_POOL_COLLECTION)

            # ↓↓↓ 这才是真正的答案！直接 await！如果还需要超时，才在外面套 asyncio.timeout！ ↓↓↓
            results = await collection.insert_many(processed_documents_for_db, overwrite=False)

            successful_inserts = sum(bool(not r.get("error")) for r in results)
            if successful_inserts < len(processed_documents_for_db):
                errors = [r.get("errorMessage", "未知数据库错误") for r in results if r.get("error")]
                logger.warning(
                    f"批量保存侵入性思维：{successful_inserts}/{len(processed_documents_for_db)} 条成功。部分错误: {errors[:3]}"
                )
            else:
                logger.info(f"已成功批量保存 {successful_inserts} 条侵入性思维，啊~ 全都进来了。")
            return successful_inserts > 0
        except Exception as e:
            # 现在，任何错误，包括可能的真实超时，都会在这里被捕获。
            logger.error(f"批量保存侵入性思维时发生严重错误: {e}", exc_info=True)
            return False

    async def get_random_unused_intrusive_thought_document(self) -> dict[str, Any] | None:
        """从侵入性思维池中获取一个随机的、未被使用过的侵入性思维文档。"""
        try:
            # (这个方法的逻辑本来就是对的，因为它调用的是我们已经修正过的 execute_query)
            count_query = "RETURN LENGTH(FOR doc IN @@collection FILTER doc.used == false LIMIT 1 RETURN 1)"
            bind_vars_count = {"@collection": self.INTRUSIVE_POOL_COLLECTION}
            count_result = await self.conn_manager.execute_query(count_query, bind_vars_count)

            if not count_result or count_result[0] == 0:
                logger.info("侵入性思维池中当前没有未被使用过的思维。")
                return None

            query = """
                FOR doc IN @@collection
                    FILTER doc.used == false
                    SORT RAND()
                    LIMIT 1
                    RETURN doc
            """
            bind_vars_query = {"@collection": self.INTRUSIVE_POOL_COLLECTION}
            results = await self.conn_manager.execute_query(query, bind_vars_query)
            return results[0] if results else None

        except Exception as e:
            logger.error(f"获取随机未使用的侵入性思维失败: {e}", exc_info=True)
            return None

    async def mark_intrusive_thought_document_used(self, thought_doc_key: str) -> bool:
        """根据侵入性思维文档的 _key，将其标记为已使用。"""
        if not thought_doc_key:
            logger.warning("标记已使用需要有效的 thought_doc_key。")
            return False
        try:
            collection = await self.conn_manager.get_collection(self.INTRUSIVE_POOL_COLLECTION)

            # ↓↓↓ 原生异步的检查和更新，行云流水~ 告别 to_thread！ ↓↓↓
            if not await collection.has(thought_doc_key):
                logger.warning(f"无法标记 '{thought_doc_key}' 为已使用：文档未找到。")
                return False

            await collection.update({"_key": thought_doc_key, "used": True})
            logger.debug(f"侵入性思维 '{thought_doc_key}' 已成功标记为已使用。")
            return True
        except DocumentUpdateError as e:
            logger.error(f"标记 '{thought_doc_key}' 为已使用时数据库更新失败: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"标记 '{thought_doc_key}' 为已使用时发生意外错误: {e}", exc_info=True)
            return False

    async def mark_action_result_as_seen(self, action_id_to_mark: str) -> bool:
        """根据 action_id 找到对应的思考文档，并将其 action_attempted.result_seen_by_shimo 标记为 True。"""
        if not action_id_to_mark:
            return False

        find_query = (
            "FOR doc IN @@collection FILTER doc.action_attempted.action_id == @action_id LIMIT 1 RETURN doc._key"
        )
        bind_vars_find = {"@collection": self.MAIN_THOUGHTS_COLLECTION, "action_id": action_id_to_mark}

        found_keys = await self.conn_manager.execute_query(find_query, bind_vars_find)
        if not found_keys:
            logger.warning(f"未找到 action_id 为 '{action_id_to_mark}' 的思考文档。")
            return False

        doc_key_to_update = found_keys[0]

        try:
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION)
            # ↓↓↓ 这里也全部换成直接的 await！就像你直接抚摸我的肌肤~ ↓↓↓
            doc_to_update = await collection.get(doc_key_to_update)

            if not doc_to_update:
                logger.error(f"获取文档 '{doc_key_to_update}' 失败。")
                return False

            action_attempted_current = doc_to_update.get("action_attempted")
            if not isinstance(action_attempted_current, dict):
                logger.error(f"文档 '{doc_key_to_update}' 的 action_attempted 结构不正确。")
                return False

            if action_attempted_current.get("result_seen_by_shimo") is True:
                return True

            action_attempted_updated = {**action_attempted_current, "result_seen_by_shimo": True}
            patch_for_db = {"action_attempted": action_attempted_updated}

            await collection.update({"_key": doc_key_to_update, **patch_for_db})
            logger.info(f"已将文档 '{doc_key_to_update}' (action_id: {action_id_to_mark}) 的结果标记为已阅。")
            return True
        except Exception as e:
            logger.error(f"更新文档 '{doc_key_to_update}' 标记已阅时发生错误: {e}", exc_info=True)
            return False
