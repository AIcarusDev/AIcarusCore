# src/database/services/entity_graph_service.py (本体论重构 V1.1 - 究极进化版)
import time
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from arangoasync.collection import EdgeCollection, StandardCollection
from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
)

# // (+) 导入我们刚刚创造的新神之卡组！
from src.database.models import (
    AccountDetails,
    ConversationDetails,
    EntityDocument,
    EntityProfileDocument,
    MembershipProperties,
)

logger = get_logger(__name__)

# 保持这个特殊的自我Profile ID
SELF_PROFILE_ID = "aic_person_0"


class EntityGraphService:
    """此类负责管理实体(Entity)与实体侧写(EntityProfile)之间的关系图谱.

    (究极进化版) 它现在是所有客观实体（包括账户和会话）的唯一管理者。
    它处理所有实体的创建、查找，以及它们之间的关系（如 represents, is_present_in）。
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        """初始化实体图谱服务."""
        self.conn_manager = conn_manager

    async def _get_collection(
        self, name: str, is_edge: bool = False
    ) -> StandardCollection | EdgeCollection:
        """一个懒人工具，用来获取集合实例."""
        return await self.conn_manager.get_collection(name, is_edge=is_edge)

    # ==============================================================================
    # (+) 新增的核心方法：会话实体的创世纪！
    # ==============================================================================
    async def get_or_create_conversation_entity(
        self,
        conversation_id: str,
        platform: str,
        conv_type: str,
        name: str | None = None,
        extra: dict | None = None,
    ) -> EntityDocument:
        """获取或创建一个会话实体(Entity).

        这是新架构的核心，所有对“会话”的操作都将通过这里。
        它确保了每个会话在我们的知识图谱中都有一个唯一的、客观的实体代表。
        """
        # // 构造一个全局唯一的UID，比如 "qq_group_123456"
        entity_uid = f"{platform}_{conv_type}_{conversation_id}"
        entities_collection = await self._get_collection(CoreDBCollections.ENTITIES)

        doc = await entities_collection.get(entity_uid)
        if doc:
            # // 找到了！直接从数据库读档，然后用我们的 from_dict 复活成强类型老婆！
            logger.debug(f"成功找到已存在的会话实体: {entity_uid}")
            return EntityDocument.from_dict(doc)

        # // 没找到？那就创造一个新的！
        logger.info(f"未找到会话实体 '{entity_uid}'，将为其创建新的实体档案。")
        details = ConversationDetails(
            platform=platform,
            conversation_id=conversation_id,
            type=conv_type,
            name=name,
            parent_id=None,  # parent_id 暂时不在这里处理，需要更复杂的逻辑
            avatar=None,
            extra=extra or {},
        )
        new_entity = EntityDocument(
            _key=entity_uid,
            entity_uid=entity_uid,
            entity_type="conversation",
            details=details,
        )
        # // 用 UPSERT 更安全，万一在高并发下有另一个协程刚刚创建了它呢 (虽然概率很小)
        # // 这叫防御性编程，就像游戏里随时准备按翻滚键一样！
        await entities_collection.upsert(new_entity.to_dict())
        logger.info(f"成功创建了新的会话实体: {entity_uid}")
        return new_entity

    # ==============================================================================
    # (±) 重构的核心方法：账户实体与侧写的查找与创建
    # ==============================================================================
    async def find_or_create_profile_and_account_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """根据用户信息，查找或创建客观的“账户”实体(Entity)和主观侧写(Profile)，并确保它们'represents'关系.

        这个方法现在明确只处理 'account' 类型的实体。
        """
        if not user_info or not user_info.user_id:
            logger.warning("提供的UserInfo不完整，无法查找或创建Profile/Account Entity。")
            return None, None

        entities_collection = await self._get_collection(CoreDBCollections.ENTITIES)
        entity_uid = f"{platform}_{user_info.user_id}"

        entity_doc = await entities_collection.get(entity_uid)

        if entity_doc:
            logger.debug(f"找到了已存在的账户实体: {entity_uid}")
            if (
                user_info.user_nickname
                and entity_doc.get("details", {}).get("last_known_nickname")
                != user_info.user_nickname
            ):
                patch_data = {"details.last_known_nickname": user_info.user_nickname}
                await entities_collection.update_by_key(entity_uid, patch_data, merge=True)

            query = """
                FOR p IN 1..1 INBOUND @entity_id @@represents_edge_coll
                    RETURN p._key
            """
            bind_vars = {
                "entity_id": f"{CoreDBCollections.ENTITIES}/{entity_uid}",
                "@represents_edge_coll": CoreDBCollections.REPRESENTS,
            }
            profile_ids = await self.conn_manager.execute_query(query, bind_vars)

            if profile_ids:
                profile_id = profile_ids[0]
                logger.debug(f"实体 {entity_uid} 已被 Profile '{profile_id}' 所表征。")
                return profile_id, entity_uid

            logger.warning(
                f"数据不一致！实体 {entity_uid} 存在但没有关联的Profile。将为其创建新的Profile。"
            )
            return await self._create_profile_for_existing_entity(entity_doc)
        else:
            logger.debug(f"未找到账户实体: {entity_uid}，将创建新的 Profile 和 Entity。")
            return await self._create_new_profile_with_account_entity(user_info, platform)

    async def _create_profile_for_existing_entity(
        self, entity_doc: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """内部工具：为一个已存在的实体创建一个新的侧写，并用'represents'边连接."""
        # ... (此方法逻辑基本不变，因为它是通用的) ...
        profile = EntityProfileDocument.create_new()
        entity_uid = entity_doc["_key"]
        entity_id = entity_doc["_id"]

        query = """
            LET profile_doc = @profile_doc
            LET timestamp = @timestamp
            LET profile_result = (INSERT profile_doc IN @@profiles_coll RETURN NEW)[0]
            LET edge_doc = {
                _key: CONCAT(profile_result._key, "_represents_", @entity_key),
                _from: profile_result._id,
                _to: @entity_id,
                created_at: timestamp
            }
            INSERT edge_doc IN @@represents_coll
            RETURN { profile_id: profile_result._key, entity_uid: @entity_key }
        """
        bind_vars = {
            "profile_doc": profile.to_dict(),
            "timestamp": int(time.time() * 1000),
            "entity_key": entity_uid,
            "entity_id": entity_id,
            "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
            "@represents_coll": CoreDBCollections.REPRESENTS,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results:
                return results[0].get("profile_id"), results[0].get("entity_uid")
            return None, None
        except Exception as e:
            logger.error(f"为现有实体创建Profile的AQL事务执行失败: {e}", exc_info=True)
            return None, None

    async def _create_new_profile_with_account_entity(
        self,
        user_info: ProtocolUserInfo,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """(内部重构) 创建一个新的Profile和一个新的“账户”Entity，并连接它们."""
        profile = (
            EntityProfileDocument.create_new()
            if not is_self
            else EntityProfileDocument(_key=SELF_PROFILE_ID, profile_id=SELF_PROFILE_ID)
        )

        # // 核心区别：现在创建的是一个完整的 EntityDocument，类型是 account
        account_details = AccountDetails(
            platform=platform,
            platform_id=user_info.user_id,
            nickname=user_info.user_nickname,
            last_known_nickname=user_info.user_nickname,
        )
        entity_uid = f"{platform}_{user_info.user_id}"
        entity = EntityDocument(
            _key=entity_uid, entity_uid=entity_uid, entity_type="account", details=account_details
        )

        query = """
            LET profile_doc = @profile_doc
            LET entity_doc = @entity_doc
            LET timestamp = @timestamp
            LET profile_result = (UPSERT { _key: profile_doc._key } INSERT profile_doc UPDATE {} IN @@profiles_coll RETURN NEW)[0]
            LET entity_result = (UPSERT { _key: entity_doc._key } INSERT entity_doc UPDATE {} IN @@entities_coll RETURN NEW)[0]
            LET edge_doc = {
                _key: CONCAT(profile_result._key, "_represents_", entity_result._key),
                _from: profile_result._id,
                _to: entity_result._id,
                created_at: timestamp
            }
            UPSERT { _key: edge_doc._key } INSERT edge_doc UPDATE {} IN @@represents_coll
            RETURN { profile_id: profile_result._key, entity_uid: entity_result._key }
        """  # noqa: E501
        bind_vars = {
            "profile_doc": profile.to_dict(),
            "entity_doc": entity.to_dict(),
            "timestamp": int(time.time() * 1000),
            "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
            "@entities_coll": CoreDBCollections.ENTITIES,
            "@represents_coll": CoreDBCollections.REPRESENTS,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results:
                return results[0].get("profile_id"), results[0].get("entity_uid")
            return None, None
        except Exception as e:
            logger.error(f"创建 Profile 和 Account Entity 的AQL事务执行失败: {e}", exc_info=True)
            return None, None

    # ==============================================================================
    # (±) 重构的核心方法：关系管理
    # ==============================================================================
    async def update_presence_in_conversation(
        self,
        account_entity_uid: str,
        conversation_entity_uid: str,
        user_info: ProtocolUserInfo,
        conversation_name: str | None,
    ) -> bool:
        """更新一个账户实体在某个会话实体中的存在关系（is_present_in 边）.

        Args:
            account_entity_uid (str): 账户实体的 _key (e.g., "qq_123456")
            conversation_entity_uid (str): 会话实体的 _key (e.g., "qq_group_98765")
            user_info (ProtocolUserInfo): 用户在会话中的信息
            conversation_name (str | None): 会话的名称
        """
        # // 就像设定游戏角色的出场地点一样，from 是角色，to 是场景！
        from_vertex = f"{CoreDBCollections.ENTITIES}/{account_entity_uid}"
        to_vertex = f"{CoreDBCollections.ENTITIES}/{conversation_entity_uid}"
        edge_key = f"{account_entity_uid}_in_{conversation_entity_uid}"

        props = MembershipProperties(
            group_name=conversation_name,
            cardname=user_info.user_cardname,
            permission_level=user_info.permission_level,
            title=user_info.user_titlename,
            last_active_timestamp=int(time.time() * 1000),
        )

        edge_doc = {"_key": edge_key, "_from": from_vertex, "_to": to_vertex, **props.to_dict()}
        is_present_in_coll = await self._get_collection(
            CoreDBCollections.IS_PRESENT_IN, is_edge=True
        )

        try:
            await is_present_in_coll.upsert(edge_doc)
            logger.debug(
                f"成功更新存在关系: Entity '{account_entity_uid}' "
                f"in Conversation Entity '{conversation_entity_uid}'"
            )
            return True
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)
            return False

    # ==============================================================================
    # (±) 重构的图谱查询方法
    # ==============================================================================
    async def get_profile_details_by_entity(
        self, platform: str, platform_id: str
    ) -> dict[str, Any] | None:
        """根据平台和平台ID，获取这个“侧写”的完整信息，包括其表征的所有实体和存在关系."""
        entity_uid = f"{platform}_{platform_id}"

        # // 这段AQL就像是在浩瀚的星海（数据库）中，定位一颗星（Profile），
        # // 然后描绘出它的所有卫星（Entities）以及卫星的航行轨迹（Presences）
        query = """
            LET entity = DOCUMENT(@@entities_coll, @entity_uid)
            FILTER entity != null

            LET profile = (FOR p IN 1..1 INBOUND entity @@represents_coll RETURN p)[0]
            FILTER profile != null

            LET all_account_entities = (FOR e IN 1..1 OUTBOUND profile @@represents_coll RETURN e)

            LET all_presences = (
                FOR acc_entity IN all_account_entities
                    // acc_entity (账户) -> is_present_in -> conv_entity (会话)
                    FOR conv_entity, edge IN 1..1 OUTBOUND acc_entity @@is_present_in_coll
                        FILTER conv_entity.entity_type == 'conversation'
                        RETURN {
                            presence_id: edge._key,
                            account_uid: acc_entity.entity_uid,
                            conversation_uid: conv_entity.entity_uid,
                            conversation_details: conv_entity.details,
                            membership_properties: UNSET(edge, "_key", "_id", "_rev", "_from", "_to")
                        }
            )

            RETURN {
                profile_id: profile.profile_id,
                profile_data: profile.profile,
                entities: all_account_entities,
                presences: all_presences,
                metadata: { created_at: profile.created_at, updated_at: profile.updated_at }
            }
        """  # noqa: E501
        bind_vars = {
            "entity_uid": entity_uid,
            "@entities_coll": CoreDBCollections.ENTITIES,
            "@represents_coll": CoreDBCollections.REPRESENTS,
            "@is_present_in_coll": CoreDBCollections.IS_PRESENT_IN,
        }

        results = await self.conn_manager.execute_query(query, bind_vars)
        return results[0] if results else None

    # ==============================================================================
    # (->) 其他方法保持或微调以适应新世界
    # ==============================================================================
    async def get_all_self_entities(self) -> list[dict[str, Any]]:
        """获取祂自身（SELF_PROFILE_ID）关联的所有平台实体信息."""
        query = """
            LET self_profile = DOCUMENT(@@profiles_coll, @self_profile_key)
            FILTER self_profile != null
            FOR entity IN 1..1 OUTBOUND self_profile @@represents_coll
                FILTER entity.entity_type == 'account'
                RETURN UNSET(entity.details, "friend_remark", "friend_request_pending")
        """
        bind_vars = {
            "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
            "self_profile_key": SELF_PROFILE_ID,
            "@represents_coll": CoreDBCollections.REPRESENTS,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(f"获取自身所有平台实体信息时失败: {e}", exc_info=True)
            return []

    async def get_pending_friend_requests(self, platform: str) -> list[dict[str, Any]]:
        """获取指定平台所有待处理的好友请求."""
        query = """
            FOR doc IN @@entities_coll
                FILTER doc.platform == @platform
                AND doc.entity_type == 'account'
                AND doc.details.friend_request_pending != null
                RETURN {
                    user_id: doc.details.platform_id,
                    nickname: doc.details.last_known_nickname,
                    flag: doc.details.friend_request_pending.flag,
                    comment: doc.details.friend_request_pending.comment,
                    timestamp: doc.details.friend_request_pending.timestamp
                }
        """
        bind_vars = {"@entities_coll": CoreDBCollections.ENTITIES, "platform": platform}
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(f"查询平台 '{platform}' 的待处理好友请求失败: {e}", exc_info=True)
            return []
