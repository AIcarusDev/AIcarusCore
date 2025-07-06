# 文件路径: src/database/services/person_storage_service.py
import time
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from arangoasync.collection import EdgeCollection, StandardCollection  # 确保导入 EdgeCollection

from src.common.custom_logging.logging_config import get_logger
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

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager

    async def _get_collection(self, name: str, is_edge: bool = False) -> StandardCollection | EdgeCollection:
        """
        一个懒人工具，用来获取集合实例。现在它知道边集合要特殊对待了。
        """
        return await self.conn_manager.get_collection(name, is_edge=is_edge)

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
                await accounts_collection.update({"_key": account_uid, "last_known_nickname": user_info.user_nickname})

            # AQL图遍历查询，从账号节点出发，反向查找拥有它的“人”
            query = """
                FOR p IN 1..1 INBOUND @account_id @@edge_collection
                    RETURN { person_id: p._key }
            """
            bind_vars = {
                "account_id": f"{CoreDBCollections.ACCOUNTS}/{account_uid}",
                "@edge_collection": CoreDBCollections.HAS_ACCOUNT,
            }
            person_results = await self.conn_manager.execute_query(query, bind_vars)

            if person_results:
                if person_id := person_results[0].get("person_id"):
                    logger.debug(f"账号 {account_uid} 已关联到Person: {person_id}")
                    return person_id, account_uid

            # 这种情况不应该发生，除非数据不一致。我们创建一个新的人并关联。
            logger.warning(f"数据不一致！账号 {account_uid} 存在但没有关联的Person。将为其创建新的Person。")
            return await self._create_person_for_existing_account(account_doc)
        else:
            # 没找到账号，说明是新面孔，创建人和账号，再把他们绑一起
            logger.debug(f"未找到账号: {account_uid}，将创建新的Person和Account。")
            return await self._create_new_person_with_account(user_info, platform)

    async def _create_person_for_existing_account(self, account_doc: dict[str, Any]) -> tuple[str | None, str | None]:
        """内部工具：为一个已存在的账号创建一个新的人，并用边连起来。"""
        person = PersonDocument.create_new()
        account_uid = account_doc["_key"]
        account_id = account_doc["_id"]

        query = """
            LET person_doc = @person_doc
            LET timestamp = @timestamp

            LET person_result = (
                INSERT person_doc IN @@persons_coll
                RETURN NEW
            )[0]

            LET edge_doc = {
                _key: CONCAT(person_result._key, "_has_", @account_key),
                _from: person_result._id,
                _to: @account_id,
                created_at: timestamp
            }

            INSERT edge_doc IN @@has_account_coll

            RETURN { person_id: person_result._key, account_uid: @account_key }
        """
        bind_vars = {
            "person_doc": person.to_dict(),
            "timestamp": int(time.time() * 1000),
            "account_key": account_uid,
            "account_id": account_id,
            "@persons_coll": CoreDBCollections.PERSONS,
            "@has_account_coll": CoreDBCollections.HAS_ACCOUNT,
        }

        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results and isinstance(results, list) and len(results) > 0:
                result = results[0]
                person_id = result.get("person_id")
                returned_account_uid = result.get("account_uid")
                if person_id and returned_account_uid:
                    logger.info(
                        f"AQL事务成功：为现有账号 '{returned_account_uid}' 创建并关联了新的 Person '{person_id}'。"
                    )
                    return person_id, returned_account_uid

            logger.error(f"为现有账号创建Person的AQL事务执行后未能返回有效的ID, 返回结果: {results}")
            return None, None

        except Exception as e:
            logger.error(f"为现有账号创建Person的AQL事务执行失败: {e}", exc_info=True)
            return None, None

    async def _create_new_person_with_account(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """内部工具：创建一个新的人，一个新的账号，并用边连起来。使用单个AQL查询确保原子性。"""
        person = PersonDocument.create_new()
        account = AccountDocument.from_user_info(user_info, platform)

        # 使用单个AQL查询来确保操作的原子性，替代JS事务
        query = """
            LET person_doc = @person_doc
            LET account_doc = @account_doc
            LET timestamp = @timestamp

            LET person_result = (
                UPSERT { _key: person_doc._key }
                INSERT person_doc
                UPDATE {}
                IN @@persons_coll
                RETURN NEW
            )[0]

            LET account_result = (
                UPSERT { _key: account_doc._key }
                INSERT account_doc
                UPDATE {}
                IN @@accounts_coll
                RETURN NEW
            )[0]

            LET edge_doc = {
                _key: CONCAT(person_result._key, "_has_", account_result._key),
                _from: person_result._id,
                _to: account_result._id,
                created_at: timestamp
            }

            UPSERT { _key: edge_doc._key }
            INSERT edge_doc
            UPDATE {}
            IN @@has_account_coll

            RETURN { person_id: person_result._key, account_uid: account_result._key }
        """
        bind_vars = {
            "person_doc": person.to_dict(),
            "account_doc": account.to_dict(),
            "timestamp": int(time.time() * 1000),
            "@persons_coll": CoreDBCollections.PERSONS,
            "@accounts_coll": CoreDBCollections.ACCOUNTS,
            "@has_account_coll": CoreDBCollections.HAS_ACCOUNT,
        }

        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results and isinstance(results, list) and len(results) > 0:
                result = results[0]
                person_id = result.get("person_id")
                account_uid = result.get("account_uid")
                if person_id and account_uid:
                    logger.info(f"AQL事务成功：创建/关联了 Person '{person_id}' 和 Account '{account_uid}'。")
                    return person_id, account_uid

            logger.error(f"AQL事务执行后未能返回有效的ID, 返回结果: {results}")
            return None, None

        except Exception as e:
            logger.error(f"创建Person和Account的AQL事务执行失败: {e}", exc_info=True)
            return None, None

    async def update_membership(
        self, account_uid: str, conversation_id: str, user_info: ProtocolUserInfo, conversation_name: str | None
    ) -> None:
        """更新账号在会话中的成员信息（边属性）。"""
        # ↓↓↓ 这里的调用现在是正确的了，因为 is_edge=True 会通过图对象获取集合 ↓↓↓
        _ = await self._get_collection(CoreDBCollections.PARTICIPATES_IN, is_edge=True)
        from_vertex = f"{CoreDBCollections.ACCOUNTS}/{account_uid}"
        to_vertex = f"{CoreDBCollections.CONVERSATIONS}/{conversation_id}"

        # 边的 _key 可以是唯一的，比如 _from 和 _to 的组合
        edge_key = f"{account_uid}_in_{conversation_id}"

        props = MembershipProperties(
            group_name=conversation_name,
            cardname=user_info.user_cardname,
            permission_level=user_info.permission_level,
            title=user_info.user_titlename,
            last_active_timestamp=int(time.time() * 1000),
        )

        edge_doc = {"_key": edge_key, "_from": from_vertex, "_to": to_vertex, **props.to_dict()}

        # 使用UPSERT AQL语句，这比先查后插/更新更高效、更原子性
        query = """
            UPSERT { _key: @key }
            INSERT @doc
            UPDATE @doc
            IN @@collection
            RETURN NEW
        """
        bind_vars = {"key": edge_key, "doc": edge_doc, "@collection": CoreDBCollections.PARTICIPATES_IN}

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
