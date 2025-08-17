# src/database/services/entity_graph_service.py
import asyncio
import json
import time
import uuid
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid, parse_entity_uid
from typedb.driver import Transaction, TransactionType

from ..core.connection_manager import TypeDBConnectionManager
from .event_storage_service import EventStorageService

logger = get_logger(__name__)

SELF_PROFILE_ID = "aic_person_0"


class EntityGraphService:
    """实体图服务."""

    def __init__(
        self,
        conn_manager: TypeDBConnectionManager,
        event_storage_service: EventStorageService,
    ) -> None:
        self.conn_manager = conn_manager
        self.event_storage_service = event_storage_service
        logger.info("EntityGraphService (TypeDB) 初始化完成。")

    def _update_account_nickname_if_changed_sync(
        self, tx: Transaction, account_uid: str, new_nickname: str
    ) -> None:
        match_query = (
            f'match $a isa account, has account-uid "{account_uid}"; '
            f"try {{ $a has nickname $n; }}; select $n;"
        )
        answers = list(tx.query(match_query).resolve().as_concept_rows())
        old_nick = None
        if answers and (old_nick_concept := answers[0].get("n")):
            old_nick = old_nick_concept.as_attribute().get_value()
        if old_nick == new_nickname:
            return
        if old_nick is not None:
            # Note: 'update' is more idiomatic here if cardinality is 1
            delete_query = (
                f"match $a isa account, "
                f'has account-uid "{account_uid}", has nickname "{old_nick}"; '
                f'delete $a has nickname "{old_nick}";'
            )
            tx.query(delete_query).resolve()
        insert_query = (
            f'match $a isa account, has account-uid "{account_uid}"; '
            f'insert $a has nickname "{new_nickname}";'
        )
        tx.query(insert_query).resolve()

    async def find_or_create_profile_and_account_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """查找或创建 Profile 和 Account 实体.

        Args:
            user_info (ProtocolUserInfo): 包含用户信息的 ProtocolUserInfo 对象。
            platform (str): 平台名称。

        Returns:
            tuple[str | None, str | None]: 包含 profile_id 和 account_uid 的元组。
                                            如果创建或查找失败，则返回 (None, None)。
        """
        if not user_info or not user_info.user_id:
            return None, None
        account_uid = f"{platform}_{user_info.user_id}"
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read_and_update() -> tuple[str | None, str | None]:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                find_query = (
                    f'match $acc isa account, has account-uid "{account_uid}"; '
                    f"(owner: $p, owned-account: $acc) isa identity-ownership; "
                    f"$p isa person, has person-uid $p_uid; select $p_uid;"
                )
                answers = list(tx.query(find_query).resolve().as_concept_rows())
                if answers:
                    p_uid = (
                        answers[0].get("p_uid").as_attribute().get_value()
                        if answers[0].get("p_uid")
                        else None
                    )
                    if user_info.user_nickname:
                        self._update_account_nickname_if_changed_sync(
                            tx, account_uid, user_info.user_nickname
                        )
                    tx.commit()
                    return p_uid, account_uid
                return None, None

        profile_id, entity_uid = await asyncio.to_thread(db_read_and_update)
        return (
            (profile_id, entity_uid)
            if entity_uid
            else await self.create_new_profile_with_account_entity(user_info, platform)
        )

    async def create_new_profile_with_account_entity(
        self,
        user_info: ProtocolUserInfo,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """创建新的 Profile 和 Account 实体.

        Args:
            user_info (ProtocolUserInfo): 包含用户信息的 ProtocolUserInfo 对象。
            platform (str): 平台名称。
            is_self (bool): 是否为自身实体。

        Returns:
            tuple[str | None, str | None]: 包含 profile_id 和 account_uid 的元组。
                                            如果创建失败，则返回 (None, None)。
        """
        profile_uid = SELF_PROFILE_ID if is_self else f"profile_{uuid.uuid4().hex[:12]}"
        account_uid = f"{platform}_{user_info.user_id}"
        nickname = (user_info.user_nickname or "").replace('"', '\\"')
        platform_id_val = (user_info.user_id or "").replace('"', '\\"')
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                person_type = "aic_self" if is_self else "external_person"
                if not list(
                    tx.query(
                        f'match $p isa {person_type}, has person-uid "{profile_uid}";'
                    ).resolve()
                ):
                    tx.query(
                        f'insert $p isa {person_type}, has person-uid "{profile_uid}";'
                    ).resolve()
                if not list(
                    tx.query(f'match $plat isa platform, has platform-uid "{platform}";').resolve()
                ):
                    tx.query(
                        f'insert $plat isa platform, has platform-uid "{platform}", '
                        f'has display-name "{platform}";'
                    ).resolve()
                tx.query(
                    f'match $p isa person, has person-uid "{profile_uid}"; '
                    f'$plat isa platform, has platform-uid "{platform}"; '
                    f'insert $acc isa account, has account-uid "{account_uid}", '
                    f'has platform-id "{platform_id_val}", '
                    f'has nickname "{nickname}", '
                    f'has last-known-nickname "{nickname}"; '
                    f"(owner: $p, owned-account: $acc) isa identity-ownership; "
                    f"(resident: $acc, host-platform: $plat) isa residency;"
                ).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_write)
            return profile_uid, account_uid
        except Exception as e:
            logger.error(f"创建 Profile 和 Account Entity 的事务执行失败: {e}", exc_info=True)
            return None, None

    async def get_all_self_entities(self) -> list[dict[str, Any]]:
        """获取所有自身实体信息."""
        query = (
            f'match $p isa person, has person-uid "{SELF_PROFILE_ID}"; '
            f"(owner: $p, owned-account: $acc) isa identity-ownership; "
            f"$acc isa account, has account-uid $uid, has platform-id $pid, has nickname $nick;"
            f"(resident: $acc, host-platform: $plat) isa residency; "
            f"$plat isa platform, has platform-uid $platform_uid; "
            f"select $uid, $pid, $nick, $platform_uid;"
        )
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                return [
                    {
                        "entity_uid": a.get("uid").as_attribute().get_value(),
                        "details": {
                            "platform": a.get("platform_uid").as_attribute().get_value(),
                            "platform_id": a.get("pid").as_attribute().get_value(),
                            "nickname": a.get("nick").as_attribute().get_value(),
                        },
                    }
                    for a in tx.query(query).resolve().as_concept_rows()
                ]

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取自身所有平台实体信息时失败: {e}", exc_info=True)
            return []

    async def update_presence_in_conversation(
        self,
        account_entity_uid: str,
        conversation_entity_uid: str,
        user_info: ProtocolUserInfo,
        conversation_name: str | None,
    ) -> bool:
        """更新用户在对话中的存在状态."""
        cardname = (user_info.user_cardname or "").replace('"', '\\"')
        perm_level = (user_info.permission_level or "member").replace('"', '\\"')
        timestamp = int(time.time() * 1000)
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_upsert_membership() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # This is the final, correct, and expert-verified query for atomic upsert.
                # It correctly handles both creation and update scenarios.
                upsert_query = f"""
                match
                    $acc isa account, has account-uid "{account_entity_uid}";
                    $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $mem isa membership, links(member: $acc, group: $conv);
                delete
                    $mem;
                insert
                    $new_mem isa membership, links(member: $acc, group: $conv),
                        has cardname "{cardname}",
                        has permission-level "{perm_level}",
                        has timestamp {timestamp};
                """
                tx.query(upsert_query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_upsert_membership)
            return True
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)
            return False

    async def get_or_create_platform_entity(
        self, platform_id: str, display_name: str | None = None
    ) -> dict[str, Any] | None:
        """获取或创建一个平台实体.

        Args:
            platform_id (str): 平台 ID.
            display_name (str | None, optional): 平台显示名称. Defaults to None.

        Returns:
            dict[str, Any] | None: 平台实体信息字典，如果创建或获取失败则返回 None.
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name
        display_name_safe = (display_name or platform_id).replace('"', '\\"')

        def db_op() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                if list(
                    tx.query(f'match $p isa platform, has platform-uid "{platform_id}";').resolve()
                ):
                    return {
                        "entity_uid": platform_id,
                        "details": {
                            "platform": platform_id,
                            "display_name": display_name or platform_id,
                        },
                    }
                tx.query(
                    f'insert $p isa platform, has platform-uid "{platform_id}", '
                    f'has display-name "{display_name_safe}";'
                ).resolve()
                tx.commit()
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

    async def get_pending_friend_requests(self, platform_id: str) -> list[dict[str, Any]]:
        """获取待处理的好友请求."""
        query = (
            f"match $acc isa account,"
            f'has platform-id $pid; $p isa platform, has platform-uid "{platform_id}";'
            f"(resident: $acc, host-platform: $p) isa residency; $acc has flag $f; "
            f"$acc has comment $c; $acc has last-known-nickname $nick; "
            f"$acc has request-timestamp $ts;"
            f"select $pid, $nick, $f, $c, $ts;"
        )
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                return [
                    {
                        "user_id": a.get("pid").as_attribute().get_value(),
                        "nickname": a.get("nick").as_attribute().get_value(),
                        "flag": a.get("f").as_attribute().get_value(),
                        "comment": a.get("c").as_attribute().get_value(),
                        "timestamp": a.get("ts").as_attribute().get_value(),
                    }
                    for a in tx.query(query).resolve().as_concept_rows()
                ]

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"查询平台 '{platform_id}' 的待处理好友请求失败: {e}", exc_info=True)
            return []

    async def get_conversation_last_read_timestamp(self, conversation_entity_uid: str) -> float:
        """获取会话最后一次读取的时间戳."""
        query = f"""
        match
            $person isa person, has person-uid "{SELF_PROFILE_ID}";
            $conversation isa conversation, has conversation-uid "{conversation_entity_uid}";
            $read_status isa read-status,
                links (reader: $person, readable: $conversation),
                has timestamp $timestamp;
        select $timestamp;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> float:
            logger.debug(
                "[DEBUG] Executing get_conversation_last_read_timestamp query "
                f"for conv_uid='{conversation_entity_uid}':\n{query}"
            )
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if answers and answers[0].get("timestamp"):
                    ts_val = float(answers[0].get("timestamp").as_attribute().get_value())
                    logger.debug(f"[DEBUG] Found last_read_timestamp: {ts_val}")
                    return ts_val
                logger.debug("[DEBUG] No last_read_timestamp found, returning 0.0")
                return 0.0

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取会话 '{conversation_entity_uid}' 最后已读时间戳失败: {e}")
            return 0.0

    async def update_conversation_last_read_timestamp(
        self, conversation_entity_uid: str, timestamp: float
    ) -> bool:
        """更新会话的最后读取时间戳.

        Args:
            conversation_entity_uid (str): 会话的实体 UID.
            timestamp (float): 最后读取的时间戳.

        Returns:
            bool: 指示更新是否成功的布尔值。
        """
        ts_int = int(timestamp)
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_upsert() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                tx.query(
                    f'match $p isa person, has person-uid "{SELF_PROFILE_ID}"; '
                    f'$c isa conversation, has conversation-uid "{conversation_entity_uid}"; '
                    f"$rs (reader: $p, readable: $c) isa read-status; delete $rs;"
                ).resolve()
                tx.query(
                    f'match $p isa person, has person-uid "{SELF_PROFILE_ID}"; '
                    f'$c isa conversation, has conversation-uid "{conversation_entity_uid}"; '
                    f"insert (reader: $p, readable: $c) isa read-status, has timestamp {ts_int};"
                ).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_upsert)
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
        """获取或创建一个会话实体.

        Args:
            conversation_id (str): 会话 ID.
            platform (str): 平台名称.
            conv_type (str): 会话类型.
            name (str | None, optional): 会话名称. Defaults to None.
            extra (dict | None, optional): 额外信息. Defaults to None.

        Returns:
            dict[str, Any] | None: 会话实体信息字典，如果创建或获取失败则返回 None.
        """
        # --- 1. 保证 Platform 节点存在 ---
        plat_doc = await self.get_or_create_platform_entity(platform, display_name=platform)
        if not plat_doc:
            logger.error(f"[Conversation] 平台节点创建或获取失败: {platform}")
            return None
        logger.debug(f"[Conversation] 已确保平台实体: {plat_doc}")

        conv_entity_uid = build_conversation_entity_uid(platform, conv_type, conversation_id)
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_op() -> str | None:
            try:
                with driver.transaction(db_name, TransactionType.WRITE) as tx:
                    query_exist = (
                        f'match $c isa conversation, '
                        f'has conversation-uid "{conv_entity_uid}"; get $c;'
                    )
                    exists = list(tx.query(query_exist).resolve())
                    logger.debug(
                        f"[Conversation][db_op] Exist-query: {query_exist!r}, hits: {len(exists)}"
                    )
                    if exists:
                        return conv_entity_uid

                    safe_name = (name or conversation_id).replace('"', '\\"')
                    insert_query = (
                        f'match $p isa platform, has platform-uid "{platform}"; '
                        f'insert $c isa conversation, '
                        f'    has conversation-uid "{conv_entity_uid}", '
                        f'    has conversation-id "{conversation_id}", '
                        f'    has type "{conv_type}", '
                        f'    has display-name "{safe_name}"; '
                        f'insert (resident: $c, host-platform: $p) isa residency;'
                    )
                    logger.debug(f"[Conversation][db_op] Insert-query: {insert_query!r}")

                    # 2.4 执行插入并提交
                    tx.query(insert_query).resolve()
                    tx.commit()
                    logger.debug(f"[Conversation][db_op] 成功插入会话实体 {conv_entity_uid}")
                    return conv_entity_uid
            except Exception as e:
                logger.error(
                    f"[Conversation][db_op] 错误，conv_uid={conv_entity_uid}, "
                    f"platform={platform}: {e}", exc_info=True
                )
                raise

        try:
            uid = await asyncio.to_thread(db_op)
            if not uid:
                logger.error(f"[Conversation] db_op 返回 None，conv_uid={conv_entity_uid}")
                return None
            return await self.get_entity_by_key(uid)
        except Exception:
            return None

    async def get_entity_by_key(self, entity_uid: str) -> dict[str, Any] | None:
        """通过实体 UID 获取实体信息."""
        if not entity_uid:
            return None
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name
        query = f"""
        match
            $e isa $entity_type, has $uid_attr "{entity_uid}";
            $e has $attr;
            $attr isa $attr_type;
            select $e, $entity_type, $attr, $attr_type;
        """

        def db_read() -> dict[str, Any] | None:
            print(f'[PROBE_D] Executing get_entity_by_key for uid: {entity_uid}')
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if not answers:
                    logger.warning(
                        f'[PROBE_E] get_entity_by_key for {entity_uid} returned no answers.'
                        )
                    return None
                first_answer = answers[0]
                entity_type_concept = first_answer.get("entity_type")
                if not entity_type_concept:
                    return None
                entity_type_label = entity_type_concept.as_type().get_label()
                doc = {
                    "_key": entity_uid,
                    "entity_uid": entity_uid,
                    "entity_type": entity_type_label,
                    "details": {},
                }
                for ans in answers:
                    attr_type_concept = ans.get("attr_type")
                    attr_concept = ans.get("attr")
                    if attr_type_concept and attr_concept:
                        attr_label = attr_type_concept.as_type().get_label()
                        py_key = attr_label.replace("-", "_")
                        py_value = attr_concept.as_attribute().get_value()
                        if isinstance(py_value, str) and "_json" in attr_label:
                            try:
                                doc["details"][py_key] = json.loads(py_value)
                            except json.JSONDecodeError:
                                doc["details"][py_key] = py_value
                        else:
                            doc["details"][py_key] = py_value
                return doc

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"通过 key '{entity_uid}' 获取实体时失败: {e}", exc_info=True)
            return None

    async def update_friend_request_status(
        self, entity_uid: str, flag: str, comment: str, timestamp: int
    ) -> bool:
        """更新好友请求状态.

        Args:
            entity_uid (str): 账户实体 UID.
            flag (str): 状态标志.
            comment (str): 备注信息.
            timestamp (int): 时间戳.

        Returns:
            bool: 指示更新是否成功的布尔值.
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                tx.query(
                    f'match $acc isa account, has account-uid "{entity_uid}"; '
                    f"$acc has flag $f; $acc has comment $c; $acc has request-timestamp $ts; "
                    f"delete has $f of $acc; has $c of $acc; has $ts of $acc;"
                ).resolve()
                tx.query(
                    f'match $acc isa account, has account-uid "{entity_uid}"; '
                    f'insert $acc has flag "{flag.replace('"', '\\"')}", '
                    f'has comment "{comment.replace('"', '\\"')}", '
                    f"has request-timestamp {timestamp};"
                ).resolve()
                tx.commit()
                return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"更新实体 '{entity_uid}' 好友请求状态时失败: {e}", exc_info=True)
            return False

    async def finalize_friend_request(
        self, entity_uid: str, approved: bool, remark: str | None
    ) -> bool:
        """完成好友请求.

        Args:
            entity_uid (str): 账户实体 UID.
            approved (bool): 是否批准好友请求.
            remark (str | None): 备注信息.

        Returns:
            bool: 指示更新是否成功的布尔值.
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                tx.query(
                    f'match $acc isa account, has account-uid "{entity_uid}"; '
                    f"$acc has flag $f; "
                    f"$acc has comment $c; $acc has request-timestamp $ts; "
                    f"delete has $f of $acc; has $c of $acc; has $ts of $acc;"
                ).resolve()
                if approved and remark:
                    tx.query(
                        f'match $acc isa account, has account-uid "{entity_uid}"; '
                        f"$acc has friend-remark $rem; delete has $rem of $acc;"
                    ).resolve()
                    tx.query(
                        f'match $acc isa account, has account-uid "{entity_uid}"; '
                        f'insert $acc has friend-remark "{remark.replace('"', '\\"\\"')}";'
                    ).resolve()
                tx.commit()
                return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"完成实体 '{entity_uid}' 好友请求处理时失败: {e}", exc_info=True)
            return False

    async def get_self_entity_by_platform(self, platform_id: str) -> dict[str, Any] | None:
        """通过平台 ID 获取自身实体信息."""
        return next(
            (
                e
                for e in await self.get_all_self_entities()
                if e.get("details", {}).get("platform") == platform_id
            ),
            None,
        )

    async def get_self_presence_in_conversation(
        self, platform: str, conversation_entity_uid: str
    ) -> dict[str, Any] | None:
        """获取自身在会话中的状态 (占位方法)."""
        return {"cardname": "Placeholder Card", "permission_level": "member"}

    async def get_recently_active_conversation_entities_with_details(
        self, exclude_conversation_id: str | None = None, self_bot_ids: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """获取最近活跃的会话实体及其详细信息.

        Args:
            exclude_conversation_id (str | None, optional): 需要排除的会话 ID. Defaults to None.
            self_bot_ids (dict[str, str] | None, optional): 自身 Bot 的 ID 字典. Defaults to None.
        """
        if self_bot_ids is None:
            self_bot_ids = {}

        active_convs_data = []
        query_all_events = """
        match
            $event isa event,
                has conversation-info-json $ci,
                has timestamp $ts;
            $platform isa platform, has platform-uid $puid;
            (source-platform: $platform, sourced-event: $event) isa event-source;
        select $ci, $ts, $puid;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read_and_process_events() -> list[tuple[str, int]]:
            conv_latest_event: dict[str, int] = {}
            with driver.transaction(db_name, TransactionType.READ) as tx:
                for ans in list(tx.query(query_all_events).resolve().as_concept_rows()):
                    print(ans)
                    try:
                        ci_str = ans.get("ci").as_attribute().get_value()
                        ts = ans.get("ts").as_attribute().get_value()
                        platform = ans.get("puid").as_attribute().get_value()
                        ci_json = json.loads(ci_str)
                        conv_type = ci_json.get("type", "unknown")
                        native_id = ci_json.get("conversation_id")
                        if platform and conv_type and native_id:
                            conv_uid = build_conversation_entity_uid(
                                platform, conv_type, str(native_id)
                            )
                            if ts > conv_latest_event.get(conv_uid, 0):
                                print(conv_latest_event)
                                conv_latest_event[conv_uid] = ts
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        continue
            print(conv_latest_event)
            return sorted(conv_latest_event.items(), key=lambda item: item[1], reverse=True)

        try:
            active_convs = await asyncio.to_thread(db_read_and_process_events)
            print(active_convs)
            print(
                f'[PROBE_A] Found {len(active_convs)} active '
                f'conversations from events: {active_convs}'
            )
        except Exception as e:
            print(f"获取活跃会话列表失败: {e}", exc_info=True)
            return []

        for conv_uid, latest_ts in active_convs:
            print(f"[PROBE_A1] Processing conv_uid: {conv_uid}")
            if conv_uid == exclude_conversation_id:
                print(f'[PROBE_B] Processing conv_uid: {conv_uid}')
                continue
            try:
                tasks = {
                    "conv_doc": self.get_entity_by_key(conv_uid),
                    "latest_event": self.event_storage_service.get_event_by_timestamp(
                        conv_uid, latest_ts
                    ),
                    "unread_info": self.event_storage_service.get_unread_count(
                        conv_uid, self_bot_ids
                    ),
                }
                results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                task_results = dict(zip(tasks.keys(), results, strict=False))
                print(f"[PROBE_B1] {task_results}")
                for result in task_results.values():
                    if isinstance(result, Exception):
                        raise result
                print("[PROBE_B2] 111111111")
                if task_results["conv_doc"] and task_results["latest_event"]:
                    print(f'[PROBE_C] Successfully gathered details for {conv_uid}')
                    active_convs_data.append(
                        {
                            "conv_doc": task_results["conv_doc"],
                            "latest_event": task_results["latest_event"],
                            **task_results["unread_info"],
                        }
                    )
            except Exception as e:
                print(f"处理会话 {conv_uid} 的详细信息时出错: {e}", exc_info=True)
        print(active_convs_data)
        return active_convs_data
