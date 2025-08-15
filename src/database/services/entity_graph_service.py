# src/database/services/entity_graph_service.py
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid
from typedb.driver import Transaction, TransactionType

from ..core.connection_manager import TypeDBConnectionManager

logger = get_logger(__name__)

SELF_PROFILE_ID = "aic_person_0"


class EntityGraphService:
    """(TypeDB版) 负责管理实体与实体侧写之间的关系图谱."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("EntityGraphService (TypeDB) 初始化完成。")

    # --- 核心实体创建与关联 ---

    async def _update_account_nickname_if_changed(
        self, tx: Transaction, account_uid: str, new_nickname: str
    ) -> None:
        """事务内辅助函数：如果昵称变化，则更新."""
        # 1. 查找旧昵称
        match_query = (
            f'match $a isa account, has account-uid "{account_uid}", has nickname $n; get $n;'
        )
        answers = list(tx.query.get(match_query).resolve())

        if answers and (old_nick_concept := answers[0].get("n")):
            old_nick = old_nick_concept.as_attribute().get_value().get_string()
            if old_nick == new_nickname:
                return  # 昵称未变，无需操作

            # 2. 删除旧昵称
            delete_query = (
                f'match $a isa account, has account-uid "{account_uid}", '
                f'has nickname "{old_nick}"; delete $a has nickname "{old_nick}";'
            )
            tx.query.delete(delete_query).resolve()

        # 3. 插入新昵称
        insert_query = (
            f'match $a isa account, has account-uid "{account_uid}"; '
            f'insert $a has nickname "{new_nickname}";'
        )
        tx.query.insert(insert_query).resolve()
        logger.debug(f"已更新账户 '{account_uid}' 的昵称为 '{new_nickname}'。")

    async def find_or_create_profile_and_account_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """原子性地查找或创建“账户”实体及其关联的“个人”档案."""
        if not user_info or not user_info.user_id:
            return None, None

        account_uid = f"{platform}_{user_info.user_id}"

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> tuple[str | None, str | None]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                find_query = f"""
                match
                    $acc isa account, has account-uid "{account_uid}";
                    (owner: $p, owned-account: $acc) isa identity-ownership;
                    $p has person-uid $p_uid;
                get $p_uid, $acc;
                """
                answers = list(tx.query.get(find_query).resolve())
                if answers:
                    p_uid_attr = answers[0].get("p_uid")
                    p_uid_value = p_uid_attr.as_attribute().get_value() if p_uid_attr else None
                    p_uid = p_uid_value.get_string() if p_uid_value else None

                    # 更新昵称
                    asyncio.run(
                        self._update_account_nickname_if_changed(
                            tx, account_uid, user_info.user_nickname
                        )
                    )
                    return p_uid, account_uid
                return None, None

        profile_id, entity_uid = await asyncio.to_thread(db_read)

        if entity_uid:
            return profile_id, entity_uid
        else:
            return await self.create_new_profile_with_account_entity(user_info, platform)

    async def create_new_profile_with_account_entity(
        self,
        user_info: ProtocolUserInfo,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """原子性地创建新的 Profile、Account 实体及它们之间的关系."""
        profile_uid = SELF_PROFILE_ID if is_self else f"profile_{uuid.uuid4().hex[:12]}"
        account_uid = f"{platform}_{user_info.user_id}"
        nickname = (user_info.user_nickname or "").replace('"', '\\"')
        platform_id = (user_info.user_id or "").replace('"', '\\"')

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                if is_self:
                    find_person_query = (
                        f'match $p isa aic_self, has person-uid "{profile_uid}"; get $p;'
                    )
                    if not list(tx.query.get(find_person_query).resolve()):
                        insert_person_query = (
                            f'insert $p isa aic_self, has person-uid "{profile_uid}";'
                        )
                        tx.query.insert(insert_person_query).resolve()
                else:
                    insert_person_query = (
                        f'insert $p isa external_person, has person-uid "{profile_uid}";'
                    )
                    tx.query.insert(insert_person_query).resolve()

                insert_account_relation_query = f"""
                match $p isa person, has person-uid "{profile_uid}";
                insert
                $acc isa account,
                    has account-uid "{account_uid}",
                    has platform "{platform}",
                    has platform-id "{platform_id}",
                    has nickname "{nickname}",
                    has last-known-nickname "{nickname}";
                (owner: $p, owned-account: $acc) isa identity-ownership;
                """
                tx.query.insert(insert_account_relation_query).resolve()
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
        """更新一个账户在某个会话中的存在关系（membership）."""
        cardname = (user_info.user_cardname or "").replace('"', '\\"')
        perm_level = (user_info.permission_level or "member").replace('"', '\\"')
        timestamp = int(time.time() * 1000)

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_upsert_membership() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                delete_query = f"""
                match
                    $acc isa account, has account-uid "{account_entity_uid}";
                    $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $mem (member: $acc, group: $conv) isa membership;
                delete $mem isa membership;
                """
                tx.query.delete(delete_query).resolve()

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
                tx.query.insert(insert_query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_upsert_membership)
            logger.debug(
                f"成功更新存在关系: Account '{account_entity_uid}' "
                f"in Conv '{conversation_entity_uid}'"
            )
            return True
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)
            return False

    async def get_or_create_platform_entity(
        self, platform_id: str, display_name: str | None = None
    ) -> dict[str, Any] | None:
        """原子性地获取或创建一个平台实体."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        display_name_safe = (display_name or platform_id).replace('"', '\\"')

        def db_op() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                match_query = f'match $p isa platform, has platform-uid "{platform_id}"; get $p;'
                if list(tx.query.get(match_query).resolve()):
                    logger.debug(f"平台实体 '{platform_id}' 已存在。")
                    return {
                        "entity_uid": platform_id,
                        "details": {
                            "platform": platform_id,
                            "display_name": display_name or platform_id,
                        },
                    }

                insert_query = f"""
                insert $p isa platform,
                    has platform-uid "{platform_id}",
                    has display-name "{display_name_safe}";
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                logger.info(f"平台实体 '{platform_id}' 创建成功。")
                return {
                    "entity_uid": platform_id,
                    "details": {
                        "platform": platform_id,
                        "display_name": display_name or platform_id,
                    },
                }

        try:
            return await asyncio.to_thread(db_op)
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
            $acc has platform $platform;
            $acc has platform-id $pid;
            $acc has nickname $nick;
        get $uid, $platform, $pid, $nick;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            entities = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                for answer in tx.query.get(query).resolve():
                    uid_val = answer.get("uid").as_attribute().get_value().get_string()
                    platform_val = answer.get("platform").as_attribute().get_value().get_string()
                    pid_val = answer.get("pid").as_attribute().get_value().get_string()
                    nick_val = answer.get("nick").as_attribute().get_value().get_string()

                    entities.append(
                        {
                            "entity_uid": uid_val,
                            "details": {
                                "platform": platform_val,
                                "platform_id": pid_val,
                                "nickname": nick_val,
                            },
                        }
                    )
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
            $acc isa account, has platform "{platform_id}";
            $acc has flag $f;
            $acc has comment $c;
            $acc has last-known-nickname $nick;
            $acc has platform-id $pid;
            $acc has request-timestamp $ts;
        get $pid, $nick, $f, $c, $ts;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            requests = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                for answer in tx.query.get(query).resolve():
                    requests.append(
                        {
                            "user_id": answer.get("pid").as_attribute().get_value().get_string(),
                            "nickname": answer.get("nick").as_attribute().get_value().get_string(),
                            "flag": answer.get("f").as_attribute().get_value().get_string(),
                            "comment": answer.get("c").as_attribute().get_value().get_string(),
                            "timestamp": answer.get("ts").as_attribute().get_value().get_integer(),
                        }
                    )
            return requests

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"查询平台 '{platform_id}' 的待处理好友请求失败: {e}", exc_info=True)
            return []

    async def get_conversation_last_read_timestamp(self, conversation_entity_uid: str) -> float:
        """获取一个会话的最后已读时间戳."""
        query = f"""
        match
            $p isa person, has person-uid "{SELF_PROFILE_ID}";
            $c isa conversation, has conversation-uid "{conversation_entity_uid}";
            (reader: $p, readable: $c) isa read-status, has timestamp $ts;
        get $ts;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> float:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query.get(query).resolve())
                if answers:
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
        """更新一个会话的最后已读时间戳."""
        ts_iso = datetime.fromtimestamp(timestamp / 1000.0, tz=UTC).isoformat(
            timespec="milliseconds"
        )

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_upsert() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                delete_query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $rs (reader: $p, readable: $c) isa read-status;
                delete $rs isa read-status;
                """
                tx.query.delete(delete_query).resolve()

                insert_query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conversation_entity_uid}";
                insert
                    (reader: $p, readable: $c) isa read-status, has timestamp {ts_iso};
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_upsert)
            logger.info(f"已更新会话实体 '{conversation_entity_uid}' 的最后已读时间戳。")
            return True
        except Exception as e:
            logger.error(
                f"更新会话实体 '{conversation_entity_uid}' 的时间戳失败: {e}", exc_info=True
            )
            return False

    async def get_or_create_conversation_entity(
        self,
        conversation_id: str,
        platform: str,
        conv_type: str,
        name: str | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any] | None:
        """获取或创建一个会话实体，并确保其与平台实体关联."""
        conv_entity_uid = build_conversation_entity_uid(platform, conv_type, conversation_id)

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_op() -> str | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                find_query = (
                    f'match $c isa conversation, has conversation-uid "{conv_entity_uid}"; get $c;'
                )
                if list(tx.query.get(find_query).resolve()):
                    return conv_entity_uid

                insert_query = f"""
                match $p isa platform, has platform-uid "{platform}";
                insert $c isa conversation,
                    has conversation-uid "{conv_entity_uid}",
                    has type "{conv_type}",
                    has display-name "{(name or conversation_id).replace('"', '\\"')}",
                    has extra-json "{
                    json.dumps(extra or {}, ensure_ascii=False).replace('"', '\\"')
                }";
                (resident: $c, host-platform: $p) isa residency;
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return conv_entity_uid

        try:
            uid = await asyncio.to_thread(db_op)
            if uid:
                return await self.get_entity_by_key(uid)
            return None
        except Exception as e:
            logger.error(f"获取或创建会話实体 '{conv_entity_uid}' 失败: {e}", exc_info=True)
            return None

    async def get_entity_by_key(self, entity_uid: str) -> dict[str, Any] | None:
        """根据 entity_uid (_key) 获取单个实体文档."""
        if not entity_uid:
            return None
        return {"entity_uid": entity_uid}

    async def update_friend_request_status(
        self, entity_uid: str, flag: str, comment: str, timestamp: int
    ) -> bool:
        """更新指定实体的待处理好友请求信息."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                delete_query = f"""
                match $acc isa account, has account-uid "{entity_uid}";
                $acc has flag $f;
                $acc has comment $c;
                $acc has request-timestamp $ts;
                delete $acc has $f; $acc has $c; $acc has $ts;
                """
                tx.query.delete(delete_query).resolve()

                insert_query = f"""
                match $acc isa account, has account-uid "{entity_uid}";
                insert $acc has flag "{flag.replace('"', '\\"')}",
                            has comment "{comment.replace('"', '\\"')}",
                            has request-timestamp {timestamp};
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"已更新实体 '{entity_uid}' 的好友请求状态。")
            return success
        except Exception as e:
            logger.error(f"更新实体 '{entity_uid}' 好友请求状态时失败: {e}", exc_info=True)
            return False

    async def finalize_friend_request(
        self, entity_uid: str, approved: bool, remark: str | None
    ) -> bool:
        """完成好友请求处理，清除待处理状态并可选地更新好友备注."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                delete_query = f"""
                match $acc isa account, has account-uid "{entity_uid}";
                $acc has flag $f;
                $acc has comment $c;
                $acc has request-timestamp $ts;
                delete $acc has $f; $acc has $c; $acc has $ts;
                """
                tx.query.delete(delete_query).resolve()

                if approved and remark:
                    remark_safe = remark.replace('"', '\\"')
                    delete_remark_query = f"""
                    match $acc isa account, has account-uid "{entity_uid}";
                    $acc has friend-remark $rem;
                    delete $acc has $rem;
                    """
                    tx.query.delete(delete_remark_query).resolve()
                    insert_remark_query = f"""
                    match $acc isa account, has account-uid "{entity_uid}";
                    insert $acc has friend-remark "{remark_safe}";
                    """
                    tx.query.insert(insert_remark_query).resolve()

                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"已完成实体 '{entity_uid}' 的好友请求处理。")
            return success
        except Exception as e:
            logger.error(f"完成实体 '{entity_uid}' 好友请求处理时失败: {e}", exc_info=True)
            return False
