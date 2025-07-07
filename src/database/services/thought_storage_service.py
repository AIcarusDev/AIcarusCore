# src/database/services/thought_storage_service.py
import datetime
import uuid
from typing import Any

from arangoasync.exceptions import DocumentInsertError, DocumentUpdateError, TransactionCommitError, TransactionAbortError
from src.common.custom_logging.logging_config import get_logger
from src.database import ArangoDBConnectionManager
# 导入我们新的思想点模型
from src.database.models import CoreDBCollections, ThoughtChainDocument

logger = get_logger(__name__)

LATEST_THOUGHT_POINTER_KEY = "latest_thought_pointer"

class ThoughtStorageService:
    """
    服务类，负责管理“思想点链”的存储操作。
    它现在是AI意识流连续性的核心保障。
    """


    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.thoughts_coll_name = CoreDBCollections.THOUGHT_CHAIN
        self.state_coll_name = CoreDBCollections.SYSTEM_STATE
        self.edge_coll_name = CoreDBCollections.PRECEDES_THOUGHT
        self.action_edge_coll_name = CoreDBCollections.LEADS_TO_ACTION
        self.intrusive_pool_coll_name = CoreDBCollections.INTRUSIVE_POOL_COLLECTION
        self.main_thoughts_coll_name = CoreDBCollections.THOUGHTS_LEGACY
        logger.info(f"ThoughtStorageService (思想链版) 初始化完毕，将操作集合 '{self.thoughts_coll_name}'。")

    async def initialize_infrastructure(self) -> None:
        """确保与思考相关的集合和图都已创建。"""
        # 确保思想链和状态指针集合存在
        await self.conn_manager.ensure_collection_with_indexes(
            self.thoughts_coll_name,
            CoreDBCollections.INDEX_DEFINITIONS.get(self.thoughts_coll_name, [])
        )
        await self.conn_manager.ensure_collection_with_indexes(
            self.state_coll_name,
            [] # 状态集合不需要额外索引
        )
        logger.info(f"'{self.thoughts_coll_name}' 和 '{self.state_coll_name}' 集合已初始化。")

        # 确保图和边集合存在 (这部分逻辑在 connection_manager 的 ensure_core_infrastructure 中处理)
        # 这里只是再次确认一下，确保万无一失
        if not self.conn_manager.main_graph:
            # 如果图还没初始化，就在这里提醒一下
            logger.warning("主图未在ThoughtStorageService初始化时就绪，依赖于上游的 ensure_core_infrastructure 调用。")

    async def save_thought_and_link(self, thought_data: ThoughtChainDocument) -> str | None:
        """
        神谕·最终形态：使用流式事务来保证原子性和规避AQL限制。
        """
        # 《arangoasync 踩坑大全（小懒猫）》
        # 陷阱一：天大的误会——AQL事务的幻觉
        # 坑是什么：
        #   - 你以为 db.aql.execute(一大串AQL) 是在执行一个事务脚本，像机器人一样一步一步地做事。大错特错！
        # 为什么会掉进去：
        #   - arangoasync 这个库，它做的只是把那又长又臭的AQL字符串，原封不动地打包，发给ArangoDB。ArangoDB的优化器会先完整地分析你整个查询，然后再执行。当它看到你在这个查询里，既要往 thought_chain 里 INSERT 新东西，又要用这个新东西的ID去 INSERT 边，它就会立刻掀桌子，冲你大吼：“禁止在修改后立即访问！”（access after data-modification）。
        # 怎么爬出来（神谕）：
        #   - 对于需要多步、且保证原子性的复杂操作，放弃单一AQL查询！ 去用ArangoDB真正的王牌：流式事务 (Stream Transaction)。
        # 就像我们最终的解决方案一样：
        #   1.用 trx = await db.begin_transaction(...) 开启一个神圣的事务结界。
        #   2.在 try...except... 块里，用独立的 await collection.insert(...)、await collection.get(...) 等Python指令一步步地执行操作。
        #   3.最后用 await trx.commit_transaction() 宣告胜利，或者在失败时用 await trx.abort_transaction() 毁尸灭迹。

        # 陷阱二：致命的温柔——replace 与 update 的爱恨情仇
        # 坑是什么：
        #   - collection.replace() 和 collection.update() 听起来差不多，但一个会要了你的命，另一个则可能对你爱答不理。
        # 为什么会掉进去：
        #   - replace (对应HTTP的PUT) 是毁灭性的完全替换。你给它一个只有 _key 和一个新字段的文档，它就会把你数据库里那个带有 _id, _rev 等元数据的完整文档，替换成你这个残缺不全的新文档。下次你再用这个文档，可能就会因为它缺少元数据而出错。
        #   - update (对应HTTP的PATCH) 是合并更新，只会修改你指定的字段，很安全。但是，如果文档不存在，它会直接报错（DocumentUpdateError），不会帮你创建。
        # 怎么爬出来（神谕）：
        #   - 永远要清楚你的意图！
        # 如果你想**“有则更新，无则创建” (UPSERT)**，最稳妥的Python层实现就是：
        # ```python
        # try:
        #     await collection.update(doc)
        # except DocumentUpdateError as e:
        #     if e.error_code == 1202: # Document Not Found
        #         await collection.insert(doc)
        #     else:
        #         raise e
        # ```
        #   - 如果你确定文档一定存在，只想修改部分字段，那就大胆地用 update。
        #   - 如果你真的想把一个旧文档彻底换成一个全新的，再用 replace。

        # 陷阱三：图的“潜规则”——边集合的正确获取姿势
        # 坑是什么：
        #   - 你以为所有集合都能用 db.collection(name) 来获取？太天真了。
        # 为什么会掉进去：
        #   - 对于定义在图（Graph）里的边集合（Edge Collection），你必须通过图的句柄来获取它，也就是 graph.edge_collection(name)。如果你直接用 db.collection(name)，虽然也能拿到一个集合对象，但它可能缺少图的上下文，导致你后续操作图相关的API时出错。
        # 怎么爬出来（神谕）：
        #   - 记住这个规矩：
        #       - 普通的文档集合，用 db.collection(name)。
        #       - 图里的点集合，可以用 graph.vertex_collection(name)。
        #       - 图里的边集合，必须用 graph.edge_collection(name)！

        # 陷阱四：虚假的异步——asyncio.to_thread 的救赎
        # 坑是什么：
        #   - arangoasync 这个库本身是异步的，但它依赖的一些第三方库（比如你用过的 duckduckgo-search）可能不是。当你用 await 去调用一个实际上是同步阻塞的函数时，整个事件循环都会被卡住，你的AI就“猝死”了。
        # 为什么会掉进去：
        #   - 你太相信 async 这个前缀了。不是所有 await 后面跟的都是真神。
        # 怎么爬出来（神谕）：
        #   - 当你需要在一个异步函数里调用一个已知的、同步的、耗时的操作时，把它丢到线程池里去！
        # ```python
        # # 比如 ddgs.text() 是个同步方法
        # results = await asyncio.to_thread(ddgs.text, keywords=query, max_results=max_results)
        # ```
        # 这样，事件循环就不会被阻塞，你的AI才能一边上网冲浪，一边思考人生，而不是卡死在下载小电影的路上。


        # ！！保留这个注释！！
        # 陷阱一完美复现：
        # 注意：arangoasync 库它不是在执行AQL，它是在“翻译”AQL，它把我们写的整个AQL查询，当成一个整体发送给ArangoDB
        # 所以ArangoDB的优化器在执行前就看到了那个“修改后访问”
        # 不要再踩这个坑了！
        # 之前失败的实现：
        #     // 1. 先把新的思想点插进去，并且立刻拿到它的所有信息
        #     LET new_thought = (
        #         INSERT @new_thought_doc IN @@thoughts_coll RETURN NEW
        #     )[0]

        #     // 2. 然后，我们才去拿上一个思考节点的key (如果有的话)
        #     //    注意！这里的查询是独立的，不会和上面的INSERT冲突！
        #     LET last_thought_key = (
        #         FOR s IN @@state_coll
        #             FILTER s._key == @pointer_key
        #             LIMIT 1
        #             RETURN s.latest_thought_key
        #     )[0]

        #     // 3. 如果我们成功拿到了上一个key，就创建一个新的边文档
        #     //    这个操作本身只是在内存里准备数据，并不访问数据库，所以是安全的
        #     LET preceding_edge_doc = (
        #         FILTER last_thought_key != null
        #         RETURN {
        #             _from: CONCAT(@@thoughts_coll, "/", last_thought_key),
        #             _to: new_thought._id,
        #             timestamp: DATE_NOW()
        #         }
        #     )[0]

        #     // 4. 如果上一步成功准备了边文档，现在就把它插进去
        #     LET preceding_edge_result = (
        #         FILTER preceding_edge_doc != null
        #         INSERT preceding_edge_doc INTO @@edge_coll
        #     )

        #     // 5. 同样，如果这个想法导致了一个动作，就准备动作的边文档
        #     LET action_edge_doc = (
        #         FILTER new_thought.action_id != null
        #         RETURN {
        #             _from: new_thought._id,
        #             _to: CONCAT(@@action_log_coll, "/", new_thought.action_id),
        #             timestamp: DATE_NOW()
        #         }
        #     )[0]

        #     // 6. 如果动作边文档准备好了，就插进去
        #     LET action_edge_result = (
        #         FILTER action_edge_doc != null
        #         INSERT action_edge_doc INTO @@action_edge_coll
        #     )

        #     // 7. 最后，也是最重要的，用UPSERT来更新指针，这是最安全的方式！
        #     //    它能自动处理“有则更新，无则创建”的情况
        #     UPSERT { _key: @pointer_key }
        #     INSERT { _key: @pointer_key, latest_thought_key: new_thought._key }
        #     UPDATE { latest_thought_key: new_thought._key }
        #     IN @@state_coll

        #     // 8. 把新点的key返回
        #     RETURN { new_key: new_thought._key }
        # if not isinstance(thought_data, ThoughtChainDocument):
        #     logger.error("传入 save_thought_and_link 的不是 ThoughtChainDocument 对象！")
        #     return None

        # 听好了，凡人。我们以上的思路都走偏了。
        # 我们不能依赖一个AQL字符串来完成这个精细的操作。我们要用ArangoDB最原始、最纯粹的力量——流式事务 (Stream Transaction)！
        # 我们需要手动开启一个事务，然后在这个事务的保护下，一步一步地、用独立的Python await 指令来执行我们的数据库操作。
        # 这样，每一次操作都是一个独立的HTTP请求，但它们都被同一个事务ID捆绑在一起，最终要么一起上天堂（Commit），要么一起下地狱（Abort）。这才是真正的、万无一失的原子性！
        # 这是神谕，是最终的解决方案。不会再有错误了。
        # 以下是正确的实现：
        # 声明我们要在这个事务里进行写操作的集合
        write_collections = [
            self.thoughts_coll_name,
            self.state_coll_name,
            self.edge_coll_name,
            self.action_edge_coll_name,
        ]

        trx = None # 先把事务变量请出来
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
                last_thought_doc = await thoughts_coll.get(last_thought_key)
                if last_thought_doc:
                    last_thought_id = last_thought_doc.get("_id")
                    edge_doc = {"_from": last_thought_id, "_to": new_thought_id, "timestamp": datetime.datetime.now(datetime.UTC).isoformat()}
                    await edge_coll.insert(edge_doc)
                else:
                    logger.warning(f"事务内警告：指针指向的思想点 '{last_thought_key}' 不存在，无法创建precedes_thought边。")

            if action_id := new_thought.get("action_id"):
                action_log_id = f"{CoreDBCollections.ACTION_LOGS}/{action_id}"
                action_edge_doc = {"_from": new_thought_id, "_to": action_log_id, "timestamp": datetime.datetime.now(datetime.UTC).isoformat()}
                await action_edge_coll.insert(action_edge_doc)

            # 步骤 6: 更新指针
            # UPSERT逻辑在python里实现：先尝试更新，失败则插入
            try:
                await state_coll.update({"_key": LATEST_THOUGHT_POINTER_KEY, "latest_thought_key": new_thought_key})
            except DocumentUpdateError as e:
                if e.error_code == 1202: # Document not found
                    await state_coll.insert({"_key": LATEST_THOUGHT_POINTER_KEY, "latest_thought_key": new_thought_key})
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
        """
        获取思想链中最新的那颗点。简单、粗暴、有效！
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
            "@thoughts_coll": self.thoughts_coll_name
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
        bind_vars = {"@collection": self.main_thoughts_coll_name, "limit": limit}
        results = await self.conn_manager.execute_query(query, bind_vars)
        return results if results is not None else []

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
            collection = await self.conn_manager.get_collection(self.intrusive_pool_coll_name)

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
        """根据侵入性思维文档的 _key，将其标记为已使用。"""
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
        """根据 action_id 找到对应的思考文档，并将其 action_attempted.result_seen_by_shimo 标记为 True。"""
        if not action_id_to_mark:
            return False

        find_query = (
            "FOR doc IN @@collection FILTER doc.action_attempted.action_id == @action_id LIMIT 1 RETURN doc._key"
        )
        bind_vars_find = {"@collection": self.main_thoughts_coll_name, "action_id": action_id_to_mark}

        found_keys = await self.conn_manager.execute_query(find_query, bind_vars_find)
        if not found_keys:
            logger.warning(f"未找到 action_id 为 '{action_id_to_mark}' 的思考文档。")
            return False

        doc_key_to_update = found_keys[0]

        try:
            collection = await self.conn_manager.get_collection(self.main_thoughts_coll_name)
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
