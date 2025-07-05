# src/database/services/person_storage_service.py
import time
from typing import Any

from arangoasync.exceptions import DocumentInsertError

from src.common.custom_logging.logging_config import get_logger
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.database import (
    AccountDocument,
    ArangoDBConnectionManager,
    CoreDBCollections,
    MembershipProperties,
    PersonDocument,
)

logger = get_logger(__name__)

class PersonStorageService:
    """
    哼，这里就是我们新的“老鸨”，管理人际关系！
    负责处理 Person, Account 以及它们之间关系的图数据库操作。
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager):
        self.conn_manager = conn_manager

    async def _get_collection(self, name: str, is_edge: bool = False):
        """一个懒人工具，用来获取集合实例。"""
        return await self.conn_manager.get_collection(name, is_edge)

    async def find_or_create_person_and_account(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """
        根据用户和平台信息，查找或创建“人”和“账号”节点，并确保它们之间有'has_account'关系。
        返回 (person_id, account_uid)。
        """
        if not user_info or not user_info.user_id:
            logger.warning("提供的UserInfo不完整，无法查找或创建Person/Account。")
            return None, None

        accounts_collection = await self._get_collection(CoreDBCollections.ACCOUNTS)
        account_uid = f"{platform}_{user_info.user_id}"

        # 1. 先找账号
        account_doc = await accounts_collection.get(account_uid)

        if account_doc:
            # 找到了账号，就去找这个账号属于哪个人
            logger.debug(f"找到了已存在的账号: {account_uid}")

            # 更新一下账号昵称，万一他改名了呢
            if user_info.user_nickname and account_doc.get("last_known_nickname") != user_info.user_nickname:
                 await accounts_collection.update(
                     {"_key": account_uid, "last_known_nickname": user_info.user_nickname}
                 )

            # AQL图遍历查询，从账号节点出发，反向查找拥有它的“人”
            query = """
                FOR p IN 1..1 INBOUND @account_id @@edge_collection
                    RETURN p._key
            """
            bind_vars = {
                "account_id": f"{CoreDBCollections.ACCOUNTS}/{account_uid}",
                "@edge_collection": CoreDBCollections.HAS_ACCOUNT,
            }
            person_keys = await self.conn_manager.execute_query(query, bind_vars)

            if person_keys:
                person_id = person_keys[0]
                logger.debug(f"账号 {account_uid} 已关联到Person: {person_id}")
                return person_id, account_uid
            else:
                # 这种情况不应该发生，除非数据不一致。我们创建一个新的人并关联。
                logger.warning(f"数据不一致！账号 {account_uid} 存在但没有关联的Person。将为其创建新的Person。")
                return await self._create_new_person_with_account(user_info, platform)
        else:
            # 没找到账号，说明是新面孔，创建人和账号，再把他们绑一起
            logger.debug(f"未找到账号: {account_uid}，将创建新的Person和Account。")
            return await self._create_new_person_with_account(user_info, platform)

    async def _create_new_person_with_account(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """内部工具：创建一个新的人，一个新的账号，并用边连起来。"""
        persons_collection = await self._get_collection(CoreDBCollections.PERSONS)
        accounts_collection = await self._get_collection(CoreDBCollections.ACCOUNTS)
        has_account_collection = await self._get_collection(CoreDBCollections.HAS_ACCOUNT, is_edge=True)

        # 创建“人”
        person = PersonDocument.create_new()
        try:
            await persons_collection.insert(person.to_dict())
        except DocumentInsertError:
            logger.error(f"创建新的Person节点 '{person.person_id}' 失败，可能已存在。")
            return None, None # 严重错误，直接返回

        # 创建“账号”
        account = AccountDocument.from_user_info(user_info, platform)
        try:
            await accounts_collection.insert(account.to_dict())
        except DocumentInsertError:
             logger.warning(f"创建Account节点 '{account.account_uid}' 失败，可能已由并发操作创建。将继续尝试连接。")
             # 不返回，继续尝试连接

        # 创建“关系”边
        edge_data = {
            "_from": person.to_dict()["_id"],
            "_to": account.to_dict()["_id"],
            "created_at": int(time.time() * 1000)
        }
        try:
            await has_account_collection.insert(edge_data)
            logger.info(f"成功创建并关联了新的Person '{person.person_id}' 和 Account '{account.account_uid}'。")
            return person.person_id, account.account_uid
        except DocumentInsertError as e:
            logger.error(f"创建Person和Account之间的关联边失败: {e}", exc_info=True)
            return None, None

    async def update_membership(
        self, account_uid: str, conversation_id: str, user_info: ProtocolUserInfo, conversation_name: str | None
    ):
        """更新账号在会话中的成员信息（边属性）。"""
        participates_in_collection = await self._get_collection(CoreDBCollections.PARTICIPATES_IN, is_edge=True)

        from_vertex = f"{CoreDBCollections.ACCOUNTS}/{account_uid}"
        to_vertex = f"{CoreDBCollections.CONVERSATIONS}/{conversation_id}"

        # 边的 _key 可以是唯一的，比如 _from 和 _to 的组合
        edge_key = f"{account_uid}_in_{conversation_id}"

        props = MembershipProperties(
            group_name=conversation_name,
            cardname=user_info.user_cardname,
            permission_level=user_info.permission_level,
            title=user_info.user_titlename,
            last_active_timestamp=int(time.time() * 1000)
        )

        edge_doc = {
            "_key": edge_key,
            "_from": from_vertex,
            "_to": to_vertex,
            **props.to_dict()
        }

        # 使用UPSERT AQL语句，这比先查后插/更新更高效、更原子性
        query = """
            UPSERT { _key: @key }
            INSERT @doc
            UPDATE @doc
            IN @@collection
            RETURN NEW
        """
        bind_vars = {
            "key": edge_key,
            "doc": edge_doc,
            "@collection": CoreDBCollections.PARTICIPATES_IN
        }

        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.debug(f"成功更新成员关系: Account '{account_uid}' in Conversation '{conversation_id}'")
        except Exception as e:
            logger.error(f"更新成员关系时失败: {e}", exc_info=True)

    async def get_person_details_by_account(self, platform: str, platform_id: str) -> dict[str, Any] | None:
        """
        根据平台和平台ID，获取这个“人”的完整信息，包括他所有的马甲。
        """
        account_uid = f"{platform}_{platform_id}"

        query = """
            LET account = DOCUMENT(@@accounts_coll, @account_uid)

            // 找到这个账号属于哪个人
            LET person = (
                FOR p IN 1..1 INBOUND account @@has_account_coll
                    RETURN p
            )[0]

            // 如果没找到人，就别玩了
            FILTER person != null

            // 找到这个人的所有账号
            LET all_accounts = (
                FOR acc IN 1..1 OUTBOUND person @@has_account_coll
                    RETURN acc
            )

            // 找到这个人参与的所有群聊
            LET all_memberships = (
                FOR acc IN all_accounts
                    FOR conv, edge IN 1..1 OUTBOUND acc @@participates_in_coll
                        RETURN {
                            membership_id: edge._key,
                            account_uid: acc.account_uid,
                            group_id: conv.conversation_id,
                            platform: conv.platform,
                            group_name: edge.group_name,
                            cardname: edge.cardname,
                            permission_level: edge.permission_level
                        }
            )

            RETURN {
                person_id: person.person_id,
                profile: person.profile,
                accounts: all_accounts,
                memberships: all_memberships,
                metadata: {
                    created_at: person.created_at,
                    updated_at: person.updated_at
                }
            }
        """
        bind_vars = {
            "account_uid": account_uid,
            "@accounts_coll": CoreDBCollections.ACCOUNTS,
            "@has_account_coll": CoreDBCollections.HAS_ACCOUNT,
            "@participates_in_coll": CoreDBCollections.PARTICIPATES_IN,
        }

        results = await self.conn_manager.execute_query(query, bind_vars)
        return results[0] if results else None