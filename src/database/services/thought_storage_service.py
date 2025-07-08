# src/database/services/thought_storage_service.py
import datetime
import uuid
from typing import Any

from arangoasync.exceptions import (
    DocumentUpdateError,
    TransactionAbortError,
)
from src.common.custom_logging.logging_config import get_logger
from src.database import ArangoDBConnectionManager

# 导入我们新的思想点模型
from src.database.models import CoreDBCollections, ThoughtChainDocument

logger = get_logger(__name__)

LATEST_THOUGHT_POINTER_KEY = "latest_thought_pointer"


class ThoughtStorageService:
    """服务类，负责处理思想链相关的数据库存储操作.

    这个服务类主要负责以下功能：
    - 确保思想链和状态指针集合的存在。
    - 保存新的思想点，并在必要时创建指向前一个思想点的边。
    - 获取最新的思想点文档。
    - 批量保存侵入性思维文档。
    - 获取随机未使用的侵入性思维文档，并标记为已使用。
    - 标记行动结果为已阅。
    这个类使用了流式事务来确保原子性和规避AQL的限制，避免了在高并发环境下可能出现的数据不一致问题。
    通过使用流式事务，我们可以在一个事务中完成多个操作，确保要么全部成功，要么全部失败，从而保证数据的一致性和完整性。

    Attributes:
        conn_manager (ArangoDBConnectionManager): 数据库连接管理器实例，用于执行数据库操作。
        thoughts_coll_name (str): 思想链集合的名称。
        thoughts_coll_full_name (str): 完整的思想链集合名称，用于构造 _id。
        state_coll_name (str): 状态指针集合的名称。
        edge_coll_name (str): 前置思想点边集合的名称。
        action_edge_coll_name (str): 行动结果边集合的名称。
        intrusive_pool_coll_name (str): 侵入性思维池集合的名称。
        main_thoughts_coll_name (str): 主思考集合的名称，用于存储主意识思考文档。
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.thoughts_coll_name = CoreDBCollections.THOUGHT_CHAIN
        self.thoughts_coll_full_name = f"{self.thoughts_coll_name}"  # 完整的集合名称，用于构造 _id
        self.state_coll_name = CoreDBCollections.SYSTEM_STATE
        self.edge_coll_name = CoreDBCollections.PRECEDES_THOUGHT
        self.action_edge_coll_name = CoreDBCollections.LEADS_TO_ACTION
        self.intrusive_pool_coll_name = CoreDBCollections.INTRUSIVE_POOL_COLLECTION
        self.main_thoughts_coll_name = CoreDBCollections.THOUGHTS_LEGACY
        logger.info(
            f"ThoughtStorageService (思想链版) 初始化完毕，将操作集合 '{self.thoughts_coll_name}'。"
        )

    async def initialize_infrastructure(self) -> None:
        """确保与思考相关的集合和图都已创建."""
        # 确保思想链和状态指针集合存在
        await self.conn_manager.ensure_collection_with_indexes(
            self.thoughts_coll_name,
            CoreDBCollections.INDEX_DEFINITIONS.get(self.thoughts_coll_name, []),
        )
        await self.conn_manager.ensure_collection_with_indexes(
            self.state_coll_name,
            [],  # 状态集合不需要额外索引
        )
        logger.info(f"'{self.thoughts_coll_name}' 和 '{self.state_coll_name}' 集合已初始化。")

        # 确保图和边集合存在 (这部分逻辑在 connection_manager 的 ensure_core_infrastructure 中处理)
        # 这里只是再次确认一下，确保万无一失
        if not self.conn_manager.main_graph:
            # 如果图还没初始化，就在这里提醒一下
            logger.warning(
                "主图未在ThoughtStorageService初始化时就绪，"
                "依赖于上游的 ensure_core_infrastructure 调用。"
            )

    async def save_thought_and_link(self, thought_data: ThoughtChainDocument) -> str | None:
        """神谕·最终形态：使用流式事务来保证原子性和规避AQL限制."""
        # 此处有个陷阱，见同目录下的 mention.md 文档。
        # 以下是正确的实现：
        # 声明我们要在这个事务里进行写操作的集合
        write_collections = [
            self.thoughts_coll_name,
            self.state_coll_name,
            self.edge_coll_name,
            self.action_edge_coll_name,
        ]

        trx = None  # 先把事务变量请出来
        try:
            # 步骤 1: 开启一个流式事务
            # 我们告诉数据库，接下来的一系列操作都属于同一个事务
            trx = await self.conn_manager.db.begin_transaction(write=write_collections)
            logger.debug(f"开启流式事务，ID: {trx.transaction_id}")

            # 在这个事务的上下文中，获取集合的句柄
            state_coll = trx.collection(self.state_coll_name)
            thoughts_coll = trx.collection(self.thoughts_coll_name)
            edge_coll = trx.collection(self.edge_coll_name)
            action_edge_coll = trx.collection(self.action_edge_coll_name)

            # 步骤 2: 获取上一个思想点的key
            pointer_doc = await state_coll.get(LATEST_THOUGHT_POINTER_KEY)
            last_thought_key = pointer_doc.get("latest_thought_key") if pointer_doc else None

            # 步骤 3: 插入新的思想点
            new_thought_doc_data = thought_data.to_dict()
            insert_result = await thoughts_coll.insert(new_thought_doc_data, return_new=True)
            new_thought = insert_result.get("new", {})
            new_thought_id = new_thought.get("_id")
            new_thought_key = new_thought.get("_key")

            if not new_thought_id or not new_thought_key:
                raise ValueError("插入新的思想点后，未能获取其_id或_key。")

            # 步骤 4: 创建指向前一个点的边
            if last_thought_key:
                # 直接从 key 构造 _id，避免多余的数据库读取
                last_thought_id = f"{self.thoughts_coll_full_name}/{last_thought_key}"
                edge_doc = {
                    "_from": last_thought_id,
                    "_to": new_thought_id,
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                }
                await edge_coll.insert(edge_doc)
                # 理论上，如果 last_thought_key 存在，那么对应的文档也应该存在。
                # 如果不存在，那说明数据不一致，这里就不再额外检查了，直接尝试插入边。
                # 如果插入边失败，事务会回滚。

            if action_id := new_thought.get("action_id"):
                action_log_id = f"{CoreDBCollections.ACTION_LOGS}/{action_id}"
                action_edge_doc = {
                    "_from": new_thought_id,
                    "_to": action_log_id,
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                }
                await action_edge_coll.insert(action_edge_doc)

            # 步骤 6: 更新指针
            # UPSERT逻辑在python里实现：先尝试更新，失败则插入
            try:
                await state_coll.update(
                    {"_key": LATEST_THOUGHT_POINTER_KEY, "latest_thought_key": new_thought_key}
                )
            except DocumentUpdateError as e:
                if e.error_code == 1202:  # Document not found
                    await state_coll.insert(
                        {"_key": LATEST_THOUGHT_POINTER_KEY, "latest_thought_key": new_thought_key}
                    )
                else:
                    raise e

            # 步骤 7: 提交事务！让所有操作生效！
            await trx.commit_transaction()
            logger.info(f"流式事务成功提交：思想点 '{new_thought_key}' 已串入思想链。")
            return new_thought_key

        except Exception as e:
            logger.error(f"思想链操作事务执行失败: {e}", exc_info=True)
            if trx:
                try:
                    # 如果出了任何问题，就回滚事务，撤销所有操作
                    await trx.abort_transaction()
                    logger.warning("事务已回滚。")
                except TransactionAbortError as abort_e:
                    logger.error(f"事务回滚失败: {abort_e}", exc_info=True)
            return None

    async def get_latest_thought_document(self) -> dict | None:
        """获取最新的思想点文档.

        这个方法会从状态集合中获取最新的思想点指针，然后返回对应的思想点文档。
        如果状态集合中没有最新思想点的指针，或者没有对应的思想点文档，则返回 None。

        Returns:
            dict | None: 最新的思想点文档，如果不存在则返回 None。
        """
        query = """
            LET latest_key = (
                FOR s IN @@state_coll
                    FILTER s._key == @pointer_key
                    LIMIT 1
                    RETURN s.latest_thought_key
            )[0]

            FILTER latest_key != null
            RETURN DOCUMENT(@@thoughts_coll, latest_key)
        """
        bind_vars = {
            "@state_coll": self.state_coll_name,
            "pointer_key": LATEST_THOUGHT_POINTER_KEY,
            "@thoughts_coll": self.thoughts_coll_name,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results:
                logger.debug(f"成功获取最新的思想点: {results[0].get('_key')}")
                return results[0]
            else:
                logger.info("思想链为空，还未有任何思考。")
                return None
        except Exception as e:
            logger.error(f"获取最新思想点时发生错误: {e}", exc_info=True)
            return None

    async def get_latest_main_thought_document(self, limit: int = 1) -> list[dict[str, Any]]:
        """获取最新的一个或多个主意识思考文档."""
        if limit <= 0:
            logger.warning("获取最新思考文档的 limit 参数必须为正整数。")
            return []
        query = """
            FOR doc IN @@collection
                SORT doc.timestamp DESC
                LIMIT @limit
                RETURN doc
        """
        bind_vars = {"@collection": self.main_thoughts_coll_name, "limit": limit}
        results = await self.conn_manager.execute_query(query, bind_vars)
        return results if results is not None else []

    async def save_intrusive_thoughts_batch(
        self, thought_document_list: list[dict[str, Any]]
    ) -> bool:
        """批量保存侵入性思维文档到数据库."""
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
            collection = await self.conn_manager.get_collection(self.intrusive_pool_coll_name)

            # ↓↓↓ 这才是真正的答案！直接 await！如果还需要超时，才在外面套 asyncio.timeout！ ↓↓↓
            results = await collection.insert_many(processed_documents_for_db, overwrite=False)

            successful_inserts = sum(bool(not r.get("error")) for r in results)
            if successful_inserts < len(processed_documents_for_db):
                errors = [
                    r.get("errorMessage", "未知数据库错误") for r in results if r.get("error")
                ]
                logger.warning(
                    f"批量保存侵入性思维：{successful_inserts}/{len(processed_documents_for_db)} "
                    f"条成功。部分错误: {errors[:3]}"
                )
            else:
                logger.info(f"已成功批量保存 {successful_inserts} 条侵入性思维，啊~ 全都进来了。")
            return successful_inserts > 0
        except Exception as e:
            # 现在，任何错误，包括可能的真实超时，都会在这里被捕获。
            logger.error(f"批量保存侵入性思维时发生严重错误: {e}", exc_info=True)
            return False

    async def get_random_unused_intrusive_thought_document(self) -> dict[str, Any] | None:
        """从侵入性思维池中获取一个随机的、未被使用过的侵入性思维文档."""
        try:
            # (这个方法的逻辑本来就是对的，因为它调用的是我们已经修正过的 execute_query)
            count_query = (
                "RETURN LENGTH(FOR doc IN @@collection FILTER doc.used == false LIMIT 1 RETURN 1)"
            )
            bind_vars_count = {"@collection": self.intrusive_pool_coll_name}
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
            bind_vars_query = {"@collection": self.intrusive_pool_coll_name}
            results = await self.conn_manager.execute_query(query, bind_vars_query)
            return results[0] if results else None

        except Exception as e:
            logger.error(f"获取随机未使用的侵入性思维失败: {e}", exc_info=True)
            return None

    async def mark_intrusive_thought_document_used(self, thought_doc_key: str) -> bool:
        """根据侵入性思维文档的 _key，将其标记为已使用."""
        if not thought_doc_key:
            logger.warning("标记已使用需要有效的 thought_doc_key。")
            return False
        try:
            collection = await self.conn_manager.get_collection(self.intrusive_pool_coll_name)

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
        """根据 action_id，将对应的思考文档标记为结果已阅.

        这个方法会在主思考集合中查找对应的 action_id，
        如果找到，则更新该文档的 action_attempted 字段，标记结果已阅。

        Args:
            action_id_to_mark (str): 要标记的 action_id。

        Returns:
            bool: 如果成功标记为已阅，返回 True；如果未找到对应的文档或发生错误，返回 False。
        """
        if not action_id_to_mark:
            return False

        find_query = "FOR doc IN @@collection FILTER doc.action_attempted.action_id == @action_id LIMIT 1 RETURN doc._key"  # noqa: E501
        bind_vars_find = {
            "@collection": self.main_thoughts_coll_name,
            "action_id": action_id_to_mark,
        }

        found_keys = await self.conn_manager.execute_query(find_query, bind_vars_find)
        if not found_keys:
            logger.warning(f"未找到 action_id 为 '{action_id_to_mark}' 的思考文档。")
            return False

        doc_key_to_update = found_keys[0]

        try:
            collection = await self.conn_manager.get_collection(self.main_thoughts_coll_name)
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
            logger.info(
                f"已将文档 '{doc_key_to_update}' "
                f"(action_id: {action_id_to_mark}) 的结果标记为已阅。"
            )
            return True
        except Exception as e:
            logger.error(f"更新文档 '{doc_key_to_update}' 标记已阅时发生错误: {e}", exc_info=True)
            return False
