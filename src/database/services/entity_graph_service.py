# src/database/services/entity_graph_service.py (TypeDB v2.6 Refactored)
import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid
from src.database.core.typedb_connection_manager import TypeDBConnectionManager
from src.database.models import (
    EntityDocument,
    PlatformDetails,
)
from typedb.driver import TransactionType

logger = get_logger(__name__)

SELF_PROFILE_ID = "aic_person_0"

class EntityGraphService:
    """(TypeDB版) 负责管理实体与实体侧写之间的关系图谱.

    这是所有客观实体（账户、会话、平台）及其关系的唯一管理者。
    """

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("EntityGraphService (TypeDB) 初始化完成。")

    # --- 核心实体创建与关联 ---

    async def _update_account_nickname_if_changed(self,
            tx,
            account_uid: str,
            new_nickname: str
        ) -> None:
        """事务内辅助函数：如果昵称变化，则更新."""
        # 1. 查找旧昵称
        match_query = f'match $a isa account, has account-uid "{account_uid}", has nickname $n; select $n;'
        response = tx.query(match_query).resolve()
        answers = list(response.as_concept_rows())

        if answers and (old_nick_concept := answers[0].get("n")):
            old_nick = old_nick_concept.as_attribute().get_value().as_string()
            if old_nick == new_nickname:
                return # 昵称未变，无需操作

            # 2. 删除旧昵称
            delete_query = f'match $a isa account, has account-uid "{account_uid}", has nickname "{old_nick}"; delete $a has nickname "{old_nick}";'
            tx.query(delete_query).resolve()

        # 3. 插入新昵称
        insert_query = f'match $a isa account, has account-uid "{account_uid}"; insert $a has nickname "{new_nickname}";'
        tx.query(insert_query).resolve()
        logger.debug(f"已更新账户 '{account_uid}' 的昵称为 '{new_nickname}'。")

    async def update_conversation_membership_status(self, conversation_entity_uid: str, status: str) -> bool:
        # TODO:这个方法不适用于新版数据库，需要重构跟上新版本。
        """原子性地更新一个会话实体的成员状态。"""
        query = """
            UPDATE @key WITH { details: { membership_status: @status } }
            IN @@collection OPTIONS { mergeObjects: true }
        """
        bind_vars = {
            "key": conversation_entity_uid,
            "status": status,
            "@collection": CoreDBCollections.ENTITIES,
        }
        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.info(f"已更新会话实体 '{conversation_entity_uid}' 的成员状态为 '{status}'。")
            return True
        except Exception as e:
            logger.error(f"更新会话 '{conversation_entity_uid}' 成员状态时失败: {e}", exc_info=True)
            return False

    async def find_or_create_profile_and_account_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """原子性地查找或创建“账户”实体及其关联的“个人”档案.

        这是处理新出现用户的核心入口点。
        """
        if not user_info or not user_info.user_id:
            return None, None

        account_uid = f"{platform}_{user_info.user_id}"
        nickname = (user_info.user_nickname or "").replace('"', '\\"')

        # 模式：先读，后写。这是在 TypeDB 中实现 "Find or Create" 的标准模式。
        # 1. 尝试查找
        find_query = f"""
        match
            $acc isa account, has account-uid "{account_uid}";
            (owner: $p, owned-account: $acc) isa identity-ownership;
            $p has person-uid $p_uid;
        select $p_uid, $acc.account-uid;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read():
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(find_query).resolve()
                answers = list(response.as_concept_rows())
                if answers:
                    p_uid = answers[0].get("p_uid").as_attribute().get_value().as_string()
                    a_uid_concept = answers[0].get("account-uid")
                    a_uid = a_uid_concept.as_attribute().get_value().as_string() if a_uid_concept else None
                    return p_uid, a_uid
                return None, None

        profile_id, entity_uid = await asyncio.to_thread(db_read)

        if entity_uid:
            # TODO: 在这里可以添加更新 nickname 等信息的逻辑
            return profile_id, entity_uid
        else:
            # 2. 如果找不到，则创建
            return await self.create_new_profile_with_account_entity(user_info, platform)

    async def create_new_profile_with_account_entity(
        self,
        user_info: ProtocolUserInfo,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """原子性地创建新的 Profile、Account 实体及它们之间的关系。"""
        profile_uid = SELF_PROFILE_ID if is_self else f"profile_{uuid.uuid4().hex[:12]}"
        account_uid = f"{platform}_{user_info.user_id}"
        nickname = (user_info.user_nickname or "").replace('"', '\\"')

        # TypeQL Logic:
        # 1. 如果是 is_self，先用 match-else-insert 模式确保唯一的 person 实体存在。
        # 2. 然后插入新的 account 实体。
        # 3. 最后插入 identity-ownership 关系，将它们连接起来。

        # 由于 TypeDB Python Driver 的限制，我们将这些操作放在一个事务中
        # 但需要分步执行查询。

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_write():
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # Step 1: 确保 Person 存在
                if is_self:
                    # 查找，如果不存在则创建
                    find_person_query = f'match $p isa person, has person-uid "{profile_uid}"; get $p;'
                    result = tx.query(find_person_query).resolve()
                    if not list(result.as_concept_rows()):
                        insert_person_query = f'insert $p isa person, has person-uid "{profile_uid}";'
                        tx.query(insert_person_query).resolve()

                else:
                    insert_person_query = f'insert $p isa person, has person-uid "{profile_uid}";'
                    tx.query(insert_person_query).resolve()

                # Step 2: 插入 Account 并建立关系
                insert_account_relation_query = f"""
                match $p isa person, has person-uid "{profile_uid}";
                insert $acc isa account,
                    has account-uid "{account_uid}",
                    has nickname "{nickname}";
                (owner: $p, owned-account: $acc) isa identity-ownership;
                """
                tx.query(insert_account_relation_query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_write)
            return profile_uid, account_uid
        except Exception as e:
            logger.error(f"创建 Profile 和 Account Entity 的事务执行失败: {e}", exc_info=True)
            return None, None

    async def update_presence_in_conversation(
        self,
        account_entity_uid: str,
        conversation_entity_uid: str,
        user_info: ProtocolUserInfo,
        conversation_name: str | None,
    ) -> bool:
        """更新一个账户在某个会话中的存在关系（membership）。"""
        cardname = (user_info.user_cardname or "").replace('"', '\\"')
        perm_level = (user_info.permission_level or "member").replace('"', '\\"')
        timestamp = int(time.time() * 1000)

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_upsert_membership():
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # Step 1: 删除旧的 membership 关系（如果有）
                delete_query = f"""
                match
                    $acc isa account, has account-uid "{account_entity_uid}";
                    $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $mem (member: $acc, group: $conv) isa membership;
                delete $mem isa membership;
                """
                tx.query(delete_query).resolve()

                # Step 2: 插入新的 membership 关系
                insert_query = f"""
                match
                    $acc isa account, has account-uid "{account_entity_uid}";
                    $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
                insert
                    (member: $acc, group: $conv) isa membership,
                        has cardname "{cardname}",
                        has permission-level "{perm_level}",
                        has timestamp {timestamp};
                """
                tx.query(insert_query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_upsert_membership)
            logger.debug(f"成功更新存在关系: Account '{account_entity_uid}' in Conv '{conversation_entity_uid}'")
            return True
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)
            return False

    async def get_or_create_platform_entity(self, platform_id: str, display_name: str | None = None) -> EntityDocument | None:
        """原子性地获取或创建一个平台实体。"""
        async with self._platform_entity_lock:
            if platform_id in self._platform_entity_cache:
                return self._platform_entity_cache[platform_id]

            find_query = f'match $p isa platform, has platform-uid "{platform_id}"; get $p;'

            driver = self.conn_manager.get_driver()
            db_name = self.conn_manager.get_database_name()

            def db_op():
                with driver.transaction(db_name, TransactionType.WRITE) as tx:
                    response = tx.query(find_query).resolve()
                    answers = list(response.as_concept_rows())
                    if answers:
                        # 实体已存在，无需创建
                        # TODO: 这里可以添加逻辑来从 Concept 对象重建 EntityDocument
                        logger.debug(f"平台实体 '{platform_id}' 已存在。")
                        return {"_key": platform_id, "details": {"platform": platform_id, "display_name": display_name}}

                    # 实体不存在，创建它
                    insert_query = f"""
                    insert $p isa platform,
                        has platform-uid "{platform_id}",
                        has display-name "{(display_name or platform_id).replace('"', '\\"')}";
                    """
                    tx.query(insert_query).resolve()
                    tx.commit()
                    logger.info(f"平台实体 '{platform_id}' 创建成功。")
                    return {
                        "_key": platform_id,
                        "details": {"platform": platform_id, "display_name": display_name}
                    }

            try:
                # 简化处理，直接返回一个模拟的字典结构
                doc_dict = await asyncio.to_thread(db_op)
                if doc_dict:
                    entity_doc = EntityDocument(
                        _key=doc_dict["_key"],
                        entity_uid=doc_dict["_key"],
                        entity_type="platform",
                        details=PlatformDetails(
                            platform_id=platform_id,
                            display_name=display_name or platform_id
                        )
                    )
                    self._platform_entity_cache[platform_id] = entity_doc
                    return entity_doc
                return None
            except Exception as e:
                logger.error(f"获取或创建平台实体 '{platform_id}' 失败: {e}", exc_info=True)
                return None

    async def get_all_self_entities(self) -> list[dict[str, Any]]:
        """获取“祂”自身关联的所有平台账户实体信息."""
        query = f"""
        match
            $p isa person, has person-uid "{SELF_PROFILE_ID}";
            (owner: $p, owned-account: $acc) isa identity-ownership;
            $acc has account-uid $uid;
            $acc has nickname $nick;
        select $uid, $nick;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read():  # noqa: ANN202
            entities = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(query).resolve()
                for answer in response.as_concept_rows():
                    uid = answer.get("uid").as_attribute().get_value().as_string()
                    platform = uid.split('_')[0]
                    entities.append({
                        "entity_uid": uid,
                        "details": {
                            "platform": platform,
                            "platform_id": uid.replace(f"{platform}_", ""),
                            "nickname": answer.get("nick").as_attribute().get_value().as_string(),
                        }
                    })
            return entities

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取自身所有平台实体信息时失败: {e}", exc_info=True)
            return []

    async def get_pending_friend_requests(self, platform_id: str) -> list[dict[str, Any]]:
        """获取平台的待处理好友请求信息."""
        query = f"""
        match
            $acc isa account, has account-uid like "{platform_id}_.*";
            $acc has flag $f;
            $acc has comment $c;
            $acc has nickname $nick;
            $acc has account-uid $uid;
        select $uid, $nick, $f, $c;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read():
            """获取平台的待处理好友请求信息."""
            requests = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(query).resolve()
                for answer in response.as_concept_rows():
                    uid = answer.get("uid").as_attribute().get_value().as_string()
                    requests.append({
                        "user_id": uid.replace(f"{platform_id}_", ""),
                        "nickname": answer.get("nick").as_attribute().get_value().as_string(),
                        "flag": answer.get("f").as_attribute().get_value().as_string(),
                        "comment": answer.get("c").as_attribute().get_value().as_string(),
                    })
            return requests

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"查询平台 '{platform_id}' 的待处理好友请求失败: {e}", exc_info=True)
            return []

    async def get_conversation_last_read_timestamp(self, conversation_entity_uid: str) -> float:
        """获取一个会话的最后已读时间戳（基于 read-status 关系）."""
        query = f"""
        match
            $p isa person, has person-uid "{SELF_PROFILE_ID}";
            $c isa conversation, has conversation-uid "{conversation_entity_uid}";
            (reader: $p, readable: $c) isa read-status, has timestamp $ts;
        select $ts;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read():
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(query).resolve()
                answers = list(response.as_concept_rows())
                if answers:
                    # TypeDB datetime is timezone-aware, convert to POSIX timestamp
                    dt_value = answers[0].get("ts").as_attribute().get_value().as_datetime()
                    return dt_value.timestamp() * 1000.0
                return 0.0

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取会话 '{conversation_entity_uid}' 最后已读时间戳失败: {e}")
            return 0.0

    async def update_conversation_last_read_timestamp(
        self, conversation_entity_uid: str, timestamp: float
    ) -> bool:
        """更新一个会话的最后已读时间戳（UPSERT read-status 关系）."""
        # TypeDB datetime requires ISO 8601 format

        ts_iso = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).isoformat(timespec='milliseconds')

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_upsert_read_status() -> None:
            """更新会话的最后已读时间戳."""
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # Step 1: Delete old status
                delete_query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $rs (reader: $p, readable: $c) isa read-status;
                delete $rs isa read-status;
                """
                tx.query(delete_query).resolve()

                # Step 2: Insert new status
                insert_query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conversation_entity_uid}";
                insert
                    (reader: $p, readable: $c) isa read-status, has timestamp {ts_iso};
                """
                tx.query(insert_query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_upsert_read_status)
            logger.info(f"已更新会话实体 '{conversation_entity_uid}' 的最后已读时间戳。")
            return True
        except Exception as e:
            logger.error(f"更新会话实体 '{conversation_entity_uid}' 的时间戳失败: {e}", exc_info=True)
            return False

    async def get_or_create_conversation_entity(
        self,
        conversation_id: str,
        platform: str,
        conv_type: str,
        name: str | None = None,
        extra: dict | None = None,
    ) -> EntityDocument | None:
        """获取或创建一个会话实体，并确保其与平台实体关联。"""
        conv_entity_uid = build_conversation_entity_uid(platform, conv_type, conversation_id)

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_op():
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # Check if conversation exists
                find_query = f'match $c isa conversation, has conversation-uid "{conv_entity_uid}"; get $c;'
                result = tx.query(find_query).resolve()
                if list(result.as_concept_rows()):
                    # TODO: Update name if changed
                    return conv_entity_uid

                # Conversation does not exist, create it and the residency relation
                insert_query = f"""
                match $p isa platform, has platform-uid "{platform}";
                insert $c isa conversation,
                    has conversation-uid "{conv_entity_uid}",
                    has type "{conv_type}",
                    has display-name "{(name or conversation_id).replace('"', '\\"')}";
                (resident: $c, host-platform: $p) isa residency;
                """
                tx.query(insert_query).resolve()
                tx.commit()
                return conv_entity_uid

        try:
            uid = await asyncio.to_thread(db_op)
            if uid:
                # Re-fetch the document to return a consistent object
                return await self.get_entity_by_key(uid)
            return None
        except Exception as e:
            logger.error(f"获取或创建会話实体 '{conv_entity_uid}' 失败: {e}", exc_info=True)
            return None

    # ... 其他方法可以根据需要继续添加 ...
    # 比如 get_entity_by_key, finalize_friend_request 等
