# 文件路径: src/services/database/services/entity_graph_service.py

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
from ..models import EntityDocument
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

    # 从 UnreadInfoService 迁移并强化的通用方法
    async def get_sender_display_name_for_event(
        self, event: dict, conv_doc: "EntityDocument", self_bot_ids: dict
    ) -> str:
        """根据复杂的业务规则，获取事件发送者在特定上下文中的最佳显示名称."""
        event = event or {}
        user_info = (
            (event.get("user_info") or {}) if isinstance(event.get("user_info"), dict) else {}
        )

        details = getattr(conv_doc, "details", None)
        conv_type = getattr(details, "type", None) or (event.get("conversation_info") or {}).get(
            "type"
        )
        platform = getattr(details, "platform", None)

        current_sender_id = user_info.get("user_id") or user_info.get("id") or ""
        is_self_sender = bool(
            platform and current_sender_id and self_bot_ids.get(platform) == str(current_sender_id)
        )

        # 将“是自己”的逻辑完全独立出来，确保它有正确的备用链。
        if is_self_sender:
            # 如果是自己，优先尝试获取在当前会话的身份信息（群名片）
            if platform and conv_doc and conv_doc._key:
                presence_info = await self.get_self_presence_in_conversation(
                    platform=platform,
                    conversation_entity_uid=conv_doc._key,
                )
                if (
                    conv_type == "group"
                    and presence_info
                    and (group_cardname := presence_info.get("cardname"))
                ):
                    return group_cardname

            # 如果没有特定会话身份，或不是群聊，则获取在该平台的通用昵称
            if platform:
                self_entity = await self.get_self_entity_by_platform(platform)
                if self_entity and (
                    platform_nickname := self_entity.get("details", {}).get("nickname")
                ):
                    return platform_nickname

            # 如果所有方法都失败了，则引发异常
            raise ValueError(f"无法确定机器人自身在平台 '{platform}' 上的显示名称。")

        # 如果不是自己，则按原有逻辑处理
        friend_remark = (
            (user_info.get("extra") or {}).get("friend_remark")
            if isinstance(user_info.get("extra"), dict)
            else None
        )
        cardname = user_info.get("user_cardname")
        nickname = user_info.get("user_nickname")

        if isinstance(friend_remark, str) and friend_remark.strip():
            return friend_remark
        if conv_type == "group" and isinstance(cardname, str) and cardname.strip():
            return cardname
        if isinstance(nickname, str) and nickname.strip():
            return nickname

        # 如果所有方法都失败了，则引发异常
        user_id = user_info.get("user_id")
        raise ValueError(f"无法确定用户 '{user_id}' 的显示名称。")

    def _update_account_nickname_if_changed_sync(
        self, tx: Transaction, account_uid: str, new_nickname: str
    ) -> None:
        """如果提供的昵称与数据库中的不同，则更新它."""
        match_query = (
            f'match $a isa account, has account-uid "{account_uid}"; '
            f"{{ $a has nickname $n; }}; select $n;"
        )
        answers = list(tx.query(match_query).resolve().as_concept_rows())
        old_nick = None
        if answers and (old_nick_concept := answers[0].get("n")):
            old_nick = old_nick_concept.as_attribute().get_value()

        if old_nick == new_nickname:
            return

        if old_nick is not None:
            # 使用正确的 'delete has ... of ...' 语法
            delete_query = (
                f'match $a isa account, has account-uid "{account_uid}"; '
                f"$a has nickname $old_nick; "
                f"delete has $old_nick of $a;"
            )
            tx.query(delete_query).resolve()

        # 插入新值
        insert_query = (
            f'match $a isa account, has account-uid "{account_uid}"; '
            f'insert $a has nickname "{new_nickname.replace('"', '\\"\\"')}";'
        )
        tx.query(insert_query).resolve()

    async def establish_friendships(
        self, self_account_uid: str, friend_account_uids: list[str]
    ) -> bool:
        """批量建立双向好友关系."""
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                for friend_uid in friend_account_uids:
                    # 跳过与自己建立好友关系
                    if friend_uid == self_account_uid:
                        logger.debug(f"跳过与自身 ({self_account_uid}) 建立好友关系。")
                        continue

                    query = f"""
                    match
                        $me isa account, has account-uid "{self_account_uid}";
                        $friend isa account, has account-uid "{friend_uid}";
                    insert
                        (friend: $me, friend: $friend) isa friendship;
                    """
                    tx.query(query).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_write)
            return True
        except Exception as e:
            logger.error(f"批量建立好友关系时失败: {e}", exc_info=True)
            return False

    async def get_all_contacts(self, self_account_uid: str) -> tuple[list[dict], list[dict]]:
        """获取一个账户的所有好友和群组列表."""
        friends_task = self.get_all_friends_for_account(self_account_uid)
        groups_task = self.get_all_groups_for_account(self_account_uid)
        friends, groups = await asyncio.gather(friends_task, groups_task)
        return friends, groups

    async def get_all_friends_for_account(self, self_account_uid: str) -> list[dict]:
        """获取一个账户的所有好友列表."""
        query = f"""
        match
            $me isa account, has account-uid "{self_account_uid}";
            (friend: $me, friend: $friend) isa friendship;
        fetch {{
            "uid": $friend.account-uid,
            "name": $friend.nickname,
            "remark": $friend.friend-remark
        }};
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> list[dict]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                results = []
                # fetch 查询的结果需要用 as_concept_documents() 解析
                for doc in tx.query(query).resolve().as_concept_documents():
                    # as_concept_documents() 返回的 doc 是一个字典，其 value 是 Python 原生类型
                    # 我们不再需要调用 .as_attribute().get_value()
                    remark_val = doc.get("remark")
                    nick_val = doc.get("name")
                    uid_val = doc.get("uid")

                    if not uid_val:
                        continue  # 跳过无效数据

                    # 优先使用备注作为显示名称
                    display_name = remark_val if remark_val else nick_val

                    # 构建与 get_all_groups_for_account 兼容的返回格式
                    # uid 现在是 account-uid (例如 qq_123456)，渲染器需要 conversation-uid
                    # 所以我们在这里进行转换
                    try:
                        platform, user_id = uid_val.split("_", 1)
                        conv_uid = build_conversation_entity_uid(platform, "private", user_id)
                    except ValueError:
                        logger.warning(f"无法解析好友的 account-uid: {uid_val}，跳过此联系人。")
                        continue

                    results.append(
                        {
                            "uid": conv_uid,
                            "name": display_name,
                            "type": "private",
                        }
                    )
                return results

        return await asyncio.to_thread(db_read)

    async def get_all_groups_for_account(self, self_account_uid: str) -> list[dict]:
        """获取一个账户所在的所有群组列表."""
        query = f"""
        match
            $me isa account, has account-uid "{self_account_uid}";
            (member: $me, group: $group) isa membership;
            $group has conversation-uid $uid, has display-name $name;
        select $uid, $name;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> list[dict]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                return [
                    {
                        "uid": ans.get("uid").as_attribute().get_value(),
                        "name": ans.get("name").as_attribute().get_value(),
                        "type": "group",
                    }
                    for ans in tx.query(query).resolve().as_concept_rows()
                ]

        return await asyncio.to_thread(db_read)

    def _update_conversation_name_if_changed_sync(
        self, tx: Transaction, conv_entity_uid: str, new_name: str | None
    ) -> None:
        """如果提供的名称与数据库中的不同，则更新它."""
        match_query = (
            f'match $c isa conversation, has conversation-uid "{conv_entity_uid}"; '
            f"{{ $c has display-name $n; }}; select $n;"
        )
        answers = list(tx.query(match_query).resolve().as_concept_rows())
        old_name = None
        if answers and (old_name_concept := answers[0].get("n")):
            old_name = old_name_concept.as_attribute().get_value()

        if old_name != new_name:
            logger.info(f"会话 '{conv_entity_uid}' 名称已从 '{old_name}' 更新为 '{new_name}'。")
            if old_name is not None:
                # 使用正确的 'delete has ... of ...' 语法
                delete_query = (
                    f'match $c isa conversation, has conversation-uid "{conv_entity_uid}"; '
                    f"$c has display-name $old_name; "
                    f"delete has $old_name of $c;"
                )
                tx.query(delete_query).resolve()

            if new_name and new_name.strip():
                insert_query = (
                    f'match $c isa conversation, has conversation-uid "{conv_entity_uid}"; '
                    f'insert $c has display-name "{new_name.replace('"', '\\"\\"')}";'
                )
                tx.query(insert_query).resolve()

    async def find_or_create_profile_and_account_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """查找或创建 Profile 和 Account 实体，复用同步核心逻辑."""
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_op() -> tuple[str | None, str | None]:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                result = self._find_or_create_profile_and_account_entity_sync(
                    tx, user_info, platform
                )
                tx.commit()
                return result

        try:
            return await asyncio.to_thread(db_op)
        except Exception as e:
            logger.error(f"查找或创建实体时（异步封装）失败: {e}", exc_info=True)
            return None, None

    # find_or_create_profile_and_account_entity 的同步版本
    def _find_or_create_profile_and_account_entity_sync(
        self, tx: Transaction, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """在一个已存在的事务中，查找或创建 Profile 和 Account 实体."""
        if not user_info or not user_info.user_id:
            return None, None

        account_uid = f"{platform}_{user_info.user_id}"

        # 查找现有实体
        find_query = (
            f'match $acc isa account, has account-uid "{account_uid}"; '
            f"(owner: $p, owned-account: $acc) isa identity-ownership; "
            f"$p isa person, has person-uid $p_uid; "
            f"select $p_uid;"
        )
        answers = list(tx.query(find_query).resolve().as_concept_rows())

        if answers:
            p_uid = answers[0].get("p_uid").as_attribute().get_value()
            if user_info.user_nickname:
                self._update_account_nickname_if_changed_sync(
                    tx, account_uid, user_info.user_nickname
                )
            return p_uid, account_uid

        # 如果找不到，则创建
        return self._create_new_profile_with_account_entity_sync(tx, user_info, platform)

    async def create_new_profile_with_account_entity(
        self,
        user_info: ProtocolUserInfo | dict,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """创建新的 Profile 和 Account 实体，复用同步核心逻辑."""
        # 兼容字典输入
        if isinstance(user_info, dict):
            user_info = ProtocolUserInfo.from_dict(user_info)

        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_op() -> tuple[str | None, str | None]:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                result = self._create_new_profile_with_account_entity_sync(
                    tx, user_info, platform, is_self
                )
                tx.commit()
                return result

        try:
            return await asyncio.to_thread(db_op)
        except Exception as e:
            logger.error(f"创建新实体时（异步封装）失败: {e}", exc_info=True)
            return None, None

    # create_new_profile_with_account_entity 的同步版本
    def _create_new_profile_with_account_entity_sync(
        self, tx: Transaction, user_info: ProtocolUserInfo, platform: str, is_self: bool = False
    ) -> tuple[str | None, str | None]:
        """在一个已存在的事务中，创建新的 Profile 和 Account 实体."""
        user_id = user_info.user_id
        nickname = user_info.user_nickname

        if not user_id:
            logger.error("传入的 user_info 中缺少 user_id，无法创建实体。")
            return None, None

        profile_uid = SELF_PROFILE_ID if is_self else f"profile_{uuid.uuid4().hex[:12]}"
        account_uid = f"{platform}_{user_id}"

        # 使用已经提取出来的值
        nickname_safe = str(nickname or "").replace('"', '\\"')
        platform_id_val = str(user_id or "").replace('"', '\\"')

        person_type = "aic_self" if is_self else "external_person"

        # 1. 检查并创建 person 实体
        person_exists_query = f'match $p isa person, has person-uid "{profile_uid}"; select $p;'
        if not list(tx.query(person_exists_query).resolve()):
            tx.query(f'insert $p isa {person_type}, has person-uid "{profile_uid}";').resolve()

        # 2. 检查并创建 platform 实体
        platform_exists_query = (
            f'match $plat isa platform, has platform-uid "{platform}"; select $plat;'
        )
        if not list(tx.query(platform_exists_query).resolve()):
            tx.query(
                f"insert $plat isa platform,"
                f'has platform-uid "{platform}", '
                f'has display-name "{platform}";'
            ).resolve()

        # 3. 插入 account 并建立关系
        insert_account_query = f"""
        match
            $p isa person, has person-uid "{profile_uid}";
            $plat isa platform, has platform-uid "{platform}";
        insert
            $acc isa account, has account-uid "{account_uid}",
                has platform-id "{platform_id_val}",
                has nickname "{nickname_safe}",
                has last-known-nickname "{nickname_safe}";
            (owner: $p, owned-account: $acc) isa identity-ownership;
            (resident: $acc, host-platform: $plat) isa residency;
        """
        tx.query(insert_account_query).resolve()

        return profile_uid, account_uid

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
                # 第一步: 执行 - 获取 promise
                promise = tx.query(query)

                # 第二步: 解析 - 获取结果
                answers = promise.resolve()

                # 第三步: 处理 - 迭代结果
                return [
                    {
                        "entity_uid": a.get("uid").as_attribute().get_value(),
                        "details": {
                            "platform": a.get("platform_uid").as_attribute().get_value(),
                            "platform_id": a.get("pid").as_attribute().get_value(),
                            "nickname": a.get("nick").as_attribute().get_value(),
                        },
                    }
                    for a in answers.as_concept_rows()
                ]

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取自身所有平台实体信息时失败: {e}", exc_info=True)
            return []

    # 移除了 conversation_name 参数
    async def update_presence_in_conversation(
        self,
        account_entity_uid: str,
        conversation_entity_uid: str,
        user_info: ProtocolUserInfo,
    ) -> bool:
        """更新用户在对话中的存在状态，复用同步核心逻辑."""
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_op() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                self._update_presence_in_conversation_sync(
                    tx, account_entity_uid, conversation_entity_uid, user_info
                )
                tx.commit()

        try:
            await asyncio.to_thread(db_op)
            return True
        except Exception as e:
            logger.error(f"更新存在关系时（异步封装）失败: {e}", exc_info=True)
            return False

    # update_presence_in_conversation 的同步版本
    def _update_presence_in_conversation_sync(
        self, tx: Transaction, account_uid: str, conv_uid: str, user_info: ProtocolUserInfo
    ) -> None:
        """在一个已存在的事务中，更新用户在会话中的存在状态."""
        # 先删除旧关系
        self.delete_presence_in_conversation_sync(tx, account_uid, conv_uid)
        # 再插入新关系
        self.create_presence_in_conversation_sync(tx, account_uid, conv_uid, user_info)

    def delete_presence_in_conversation_sync(
        self, tx: Transaction, account_uid: str, conv_uid: str
    ) -> None:
        """在一个已存在的事务中，删除用户在会话中的存在状态."""
        if not account_uid or not conv_uid:
            logger.warning(
                f"尝试删除存在关系时，提供的 account_uid 或 conv_uid 为空: "
                f"account_uid='{account_uid}', conv_uid='{conv_uid}'"
            )
            return
        if account_uid and conv_uid:
            # logger.info(f"开始删除用户 '{account_uid}' 在会话 '{conv_uid}' 中的存在关系。")
            delete_query = f"""
            match
                $账号 isa account, has account-uid "{account_uid}";
                $会话 isa conversation, has conversation-uid "{conv_uid}";
                $存在关系 isa membership, links(member: $账号, group: $会话);
            delete
                $存在关系;
            """
        tx.query(delete_query).resolve()

    def create_presence_in_conversation_sync(
        self, tx: Transaction, account_uid: str, conv_uid: str, user_info: ProtocolUserInfo
    ) -> None:
        """在一个已存在的事务中，创建用户在会话中的存在状态."""
        if not account_uid or not conv_uid:
            logger.warning(
                f"尝试创建存在关系时，提供的 account_uid 或 conv_uid 为空: "
                f"account_uid='{account_uid}', conv_uid='{conv_uid}'"
            )
            return
        cardname = str(user_info.user_cardname or "").replace('"', '\\"')
        perm_level = str(user_info.permission_level or "member").replace('"', '\\"')
        timestamp = int(time.time() * 1000)
        # logger.info(
        #     f"开始创建用户 '{account_uid}' 在会话 '{conv_uid}' 中的存在关系，"
        #     f"cardname='{cardname}', permission_level='{perm_level}', timestamp={timestamp}"
        # )
        insert_query = f"""
        match
            $账号 isa account, has account-uid "{account_uid}";
            $会话 isa conversation, has conversation-uid "{conv_uid}";
        insert
            membership (member: $账号, group: $会话),
                has cardname "{cardname}",
                has permission-level "{perm_level}",
                has timestamp {timestamp};
        """
        tx.query(insert_query).resolve()

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
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if answers and answers[0].get("timestamp"):
                    return float(answers[0].get("timestamp").as_attribute().get_value())
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
                # 修正了关系匹配的语法，将 `isa read-status` 提前
                delete_query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $rs_old isa read-status(reader: $p, readable: $c);
                delete
                    $rs_old;
                """
                tx.query(delete_query).resolve()

                # 插入查询本身是正确的，但为了清晰，也使用正确的 shorthand
                insert_query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conversation_entity_uid}";
                insert
                    $new_rs isa read-status(reader: $p, readable: $c), has timestamp {ts_int};
                """
                tx.query(insert_query).resolve()
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
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 检查会话实体是否存在
                exists_query = (
                    f'match $c isa conversation, has conversation-uid "{conv_entity_uid}";'
                )
                is_existing = list(tx.query(exists_query).resolve())

                if is_existing:
                    # 无论 name 是否为 None，都调用更新逻辑
                    self._update_conversation_name_if_changed_sync(tx, conv_entity_uid, name)
                    tx.commit()
                    return conv_entity_uid

                # 如果实体不存在，则创建它
                insert_query_parts = [
                    f'match $p isa platform, has platform-uid "{platform}";',
                    "insert $c isa conversation,",
                    f'    has conversation-uid "{conv_entity_uid}",',
                    f'    has conversation-id "{conversation_id}",',
                    f'    has type "{conv_type}";',
                    "residency (resident: $c, host-platform: $p);",
                ]

                # 只有当 name 存在时，才添加 has display-name
                if name and name.strip():
                    safe_name = name.replace('"', '\\"')
                    insert_query_parts[2] = (
                        insert_query_parts[2] + f'\n    has display-name "{safe_name}",'
                    )

                full_query = "\n".join(insert_query_parts)
                tx.query(full_query).resolve()
                tx.commit()
                return conv_entity_uid

        try:
            uid = await asyncio.to_thread(db_op)
            return await self.get_entity_by_key(uid) if uid else None
        except Exception as e:
            logger.error(f"获取或创建会话实体 '{conv_entity_uid}' 失败: {e}", exc_info=True)
            return None

    async def get_conversations_by_platform(self, platform_uid: str) -> dict:
        """Get all conversation UIDs for a specific platform.

        Args:
            platform_uid: The platform UID.

        Returns:
            dict: Dictionary with conversation UIDs as keys and display names as values
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name
        conversations = {}

        def db_read() -> None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                query = f"""
                match
                    $c isa conversation, has conversation-uid $uid, has display-name $name;
                    $p isa platform, has platform-uid "{platform_uid}";
                    (resident: $c, host-platform: $p) isa residency;
                select $uid, $name;
                """
                results = list(tx.query(query).resolve().as_concept_rows())
                for result in results:
                    conv_uid = result.get("uid").as_attribute().get_value()
                    display_name = result.get("name").as_attribute().get_value()
                    conversations[conv_uid] = display_name

        await asyncio.to_thread(db_read)
        return conversations

    async def get_entity_by_key(self, entity_uid: str) -> EntityDocument | None:
        """通过实体 UID 获取实体信息."""
        if not entity_uid:
            return None

        # 1. 智能判断 UID 类型并构建查询
        parts = entity_uid.split("_")
        if len(parts) >= 3:  # 认为是会话: platform_type_id...
            entity_type_label = "conversation"
            uid_attribute_label = "conversation-uid"
        elif len(parts) == 2:  # 认为是账户: platform_id
            entity_type_label = "account"
            uid_attribute_label = "account-uid"
        else:
            logger.error(f"无法识别的实体UID格式: '{entity_uid}'")
            return None

        query = f"""
        match
            $e isa {entity_type_label}, has {uid_attribute_label} "{entity_uid}";
            (resident: $e, host-platform: $p) isa residency;
            $p isa platform, has platform-uid $platform_uid;
            $e has $attr;
            $attr isa $attr_type;
        select $e, $platform_uid, $attr, $attr_type;
        """

        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> EntityDocument | None:
            logger.debug(f"[PROBE_D] Executing get_entity_by_key for uid: {entity_uid}")
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if not answers:
                    logger.warning(
                        f"[PROBE_E] get_entity_by_key for {entity_uid} returned no answers."
                    )
                    return None

                platform_from_relation = answers[0].get("platform_uid").as_attribute().get_value()
                attr_to_field_map = {"display-name": "name"}

                doc = {
                    "_key": entity_uid,
                    "entity_uid": entity_uid,
                    "entity_type": entity_type_label,
                    "details": {"platform": platform_from_relation},
                }

                for ans in answers:
                    attr_type_concept = ans.get("attr_type")
                    attr_concept = ans.get("attr")
                    if attr_type_concept and attr_concept:
                        attr_label = attr_type_concept.as_type().get_label()

                        # 优先使用映射，如果没有则使用默认规则
                        py_key = attr_to_field_map.get(attr_label, attr_label.replace("-", "_"))

                        py_value = attr_concept.as_attribute().get_value()
                        if isinstance(py_value, str) and "_json" in attr_label:
                            try:
                                doc["details"][py_key] = json.loads(py_value)
                            except json.JSONDecodeError:
                                doc["details"][py_key] = py_value
                        else:
                            doc["details"][py_key] = py_value

                top_level_keys = ["last_read_timestamp", "bot_profile_in_this_conversation"]
                for key in top_level_keys:
                    if key in doc["details"]:
                        doc[key] = doc["details"].pop(key)

                return EntityDocument.from_dict(doc)

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
        """获取自身在指定会话中的存在信息（如群名片、权限等）."""
        self_entity = await self.get_self_entity_by_platform(platform)
        if not self_entity or not self_entity.get("entity_uid"):
            logger.warning(f"在查询群内档案时，未能找到平台 '{platform}' 对应的自身实体UID。")
            return None
        self_account_uid = self_entity["entity_uid"]

        # [FIXED] The `fetch` query now correctly uses `select` to handle optional attributes
        # and avoids the FEX1 error by not fetching attributes that might not exist.
        query = f"""
        match
            $acc isa account, has account-uid "{self_account_uid}";
            $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
            $mem (member: $acc, group: $conv) isa membership;
        select $mem;
        """

        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # First, check if the membership relation exists at all.
                answers = list(tx.query(query).resolve().as_concept_rows())
                if not answers:
                    return None  # The bot is not a member of this conversation.

                presence_info = {}

                # Query for cardname if it exists
                card_query = f"""
                match
                    $acc isa account, has account-uid "{self_account_uid}";
                    $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $mem (member: $acc, group: $conv) isa membership, has cardname $c;
                select $c; limit 1;
                """
                card_answers = list(tx.query(card_query).resolve().as_concept_rows())
                if card_answers and (card_attr := card_answers[0].get("c")):
                    presence_info["cardname"] = card_attr.as_attribute().get_value()

                # Query for permission-level if it exists
                perm_query = f"""
                match
                    $acc isa account, has account-uid "{self_account_uid}";
                    $conv isa conversation, has conversation-uid "{conversation_entity_uid}";
                    $mem (member: $acc, group: $conv) isa membership, has permission-level $p;
                select $p; limit 1;
                """
                perm_answers = list(tx.query(perm_query).resolve().as_concept_rows())
                if perm_answers and (perm_attr := perm_answers[0].get("p")):
                    presence_info["permission_level"] = perm_attr.as_attribute().get_value()

                return presence_info

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(
                f"查询自身在会话 '{conversation_entity_uid}' 的存在信息时失败: {e!r}",
                exc_info=True,
            )
            return None

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
                                conv_latest_event[conv_uid] = ts
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        continue
            return sorted(conv_latest_event.items(), key=lambda item: item[1], reverse=True)

        try:
            active_convs = await asyncio.to_thread(db_read_and_process_events)
        except Exception as e:
            logger.error(f"获取活跃会话列表失败: {e}", exc_info=True)
            return []

        for conv_uid, latest_ts in active_convs:
            if conv_uid == exclude_conversation_id or conv_uid.startswith("qq_system"):
                continue
            try:
                # 使用 gather 并发执行所有异步调用
                results = await asyncio.gather(
                    self.get_entity_by_key(conv_uid),
                    self.event_storage_service.get_event_by_timestamp(conv_uid, latest_ts),
                    self.event_storage_service.get_unread_count(conv_uid, self_bot_ids),
                    return_exceptions=True,
                )

                # 检查是否有任何任务失败
                for result in results:
                    if isinstance(result, Exception):
                        raise result

                conv_doc, latest_event, unread_info = results

                if conv_doc and latest_event:
                    active_convs_data.append(
                        {
                            "conv_doc": conv_doc,
                            "latest_event": latest_event,
                            **unread_info,
                        }
                    )
            except Exception as e:
                logger.error(f"处理会话 {conv_uid} 的详细信息时出错: {e}", exc_info=True)

        return active_convs_data

    async def get_paged_conversations(
        self, platform_id: str, page: int, page_size: int, self_bot_ids: dict
    ) -> tuple[list[dict], int]:
        """获取指定平台的分页会话列表，按最新活动时间排序."""
        import math

        all_active_convs = await self.get_recently_active_conversation_entities_with_details(
            self_bot_ids=self_bot_ids
        )

        platform_convs = [
            c
            for c in all_active_convs
            if c.get("conv_doc") and c["conv_doc"].details.platform == platform_id
        ]

        total_items = len(platform_convs)
        if total_items == 0:
            return [], 1

        total_pages = math.ceil(total_items / page_size)
        start_index = (page - 1) * page_size
        end_index = start_index + page_size

        return platform_convs[start_index:end_index], total_pages

    async def batch_update_group_members_and_mark_synced(
        self, conversation_uid: str, member_list: list[dict]
    ) -> bool:
        """在一个原子事务中批量更新群成员信息，并标记该群为已同步."""
        if not member_list:
            # 如果列表为空，我们依然需要标记为已同步，表示我们已经检查过了
            logger.info(f"群聊 {conversation_uid} 成员列表为空，仅更新同步时间戳。")
            await self.mark_group_as_synced(conversation_uid)
            return True

        platform, _, _ = parse_entity_uid(conversation_uid)
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write_atomic_transaction() -> None:
            """这个纯同步函数包含了所有数据库操作，以保证原子性."""
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                logger.info(
                    f"启动原子事务：为群聊 {conversation_uid} 批量更新 {len(member_list)} 位成员..."
                )
                for member_data in member_list:
                    # Napcat 返回的成员列表里的每个对象，其结构和 sender 对象一致
                    user_info = ProtocolUserInfo.from_dict(member_data)
                    if not user_info.user_id:
                        continue

                    # 在同一个事务tx中，调用我们准备好的同步“积木块”
                    _, account_uid = self._find_or_create_profile_and_account_entity_sync(
                        tx, user_info, platform
                    )
                    if account_uid:
                        self._update_presence_in_conversation_sync(
                            tx, account_uid, conversation_uid, user_info
                        )

                # 在同一个事务中，更新同步时间戳
                current_ts = int(time.time() * 1000)
                # 先删除可能存在的旧属性
                tx.query(
                    f'match $c isa conversation, has conversation-uid "{conversation_uid}"; '
                    f"$c has last-full-sync-timestamp $old_ts; "
                    f"delete $old_ts of $c;"
                ).resolve()
                # 再插入新属性
                tx.query(
                    f'match $c isa conversation, has conversation-uid "{conversation_uid}"; '
                    f"insert $c has last-full-sync-timestamp {current_ts};"
                ).resolve()

                logger.info(f"原子事务即将提交：群聊 {conversation_uid} 成员列表及同步标记。")
                tx.commit()

        try:
            # 将整个原子操作作为一个单元，扔到后台线程执行
            await asyncio.to_thread(db_write_atomic_transaction)
            logger.success(f"群聊 {conversation_uid} 成员列表批量更新成功。")
            return True
        except Exception as e:
            logger.error(f"批量更新群聊 {conversation_uid} 成员的原子事务失败: {e}", exc_info=True)
            return False

    # 一个独立的标记函数，用于处理成员列表为空的情况
    async def mark_group_as_synced(self, conversation_uid: str) -> None:
        """仅将会话标记为已同步，不处理成员列表."""
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write_mark() -> None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                current_ts = int(time.time() * 1000)
                tx.query(
                    f"match $c isa conversation, "
                    f'has conversation-uid "{conversation_uid}"; '
                    f"$c has last-full-sync-timestamp $old_ts; delete $c has $old_ts;"
                ).resolve()
                tx.query(
                    f"match $c isa conversation, "
                    f'has conversation-uid "{conversation_uid}"; '
                    f"insert $c has last-full-sync-timestamp {current_ts};"
                ).resolve()
                tx.commit()

        try:
            await asyncio.to_thread(db_write_mark)
        except Exception as e:
            logger.error(f"标记群聊 {conversation_uid} 为已同步时失败: {e}", exc_info=True)

    async def is_group_sync_due(self, conversation_uid: str, ttl_seconds: int = 86400) -> bool:
        """检查群组是否需要进行完整的成员列表同步（例如超过24小时未同步）."""
        query = f"""
        match
            $c isa conversation, has conversation-uid "{conversation_uid}";
        fetch {{ "last_sync": $c.last-full-sync-timestamp }};
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> bool:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_documents())
                if not answers or "last_sync" not in answers[0]:
                    logger.debug(f"群聊 {conversation_uid} 从未进行过成员同步。")
                    return True  # 从未同步过，需要同步

                last_sync_ts = answers[0]["last_sync"]
                if last_sync_ts is None:
                    logger.debug(f"群聊 {conversation_uid} 从未进行过成员同步。")
                    return True

                if (time.time() * 1000 - last_sync_ts) > (ttl_seconds * 1000):  # 使用毫秒进行比较
                    logger.debug(f"群聊 {conversation_uid} 的成员列表缓存已过期。")
                    return True  # 同步时间已过期

                return False

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"检查群聊 {conversation_uid} 同步状态时失败: {e}", exc_info=True)
            return False  # 出错时保守地返回False，避免频繁触发

    async def get_group_member_count(self, conversation_uid: str) -> int:
        """获取指定群聊的成员数量."""
        query = f"""
        match
            $group isa conversation, has conversation-uid "{conversation_uid}";
            (member: $member, group: $group) isa membership;
        reduce $count = count;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> int:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # 使用 .resolve().as_value() 来直接获取聚合结果
                result_iterator = tx.query(query).resolve()
                result_value = next(result_iterator, None)
                if result_value:
                    return result_value.as_value().get_integer()
                return 0

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取群聊 {conversation_uid} 成员数量失败: {e}", exc_info=True)
            return 0
