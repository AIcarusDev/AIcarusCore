import asyncio
import time
import uuid
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from loguru import logger
from typedb.driver import TransactionType

from ..connection_manager import TypeDBConnectionManager

SELF_PROFILE_ID = "aic_person_0"


class EntityGraphService:
    """(TypeDB版) 负责管理实体与实体侧写之间的关系图谱."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("EntityGraphService (TypeDB) 初始化完成。")

    # --- 已有的方法保持不变 ---
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
                answers = list(tx.query.get(query).resolve())
                for answer in answers:
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
                answers = list(tx.query.get(query).resolve())
                for answer in answers:
                    pid_val = answer.get("pid").as_attribute().get_value().get_string()
                    nick_val = answer.get("nick").as_attribute().get_value().get_string()
                    flag_val = answer.get("f").as_attribute().get_value().get_string()
                    comment_val = answer.get("c").as_attribute().get_value().get_string()
                    ts_val = answer.get("ts").as_attribute().get_value().get_integer()

                    requests.append(
                        {
                            "user_id": pid_val,
                            "nickname": nick_val,
                            "flag": flag_val,
                            "comment": comment_val,
                            "timestamp": ts_val,
                        }
                    )
            return requests

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"查询平台 '{platform_id}' 的待处理好友请求失败: {e}", exc_info=True)
            return []

    async def update_presence_in_conversation(
        self,
        account_entity_uid: str,
        conversation_entity_uid: str,
        user_info: ProtocolUserInfo,
    ) -> bool:
        """更新一个账户实体在某个会话实体中的存在关系（membership）."""
        cardname_safe = (user_info.user_cardname or "").replace('"', '\\"')
        perm_level_safe = (user_info.permission_level or "member").replace('"', '\\"')
        timestamp = int(time.time() * 1000)

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_upsert_membership() -> bool:
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
                        has cardname "{cardname_safe}",
                        has permission-level "{perm_level_safe}",
                        has timestamp {timestamp};
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_upsert_membership)
            if success:
                logger.debug(
                    f"成功更新存在关系: Account '{account_entity_uid}' "
                    f"in Conv '{conversation_entity_uid}'"
                )
            return success
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)
            return False

    async def find_or_create_profile_and_account_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """原子性地查找或创建“账户”实体及其关联的“个人”档案."""
        if not user_info or not user_info.user_id:
            logger.warning("提供的UserInfo不完整，无法查找或创建Profile/Account Entity。")
            return None, None

        account_uid = f"{platform}_{user_info.user_id}"

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read_op() -> tuple[str | None, str | None]:
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
                    logger.debug(f"实体 {account_uid} 已找到，关联的 Profile ID: {p_uid}")
                    return p_uid, account_uid
                return None, None

        profile_id, entity_uid = await asyncio.to_thread(db_read_op)

        if entity_uid:
            return profile_id, entity_uid
        else:
            logger.debug(f"未找到账户实体: {account_uid}，将创建新的 Profile 和 Entity。")
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
        nickname_safe = (user_info.user_nickname or "").replace('"', '\\"')
        platform_id_safe = (user_info.user_id or "").replace('"', '\\"')

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> tuple[str, str]:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                if is_self:
                    match_person_query = (
                        f'match $p isa aic_self, has person-uid "{profile_uid}"; get $p;'
                    )
                    answers = list(tx.query.get(match_person_query).resolve())
                    if not answers:
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
                    has platform-id "{platform_id_safe}",
                    has nickname "{nickname_safe}",
                    has last-known-nickname "{nickname_safe}";
                (owner: $p, owned-account: $acc) isa identity-ownership;
                """
                tx.query.insert(insert_account_relation_query).resolve()
                tx.commit()
                return profile_uid, account_uid

        try:
            profile_id, entity_uid = await asyncio.to_thread(db_write)
            logger.info(f"成功创建 Profile '{profile_id}' 并关联到 Account '{entity_uid}'。")
            return profile_id, entity_uid
        except Exception as e:
            logger.error(f"创建 Profile 和 Account Entity 的事务执行失败: {e}", exc_info=True)
            return None, None

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

                answers_iterator = tx.query.get(match_query)
                answers = list(answers_iterator.resolve())

                if answers:
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

    # --- 新增实现的方法 ---
    async def update_friend_request_status(
        self, entity_uid: str, flag: str, comment: str, timestamp: int
    ) -> bool:
        """更新指定实体的待处理好友请求信息."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 首先删除旧的请求属性，确保幂等性
                delete_query = f"""
                match $acc isa account, has account-uid "{entity_uid}";
                $acc has flag $f;
                $acc has comment $c;
                $acc has request-timestamp $ts;
                delete $acc has $f; $acc has $c; $acc has $ts;
                """
                tx.query.delete(delete_query).resolve()  # resolve()确保操作完成

                # 插入新的请求属性
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
                # 统一删除所有请求相关属性
                delete_query = f"""
                match $acc isa account, has account-uid "{entity_uid}";
                $acc has flag $f;
                $acc has comment $c;
                $acc has request-timestamp $ts;
                delete $acc has $f; $acc has $c; $acc has $ts;
                """
                tx.query.delete(delete_query).resolve()

                # 如果同意且有备注，则更新备注
                if approved and remark:
                    remark_safe = remark.replace('"', '\\"')
                    # 删除旧备注
                    delete_remark_query = f"""
                    match $acc isa account, has account-uid "{entity_uid}";
                    $acc has friend-remark $rem;
                    delete $acc has $rem;
                    """
                    tx.query.delete(delete_remark_query).resolve()
                    # 插入新备注
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
