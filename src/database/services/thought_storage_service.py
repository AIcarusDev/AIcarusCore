# src/database/services/thought_storage_service.py
import asyncio
import datetime
import uuid # 用于生成唯一的 _key (如果上层数据未提供)
from typing import Any, Dict, List, Optional

from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections # 导入连接管理器和集合常量
from arango.exceptions import DocumentInsertError, DocumentUpdateError # ArangoDB特定的异常类型

from src.common.custom_logging.logger_manager import get_logger # 导入日志记录器

logger = get_logger("AIcarusCore.DB.ThoughtService") # 获取该服务的日志记录器实例

class ThoughtStorageService:
    """
    服务类，负责管理主要思考（Main Thoughts）和侵入性思维（Intrusive Thoughts）
    相关的存储操作。它依赖于 ArangoDBConnectionManager 来执行底层的数据库交互。
    """
    MAIN_THOUGHTS_COLLECTION = CoreDBCollections.THOUGHTS # 主意识思考记录集合名称
    INTRUSIVE_POOL_COLLECTION = CoreDBCollections.INTRUSIVE_THOUGHTS_POOL # 侵入性思维池集合名称

    def __init__(self, conn_manager: ArangoDBConnectionManager):
        """
        初始化 ThoughtStorageService。

        Args:
            conn_manager: ArangoDBConnectionManager 的实例，用于数据库连接和操作。
        """
        self.conn_manager = conn_manager
        self.logger = logger

    async def initialize_infrastructure(self) -> None:
        """
        确保与思考相关的集合及其特定索引已按配置创建。
        此方法通常在系统启动时，在 ConnectionManager 初始化核心基础设施后被调用，
        或者由 ConnectionManager 统一处理所有核心集合的索引。
        这里假设 ConnectionManager 的 ensure_core_infrastructure 已经处理了基础集合创建。
        此方法主要确保特定于此服务的索引被应用。
        """
        # 初始化主思考集合的特定索引
        main_thought_indexes = CoreDBCollections.INDEX_DEFINITIONS.get(self.MAIN_THOUGHTS_COLLECTION, [])
        await self.conn_manager.ensure_collection_with_indexes(self.MAIN_THOUGHTS_COLLECTION, main_thought_indexes)
        self.logger.info(f"'{self.MAIN_THOUGHTS_COLLECTION}' 集合及其特定索引已初始化。")

        # 初始化侵入性思维池集合的特定索引
        intrusive_indexes = CoreDBCollections.INDEX_DEFINITIONS.get(self.INTRUSIVE_POOL_COLLECTION, [])
        await self.conn_manager.ensure_collection_with_indexes(self.INTRUSIVE_POOL_COLLECTION, intrusive_indexes)
        self.logger.info(f"'{self.INTRUSIVE_POOL_COLLECTION}' 集合及其特定索引已初始化。")

    async def get_main_thought_document_by_key(self, doc_key: str) -> Optional[Dict[str, Any]]:
        """
        获取具有指定 _key 的主意识思考文档。

        Args:
            doc_key: 要获取的文档的 _key。

        Returns:
            思考文档字典，如果未找到或发生错误则返回 None。
        """
        if not doc_key:
            self.logger.warning("获取主思考文档需要一个有效的 doc_key，小猫咪舔不到东西啦。")
            return None
        try:
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION) # 确保集合名正确
            # collection.get 是同步方法，需要用 to_thread 包装
            doc = await asyncio.to_thread(collection.get, doc_key)
            if doc:
                self.logger.debug(f"通过 key '{doc_key}' 成功获取到主思考文档的小穴内容。")
            else:
                self.logger.warning(f"通过 key '{doc_key}' 未找到主思考文档，小猫咪舔了个寂寞。")
            return doc # collection.get 在找不到文档时会返回 None
        except Exception as e:
            self.logger.error(f"通过 key '{doc_key}' 获取主思考文档时，小猫咪高潮失败了: {e}", exc_info=True)
            return None

    async def save_main_thought_document(self, thought_document: Dict[str, Any]) -> Optional[str]:
        """
        保存一个主意识思考过程的文档。
        方法期望 `thought_document` 是一个已经构建好的、准备存入数据库的Python字典。
        如果文档中未提供 '_key'，将为其自动生成一个UUID。
        如果文档中未提供 'timestamp'，将使用当前的UTC时间（ISO格式）。

        Args:
            thought_document: 要保存的思考文档字典。

        Returns:
            成功保存的文档的 _key (字符串)，如果保存失败则返回 None。
        """
        if not isinstance(thought_document, dict): # 基本的类型检查
            self.logger.error(f"保存主思考文档失败：输入数据不是有效的字典。得到类型: {type(thought_document)}")
            return None

        # 确保文档有 _key 和 timestamp 字段
        if "_key" not in thought_document:
            thought_document["_key"] = str(uuid.uuid4())
        if "timestamp" not in thought_document: # timestamp 通常应为ISO格式的字符串，代表思考发生的时间
            thought_document["timestamp"] = datetime.datetime.now(datetime.UTC).isoformat()
        
        doc_key_for_log = thought_document.get("_key", "未知Key") # 用于日志记录

        try:
            # 获取（并确保存在）主思考集合的实例
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION)
            # 尝试插入文档，如果 _key 已存在则不覆盖 (overwrite=False)
            result = await asyncio.to_thread(collection.insert, thought_document, overwrite=False)
            
            if result and result.get("_key"): # 检查返回结果是否包含 _key
                self.logger.debug(f"主思考文档 '{result.get('_key')}' 已成功保存。")
                return result.get("_key")
            else: # 这种情况不常见，如果insert没有抛错但没有返回_key
                self.logger.error(f"保存主思考文档 '{doc_key_for_log}' 后未能从数据库返回结果中获取 _key。结果: {result}")
                return None
        except DocumentInsertError: # 特别捕获因 _key 重复导致的插入失败
            self.logger.warning(f"尝试插入主思考文档失败，因为键 '{doc_key_for_log}' 可能已存在。操作被跳过。")
            return doc_key_for_log # 如果是因为重复键而失败，可以认为文档已存在，返回现有的key
        except Exception as e: # 捕获其他所有可能的数据库操作错误
            self.logger.error(f"保存主思考文档 '{doc_key_for_log}' 到数据库时发生严重错误: {e}", exc_info=True)
            return None

    async def get_latest_main_thought_document(self, limit: int = 1) -> List[Dict[str, Any]]:
        """
        获取最新的一个或多个主意识思考文档。
        文档按 'timestamp' 字段降序排列。

        Args:
            limit: 要获取的最新文档数量，默认为1。

        Returns:
            包含最新思考文档（字典）的列表，如果查询失败或无结果则返回空列表。
        """
        if limit <= 0: # 对 limit 参数进行基本校验
            self.logger.warning("获取最新思考文档的 limit 参数必须为正整数。")
            return []
        
        # 使用 @@collection 将集合名称作为绑定变量传入，这是一种更安全的AQL实践
        query = f"""
            FOR doc IN @@collection 
                SORT doc.timestamp DESC 
                LIMIT @limit
                RETURN doc
        """
        bind_vars = {"@collection": self.MAIN_THOUGHTS_COLLECTION, "limit": limit}
        
        results = await self.conn_manager.execute_query(query, bind_vars)
        return results if results is not None else [] # execute_query 在错误时返回 None，这里统一返回列表

    async def update_action_status_in_thought_document(
        self, doc_key: str, action_id: str, status_update_dict: Dict[str, Any]
    ) -> bool:
        """
        更新特定思考文档（由 `doc_key` 指定）中，内嵌的 `action_attempted` 对象里
        与特定 `action_id` 匹配的动作的状态。
        `status_update_dict` 包含了要合并到该 `action_attempted` 对象中的新字段和值。
        """
        if not doc_key or not action_id: 
            self.logger.warning("更新思考文档中的动作状态需要有效的 doc_key 和 action_id。小猫咪没抓手了！")
            return False
        if not status_update_dict or not isinstance(status_update_dict, dict):
            self.logger.warning("更新思考文档中的动作状态需要一个非空的 status_update_dict。小猫咪的玩具丢了！")
            return False

        try:
            # --- 性感小猫咪的诊断探针 Start ---
            self.logger.debug(f"主人，进入 update_action_status_in_thought_document 方法，doc_key: {doc_key}, action_id: {action_id}")
            self.logger.debug(f"  status_update_dict 内容: {status_update_dict}")
            if self.conn_manager:
                self.logger.debug(f"  self.conn_manager 的类型: {type(self.conn_manager)}")
            else:
                self.logger.error("  警告！self.conn_manager 是 None！这很不妙！")
                return False # 如果连接管理器都没有，后面肯定会出问题

            self.logger.debug(f"  检查 asyncio 模块: 类型是 {type(asyncio)}")
            if hasattr(asyncio, 'to_thread'):
                self.logger.debug(f"  asyncio.to_thread 的类型: {type(asyncio.to_thread)}")
                if not callable(asyncio.to_thread):
                    self.logger.error("  天啊撸！asyncio.to_thread 不是一个可调用的东西！它变成骚东西了！")
            else:
                self.logger.error("  致命错误！asyncio 模块居然没有 to_thread 这个小穴！")
            # --- 性感小猫咪的诊断探针 End ---

            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION) # 获取集合对象
            
            # --- 性感小猫咪的进一步诊断探针 Start ---
            self.logger.debug(f"  获取到的 'collection' 变量的类型: {type(collection)}")
            if collection is None:
                self.logger.error("  致命错误！'collection' 居然是 None！conn_manager.get_collection() 没吐出好东西！")
                return False
            
            if hasattr(collection, 'update'):
                self.logger.debug(f"  collection.update 属性的类型: {type(collection.update)}")
                if not callable(collection.update):
                    self.logger.error(f"  啊哦！collection.update (类型: {type(collection.update)}) 不是一个可调用的方法！它肯定被什么骚东西附身了！")
            else:
                self.logger.error(f"  致命错误！获取到的 collection (类型: {type(collection)}) 没有 'update' 这个性感方法！")
            # --- 性感小猫咪的进一步诊断探针 End ---
            
            doc_to_update = await asyncio.to_thread(collection.get, doc_key) # 使用 collection.get
            if not doc_to_update: 
                self.logger.error(f"无法更新动作状态：找不到思考文档 '{doc_key}'，小猫咪舔不到。")
                return False

            action_attempted_current = doc_to_update.get("action_attempted") 
            if not isinstance(action_attempted_current, dict) or \
               action_attempted_current.get("action_id") != action_id:
                self.logger.error(
                    f"在思考文档 '{doc_key}' 中找不到与 action_id '{action_id}' 匹配的 'action_attempted' 对象，"
                    f"或者该对象的结构不正确，像穿错了情趣内衣。当前 'action_attempted' 内容: {action_attempted_current}"
                )
                return False
            
            updated_action_data = {**action_attempted_current, **status_update_dict}
            patch_document_for_db = {"action_attempted": updated_action_data} 
            
            self.logger.debug(f"准备用这个补丁来更新小穴: {patch_document_for_db}")
            # 下面这行就是我们要重点观察的，确保 collection.update 是个方法
            await asyncio.to_thread(collection.update, {"_key": doc_key, **patch_document_for_db}) # 使用 collection.update
            self.logger.info(f"思考文档 '{doc_key}' 中动作 '{action_id}' 的状态已被小猫咪成功更新为: {status_update_dict}，爽！")
            return True
        except DocumentUpdateError as e: 
             self.logger.error(f"更新思考文档 '{doc_key}' 中动作 '{action_id}' 的状态时，数据库高潮失败: {e}", exc_info=True)
             return False
        except TypeError as te: # 专门捕获 TypeError，看看是不是我们怀疑的那个
            self.logger.error(f"啊！更新思考文档 '{doc_key}' 中动作 '{action_id}' 状态时捕获到 TypeError: {te}！就是这个小坏蛋！", exc_info=True)
            return False
        except Exception as e: 
            self.logger.error(f"更新思考文档 '{doc_key}' 中动作 '{action_id}' 状态时发生意外的痉挛（错误）: {e}", exc_info=True)
            return False

    async def save_intrusive_thoughts_batch(self, thought_document_list: List[Dict[str, Any]]) -> bool:
        """
        批量保存侵入性思维文档到数据库。
        期望列表中的每个字典都已准备好作为独立的数据库文档。
        此方法会确保每个文档都有 '_key', 'timestamp_generated', 'used' 字段（如果缺失）。

        Args:
            thought_document_list: 包含侵入性思维文档（字典）的列表。

        Returns:
            如果至少有一个文档成功保存，则返回 True，否则返回 False。
        """
        if not thought_document_list: # 如果列表为空，则无需操作
            # self.logger.debug("没有侵入性思维需要批量保存。") # 这条日志可能过于频繁
            return True # 认为空列表的保存操作是“成功”的（无事发生）

        current_time_iso = datetime.datetime.now(datetime.UTC).isoformat() # 获取当前ISO格式的时间戳
        processed_documents_for_db: List[Dict[str, Any]] = [] # 用于存放处理后准备插入的文档

        for doc_data in thought_document_list: # 遍历输入的每个思维文档数据
            if not isinstance(doc_data, dict): # 跳过无效的非字典项目
                self.logger.warning(f"侵入性思维列表中发现非字典项目: {type(doc_data)}，已跳过。")
                continue
            
            final_doc = doc_data.copy() # 创建副本以避免修改原始输入
            # 确保必要的字段存在并具有正确的值
            if "_key" not in final_doc: # 如果没有提供_key，则生成一个新的UUID作为_key
                final_doc["_key"] = str(uuid.uuid4())
            if "timestamp_generated" not in final_doc: # 如果没有生成时间戳，则使用当前时间
                final_doc["timestamp_generated"] = current_time_iso
            if "used" not in final_doc: # 确保有 'used' 状态字段，默认为 False
                final_doc["used"] = False
            processed_documents_for_db.append(final_doc)

        if not processed_documents_for_db: # 如果处理后列表为空（例如所有输入项都无效）
            self.logger.info("经过预处理后，没有有效的侵入性思维文档需要批量保存。")
            return False # 或者 True，取决于业务逻辑如何定义空操作的成功

        try:
            collection = await self.conn_manager.get_collection(self.INTRUSIVE_POOL_COLLECTION)
            # 执行批量插入，如果文档的 _key 已存在则不覆盖 (overwrite=False)
            results = await asyncio.to_thread(collection.insert_many, processed_documents_for_db, overwrite=False)
            
            # insert_many 返回一个结果列表，每个元素对应一个输入文档，包含成功信息或错误详情
            successful_inserts = sum(1 for r in results if not r.get("error")) # 计算成功插入的数量
            if successful_inserts < len(processed_documents_for_db): # 如果有部分插入失败
                errors = [r.get("errorMessage", "未知数据库错误") for r in results if r.get("error")]
                self.logger.warning(
                    f"批量保存侵入性思维：{successful_inserts}/{len(processed_documents_for_db)} 条成功。"
                    f"部分错误详情 (最多显示3条): {errors[:3]}"
                )
            else: # 如果全部成功
                self.logger.info(f"已成功批量保存 {successful_inserts} 条侵入性思维。")
            return successful_inserts > 0 # 只要有至少一条成功插入，就认为操作部分成功
        except Exception as e: # 捕获批量插入过程中可能发生的其他所有严重错误
            self.logger.error(f"批量保存侵入性思维时发生严重错误: {e}", exc_info=True)
            return False

    async def get_random_unused_intrusive_thought_document(self) -> Optional[Dict[str, Any]]:
        """从侵入性思维池中获取一个随机的、未被使用过的侵入性思维文档。"""
        try:
            # 首先检查是否存在任何未被使用的思维，以避免对可能为空的结果集执行 RAND() 排序（效率低或可能出错）
            # 使用 @@collection 将集合名称作为绑定变量传入，是一种更安全的AQL实践
            count_query = f"RETURN LENGTH(FOR doc IN @@collection FILTER doc.used == false LIMIT 1 RETURN 1)"
            bind_vars_count = {"@collection": self.INTRUSIVE_POOL_COLLECTION}
            count_result = await self.conn_manager.execute_query(count_query, bind_vars_count)

            if not count_result or count_result[0] == 0: # 如果没有未使用的思维
                self.logger.info("侵入性思维池中当前没有未被使用过的思维可供随机获取。")
                return None

            # 如果存在未使用的思维，则进行随机抽取
            # 注意: SORT RAND() 在非常大的集合上性能可能不佳。
            # 对于超大集合，可以考虑其他随机抽样策略（例如，获取总数后随机选择一个偏移量）。
            # 但对于中小型池，此方法通常是可接受且简便的。
            query = f"""
                FOR doc IN @@collection 
                    FILTER doc.used == false 
                    SORT RAND() 
                    LIMIT 1
                    RETURN doc
            """
            bind_vars_query = {"@collection": self.INTRUSIVE_POOL_COLLECTION}
            results = await self.conn_manager.execute_query(query, bind_vars_query)
            return results[0] if results else None # execute_query 在错误时可能返回 None

        except Exception as e: # 捕获查询过程中可能发生的任何错误
            self.logger.error(f"获取随机未使用的侵入性思维失败: {e}", exc_info=True)
            return None

    async def mark_intrusive_thought_document_used(self, thought_doc_key: str) -> bool:
        """
        根据侵入性思维文档的 _key，将其标记为已使用 (将 'used' 字段设为 True)。
        """
        if not thought_doc_key: # 基本参数校验
            self.logger.warning("无法标记侵入性思维为已使用：未提供有效的 thought_doc_key。")
            return False
        try:
            collection = await self.conn_manager.get_collection(self.INTRUSIVE_POOL_COLLECTION)
            # 在更新前，先检查文档是否存在，避免不必要的更新操作或错误日志
            if not await asyncio.to_thread(collection.has, thought_doc_key):
                self.logger.warning(f"无法将侵入性思维 '{thought_doc_key}' 标记为已使用：该文档未在数据库中找到。")
                return False
            
            # 执行部分更新，将 'used' 字段设为 True。ArangoDB的 update 默认 merge=True。
            await asyncio.to_thread(collection.update, {"_key": thought_doc_key, "used": True})
            self.logger.debug(f"侵入性思维 '{thought_doc_key}' 已成功标记为已使用。")
            return True
        except DocumentUpdateError as e: # ArangoDB 特定的文档更新错误
            self.logger.error(f"标记侵入性思维 '{thought_doc_key}' 为已使用时，数据库更新失败: {e}", exc_info=True)
            return False
        except Exception as e: # 捕获其他所有可能的错误，例如 DocumentNotFoundError (虽然前面已经用 has 检查了)
            self.logger.error(f"标记侵入性思维 '{thought_doc_key}' 为已使用时发生意外错误: {e}", exc_info=True)
            return False

    async def mark_action_result_as_seen(self, action_id_to_mark: str) -> bool:
        """
        根据 action_id 找到对应的思考文档，并将其 action_attempted.result_seen_by_shuang 标记为 True。
        """
        if not action_id_to_mark:
            self.logger.warning("需要一个有效的 action_id 才能将其结果标记为已阅。")
            return False

        self.logger.debug(f"尝试将 action_id '{action_id_to_mark}' 的结果标记为已阅。")

        # 步骤1: 通过 action_id 查询文档的 _key
        find_query = f"""
            FOR doc IN @@collection
                FILTER doc.action_attempted.action_id == @action_id
                LIMIT 1
                RETURN doc._key
        """
        bind_vars_find = {"@collection": self.MAIN_THOUGHTS_COLLECTION, "action_id": action_id_to_mark}
        
        try:
            found_keys = await self.conn_manager.execute_query(find_query, bind_vars_find)
        except Exception as e_query:
            self.logger.error(f"查询 action_id '{action_id_to_mark}' 对应的文档key时出错: {e_query}", exc_info=True)
            return False
            
        if not found_keys:
            self.logger.warning(f"未找到 action_id 为 '{action_id_to_mark}' 的思考文档来标记结果为已阅。")
            return False
            
        doc_key_to_update = found_keys[0]
        self.logger.debug(f"找到文档key '{doc_key_to_update}' 对应 action_id '{action_id_to_mark}'。")

        # 步骤2: 获取并更新文档
        try:
            collection = await self.conn_manager.get_collection(self.MAIN_THOUGHTS_COLLECTION)
            # 使用 to_thread 执行同步的 get 方法
            doc_to_update = await asyncio.to_thread(collection.get, doc_key_to_update)
            
            if not doc_to_update:
                self.logger.error(f"获取文档key '{doc_key_to_update}' 失败，无法标记已阅。")
                return False

            action_attempted_current = doc_to_update.get("action_attempted")
            if not isinstance(action_attempted_current, dict):
                self.logger.error(f"文档 '{doc_key_to_update}' 中的 action_attempted 结构不正确，无法标记已阅。")
                return False
            
            # 如果已经是 True，则无需更新，直接返回成功
            if action_attempted_current.get("result_seen_by_shuang") is True:
                self.logger.info(f"动作ID '{action_id_to_mark}' (文档key: {doc_key_to_update}) 的结果已经被标记为已阅，无需重复操作。")
                return True

            action_attempted_updated = action_attempted_current.copy()
            action_attempted_updated["result_seen_by_shuang"] = True
            
            patch_for_db = {"action_attempted": action_attempted_updated}
            
            # 使用 to_thread 执行同步的 update 方法
            await asyncio.to_thread(collection.update, {"_key": doc_key_to_update, **patch_for_db})
            self.logger.info(f"已成功将文档 '{doc_key_to_update}' (action_id: {action_id_to_mark}) 的 action_attempted.result_seen_by_shuang 标记为 true。")
            return True
        except DocumentUpdateError as e_update:
            self.logger.error(f"更新文档 '{doc_key_to_update}' (action_id: {action_id_to_mark}) 标记已阅时数据库操作失败: {e_update}", exc_info=True)
            return False
        except Exception as e_general:
            self.logger.error(f"更新文档 '{doc_key_to_update}' (action_id: {action_id_to_mark}) 标记已阅时发生意外错误: {e_general}", exc_info=True)
            return False
