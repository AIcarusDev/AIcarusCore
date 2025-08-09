# src/database/services/entity_graph_service.py (本体论重构 V1.4 - 封装最终版)
import asyncio
import time
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from arangoasync.collection import EdgeCollection, StandardCollection
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
)
from src.database.models import (
    AccountDetails,
    ConversationDetails,
    EntityDocument,
    EntityProfileDocument,
    MembershipProperties,
    PlatformDetails,
)

logger = get_logger(__name__)

SELF_PROFILE_ID = "aic_person_0"


class EntityGraphService:
    """此类负责管理实体(Entity)与实体侧写(EntityProfile)之间的关系图谱.

    (究极进化版) 它现在是所有客观实体（包括账户和会话）的唯一管理者。
    它处理所有实体的创建、查找，以及它们之间的关系（如 represents, is_present_in）。
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        """初始化实体图谱服务."""
        self.conn_manager = conn_manager
        self._platform_entity_cache: dict[str, EntityDocument] = {}
        self._platform_entity_lock = asyncio.Lock()

    async def _get_collection(
        self, name: str, is_edge: bool = False
    ) -> StandardCollection | EdgeCollection:
        """一个懒人工具，用来获取集合实例."""
        return await self.conn_manager.get_collection(name, is_edge=is_edge)

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
            # 检查昵称是否有变化，如果有，则更新
            if (
                user_info.user_nickname
                and entity_doc.get("details", {}).get("last_known_nickname")
                != user_info.user_nickname
            ):
                # 使用 AQL 进行原子性的合并更新，这是最安全、最正确的方式
                query = """
                    UPDATE @key WITH { details: { last_known_nickname: @nickname } }
                    IN @@collection OPTIONS { mergeObjects: true }
                """
                bind_vars = {
                    "key": entity_uid,
                    "nickname": user_info.user_nickname,
                    "@collection": CoreDBCollections.ENTITIES,
                }
                # 执行查询，但不关心返回结果
                await self.conn_manager.execute_query(query, bind_vars)
                logger.debug(f"已通过AQL更新实体 '{entity_uid}' 的 last_known_nickname。")

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
            # 更新对此方法的内部调用
            return await self.create_new_profile_with_account_entity(user_info, platform)

    async def _create_profile_for_existing_entity(
        self, entity_doc: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """内部工具：为一个已存在的实体创建一个新的侧写，并用'represents'边连接."""
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

    # --- [修改] 变为公共方法，移除前导下划线，并更新文档字符串 ---
    async def create_new_profile_with_account_entity(
        self,
        user_info: ProtocolUserInfo,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """公共接口：创建一个新的Profile和一个新的“账户”Entity，并用 'represents' 边连接它们.

        这是一个原子性的操作，用于为新用户或新平台身份登记.

        Args:
            user_info: 包含用户ID和昵称的协议对象。
            platform: 该账户所属的平台ID。
            is_self: 如果为True，则将此账户关联到核心AI的唯一Profile (aic_person_0)。

        Returns:
            一个元组 (profile_id, account_entity_uid)，如果操作失败则返回 (None, None)。
        """
        profile = (
            EntityProfileDocument(_key=SELF_PROFILE_ID, profile_id=SELF_PROFILE_ID)
            if is_self
            else EntityProfileDocument.create_new()
        )

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
        Returns:
            bool: 更新是否成功
        """
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

        edge_doc_insert = {
            "_key": edge_key,
            "_from": from_vertex,
            "_to": to_vertex,
            **props.to_dict(),
        }
        edge_doc_update = props.to_dict()  # 更新时只需要更新属性

        # 使用 AQL 的 UPSERT 语句
        query = """
            UPSERT { _key: @key }
            INSERT @doc_insert
            UPDATE @doc_update IN @@collection
        """
        # 这里的 @collection 是一个占位符，用于指定集合名称
        # 这样可以避免硬编码集合名，增加灵活性和可维护性
        bind_vars = {
            "key": edge_key,
            "doc_insert": edge_doc_insert,
            "doc_update": edge_doc_update,
            "@collection": CoreDBCollections.IS_PRESENT_IN,
        }

        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.debug(
                f"成功更新存在关系: Entity '{account_entity_uid}' "
                f"in Conversation Entity '{conversation_entity_uid}'"
            )
            return True
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)
            return False

    async def get_or_create_platform_entity(
        self, platform_id: str, display_name: str | None = None
    ) -> EntityDocument:
        """原子性地获取或创建一个平台实体(Entity)，并确保其拥有关联的主观侧写(Profile)."""
        # --- [并发修复] 步骤 1: 检查内存缓存 ---
        if platform_id in self._platform_entity_cache:
            return self._platform_entity_cache[platform_id]

        # --- [并发修复] 步骤 2: 加锁，防止多个协程同时进行数据库操作 ---
        async with self._platform_entity_lock:
            # 双重检查，可能在等待锁的时候，其他协程已经完成了工作
            if platform_id in self._platform_entity_cache:
                return self._platform_entity_cache[platform_id]

            # --- 原有逻辑在新锁的保护下执行 ---
            entity_uid = platform_id
            timestamp = int(time.time() * 1000)

            details = PlatformDetails(
                platform_id=platform_id, display_name=display_name or platform_id
            )
            entity_to_upsert = EntityDocument(
                _key=entity_uid,
                entity_uid=entity_uid,
                entity_type="platform",
                details=details,
                created_at=timestamp,
            )

            upsert_entity_query = """
                UPSERT { _key: @uid }
                INSERT @doc_to_insert
                UPDATE { details: { display_name: @display_name } } IN @@entities_coll
                RETURN NEW
            """
            upsert_entity_bind_vars = {
                "uid": entity_uid,
                "doc_to_insert": entity_to_upsert.to_dict(),
                "display_name": display_name or platform_id,
                "@entities_coll": CoreDBCollections.ENTITIES,
            }

            entity_results = await self.conn_manager.execute_query(
                upsert_entity_query, upsert_entity_bind_vars
            )
            if not (entity_results and entity_results[0]):
                raise RuntimeError(f"创建或更新平台实体 '{entity_uid}' 时数据库未能返回有效文档。")

            platform_entity_doc_dict = entity_results[0]
            platform_entity_id = platform_entity_doc_dict["_id"]

            profile_to_insert = EntityProfileDocument.create_new()

            ensure_profile_query = """
                LET platform_entity_id = @platform_entity_id
                LET existing_profile = (
                    FOR p IN 1..1 INBOUND platform_entity_id @@represents_coll
                    LIMIT 1
                    RETURN p
                )[0]
                LET profile_creation_result = (
                    FILTER existing_profile == null
                    LET new_profile = (INSERT @profile_doc IN @@profiles_coll RETURN NEW)[0]
                    LET edge_doc = {
                        _key: CONCAT(new_profile._key, "_represents_", @entity_key),
                        _from: new_profile._id,
                        _to: platform_entity_id,
                        created_at: @timestamp
                    }
                    INSERT edge_doc IN @@represents_coll
                )
                RETURN true
            """
            ensure_profile_bind_vars = {
                "platform_entity_id": platform_entity_id,
                "profile_doc": profile_to_insert.to_dict(),
                "entity_key": entity_uid,
                "timestamp": timestamp,
                "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
                "@represents_coll": CoreDBCollections.REPRESENTS,
            }

            await self.conn_manager.execute_query(ensure_profile_query, ensure_profile_bind_vars)

            platform_entity_obj = EntityDocument.from_dict(platform_entity_doc_dict)

            # --- [并发修复] 步骤 3: 将结果存入缓存 ---
            self._platform_entity_cache[platform_id] = platform_entity_obj

            logger.debug(f"成功确保平台实体 '{entity_uid}' 及其 Profile 和关系存在。")
            return platform_entity_obj

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

    async def get_all_self_entities(self) -> list[dict[str, Any]]:
        """获取祂自身（SELF_PROFILE_ID）关联的所有平台实体信息."""
        query = """
            LET self_profile = DOCUMENT(@@profiles_coll, @self_profile_key)
            FILTER self_profile != null
            FOR entity IN 1..1 OUTBOUND self_profile @@represents_coll
                FILTER entity.entity_type == 'account'
                RETURN MERGE(entity, {
                    details: UNSET(entity.details, "friend_remark", "friend_request_pending")
                })
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
                FILTER doc.details.platform == @platform
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
        bind_vars = {
            "@entities_coll": CoreDBCollections.ENTITIES,
            "platform": platform,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(f"查询平台 '{platform}' 的待处理好友请求失败: {e}", exc_info=True)
            return []

    async def get_self_presence_in_conversation(
        self, platform: str, conversation_entity_uid: str
    ) -> dict[str, Any] | None:
        """获取'祂'在特定会话中的存在信息 (is_present_in 边的属性).

        Args:
            platform (str): 当前平台ID.
            conversation_entity_uid (str): 目标会话实体的UID.

        Returns:
            一个包含群名片、权限等信息的字典，如果不存在则返回 None.
        """
        query = """
            LET self_profile = DOCUMENT(@@profiles_coll, @self_profile_key)
            FILTER self_profile != null

            LET self_account_entity = (
                FOR entity IN 1..1 OUTBOUND self_profile @@represents_coll
                    FILTER entity.details.platform == @platform
                    AND entity.entity_type == 'account'
                    LIMIT 1
                    RETURN entity
            )[0]
            FILTER self_account_entity != null

            FOR conv, edge IN 1..1 OUTBOUND self_account_entity @@is_present_in_coll
                FILTER conv._key == @conv_uid
                LIMIT 1
                RETURN UNSET(edge, "_key", "_id", "_rev", "_from", "_to")
        """
        bind_vars = {
            "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
            "self_profile_key": SELF_PROFILE_ID,
            "@represents_coll": CoreDBCollections.REPRESENTS,
            "@is_present_in_coll": CoreDBCollections.IS_PRESENT_IN,
            "platform": platform,
            "conv_uid": conversation_entity_uid,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results[0] if results else None
        except Exception as e:
            logger.error(
                f"查询自身在会话 '{conversation_entity_uid}' 的存在信息时失败: {e}", exc_info=True
            )
            return None

    async def get_recently_active_conversation_entities_with_details(
        self, exclude_conversation_id: str | None, self_bot_ids: dict[str, str]
    ) -> list[dict[str, Any]]:
        """通过一次AQL查询，获取所有活跃会話的完整聚合信息."""
        query = """
            LET bot_ids = VALUES(@self_bot_ids)
            LET twenty_four_hours_ago = DATE_TIMESTAMP(DATE_SUBTRACT(DATE_NOW(), 24, "h"))

            FOR conv IN @@entities_coll
                FILTER conv.entity_type == 'conversation'
                AND conv._key != @exclude_conv_id

                LET events_in_conv = (
                    FOR e IN @@events_coll
                        FILTER e.conversation_id_extracted == conv.details.conversation_id
                        AND e.timestamp >= twenty_four_hours_ago
                        SORT e.timestamp DESC
                        RETURN e
                )

                FILTER LENGTH(events_in_conv) > 0
                LET latest_event = events_in_conv[0]

                LET last_read_ts = conv.last_read_timestamp || 0

                LET unread_events = (
                    FOR e IN events_in_conv
                        FILTER e.timestamp > last_read_ts
                        AND e.user_info.user_id NOT IN bot_ids
                        RETURN e
                )

                LET unread_count = LENGTH(unread_events)

                LET latest_high_priority_event = (
                    FOR e IN unread_events
                        LET is_high_priority = (
                            FOR seg IN e.content
                                FILTER (seg.type == 'at' OR seg.type == 'quote')
                                AND seg.data.user_id IN bot_ids
                                LIMIT 1
                                RETURN true
                        )[0]
                        FILTER is_high_priority == true
                        SORT e.timestamp DESC
                        LIMIT 1
                        RETURN e
                )[0]

                SORT latest_event.timestamp DESC
                RETURN {
                    conv_doc: conv,
                    latest_event: latest_high_priority_event || latest_event,
                    unread_count: unread_count,
                    has_high_priority: latest_high_priority_event != null
                }
        """
        bind_vars = {
            "@entities_coll": CoreDBCollections.ENTITIES,
            "@events_coll": CoreDBCollections.EVENTS,
            "exclude_conv_id": exclude_conversation_id,
            "self_bot_ids": self_bot_ids,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(f"查询最近活跃会话详情时失败: {e}", exc_info=True)
            return []

    async def get_conversation_last_read_timestamp(self, conversation_entity_uid: str) -> float:
        """获取一个会话的最后已读时间戳."""
        entity = await self.get_entity_by_key(conversation_entity_uid)
        # 如果实体存在且有时间戳，则返回它，否则返回0
        return entity.get("last_read_timestamp", 0.0) if entity else 0.0

    async def update_conversation_last_read_timestamp(
        self, conversation_entity_uid: str, timestamp: float
    ) -> bool:
        """更新一个会话的最后已读时间戳."""
        try:
            entities_collection = await self._get_collection(CoreDBCollections.ENTITIES)
            await entities_collection.update(
                {"_key": conversation_entity_uid, "last_read_timestamp": timestamp}
            )
            logger.info(f"已更新会话实体 '{conversation_entity_uid}' 的最后已读时间戳。")
            return True
        except Exception as e:
            logger.error(
                f"更新会话实体 '{conversation_entity_uid}' 的时间戳失败: {e}", exc_info=True
            )
            return False

    async def get_entity_by_key(self, entity_uid: str) -> EntityDocument | None:
        """根据 entity_uid (_key) 获取单个实体文档，并将其转换为 EntityDocument 对象."""
        if not entity_uid:
            return None
        try:
            entities_collection = await self._get_collection(CoreDBCollections.ENTITIES)
            doc = await entities_collection.get(entity_uid)
            return EntityDocument.from_dict(doc) if doc else None
        except Exception as e:
            logger.error(f"根据 key '{entity_uid}' 获取实体时失败: {e}", exc_info=True)
            return None

    async def get_or_create_conversation_entity(
        self,
        conversation_id: str,
        platform: str,
        conv_type: str,
        name: str | None = None,
        extra: dict | None = None,
    ) -> EntityDocument:
        """获取或创建一个会話实体(Entity)，并确保其拥有关联的主观侧写(Profile).

        以及一条指向其所属平台实体的 `resides_on` 边.
        此方法通过分离“实体创建”和“关系创建”两个步骤来规避 ArangoDB 的事务限制.
        """
        # 步骤 1: (Python 层面) 获取平台实体的完整 ID (_id)。
        # 这是一个安全操作，因为它假定平台实体已被上游逻辑创建。
        platform_entity = await self.get_or_create_platform_entity(platform)
        if not (platform_entity and platform_entity._id):
            raise RuntimeError(f"未能为平台 '{platform}' 获取有效的实体文档。")
        platform_entity_id = platform_entity._id

        # --- 步骤 2: 确保会话实体存在 ---
        conv_entity_uid = build_conversation_entity_uid(platform, conv_type, conversation_id)
        timestamp = int(time.time() * 1000)
        details = ConversationDetails(
            platform=platform,
            conversation_id=conversation_id,
            type=conv_type,
            name=name,
            parent_id=None,
            avatar=None,
            extra=extra or {},
        )
        conv_entity_to_upsert = EntityDocument(
            _key=conv_entity_uid,
            entity_uid=conv_entity_uid,
            entity_type="conversation",
            details=details,
            created_at=timestamp,
        )

        upsert_conv_entity_query = """
            UPSERT { _key: @uid }
            INSERT @doc_to_insert
            UPDATE { details: { name: @name, extra: @extra } } IN @@entities_coll
            RETURN NEW
        """
        upsert_conv_entity_bind_vars = {
            "uid": conv_entity_uid,
            "doc_to_insert": conv_entity_to_upsert.to_dict(),
            "name": name,
            "extra": extra or {},
            "@entities_coll": CoreDBCollections.ENTITIES,
        }

        conv_entity_results = await self.conn_manager.execute_query(
            upsert_conv_entity_query, upsert_conv_entity_bind_vars
        )
        if not (conv_entity_results and conv_entity_results[0]):
            raise RuntimeError(f"创建或更新会话实体 '{conv_entity_uid}' 时数据库未能返回有效文档。")

        conv_entity_doc = conv_entity_results[0]

        # --- 步骤 3: 确保 Profile 和关系边存在 ---
        profile_to_insert = EntityProfileDocument.create_new()

        ensure_relations_query = """
            LET profile_doc = @profile_doc
            LET timestamp = @timestamp
            LET conv_entity_id = @conv_entity_id
            LET platform_entity_id = @platform_entity_id

            // 子任务1: 确保 Profile 和 represents 边存在
            LET ensure_profile = (
                FILTER (FOR p IN 1..1 INBOUND conv_entity_id @@represents_coll LIMIT 1 RETURN 1)[0] == null
                LET new_profile = (INSERT profile_doc IN @@profiles_coll RETURN NEW)[0]
                INSERT {
                    _key: CONCAT(new_profile._key, "_represents_", @conv_entity_key),
                    _from: new_profile._id,
                    _to: conv_entity_id,
                    created_at: timestamp
                } IN @@represents_coll
            )

            // 子任务2: 确保 `resides_on` 边存在
            LET resides_on_edge = {
                _from: conv_entity_id,
                _to: platform_entity_id,
                created_at: timestamp
            }
            UPSERT { _from: resides_on_edge._from, _to: resides_on_edge._to }
            INSERT resides_on_edge
            UPDATE {} IN @@resides_on_coll

            RETURN true
        """  # noqa: E501

        ensure_relations_bind_vars = {
            "platform_entity_id": platform_entity_id,
            "conv_entity_id": conv_entity_doc["_id"],
            "conv_entity_key": conv_entity_uid,
            "profile_doc": profile_to_insert.to_dict(),
            "timestamp": timestamp,  # 复用之前生成的timestamp
            "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
            "@represents_coll": CoreDBCollections.REPRESENTS,
            "@resides_on_coll": CoreDBCollections.RESIDES_ON,
        }

        await self.conn_manager.execute_query(ensure_relations_query, ensure_relations_bind_vars)

        logger.debug(f"成功确保会话实体 '{conv_entity_uid}' 及其 Profile 和关系存在。")
        return EntityDocument.from_dict(conv_entity_doc)

    async def find_conversation_entity_by_platform_and_id(
        self, platform: str, conversation_id: str
    ) -> EntityDocument | None:
        """根据平台和裸的会话ID（如纯数字群号），查找对应的会话实体.

        它会尝试匹配 group 和 private 两种可能性。
        """
        # 尝试匹配 group 类型
        group_entity_uid = build_conversation_entity_uid(platform, "group", conversation_id)
        entity_doc = await self.get_entity_by_key(group_entity_uid)
        if entity_doc:
            return entity_doc

        # 如果不是 group，再尝试匹配 private 类型
        private_entity_uid = build_conversation_entity_uid(platform, "private", conversation_id)
        entity_doc = await self.get_entity_by_key(private_entity_uid)
        if entity_doc:
            return entity_doc

        logger.warning(
            f"在平台 '{platform}' 下，未能通过裸ID '{conversation_id}' "
            f"找到任何 group 或 private 类型的会话实体。"
        )
        return None

    # --- [新增] 公共方法，用于更新好友请求状态 ---
    async def update_friend_request_status(
        self, entity_uid: str, flag: str, comment: str, timestamp: int
    ) -> bool:
        """更新指定实体的待处理好友请求信息."""
        query = """
            UPDATE @key WITH {
                details: {
                    friend_request_pending: {
                        flag: @flag,
                        comment: @comment,
                        timestamp: @timestamp
                    }
                }
            } IN @@collection OPTIONS { mergeObjects: true, keepNull: false }
        """
        bind_vars = {
            "key": entity_uid,
            "flag": flag,
            "comment": comment,
            "timestamp": timestamp,
            "@collection": CoreDBCollections.ENTITIES,
        }
        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.info(f"已更新实体 '{entity_uid}' 的好友请求状态。")
            return True
        except Exception as e:
            logger.error(f"更新实体 '{entity_uid}' 好友请求状态时失败: {e}", exc_info=True)
            return False

    # --- [新增] 公共方法，用于完成好友请求处理 ---
    async def finalize_friend_request(
        self, entity_uid: str, approved: bool, remark: str | None
    ) -> bool:
        """完成好友请求处理，清除待处理状态并可选地更新好友备注."""
        update_fields = {"friend_request_pending": None}
        if approved and remark:
            update_fields["friend_remark"] = remark.strip()

        # 使用 AQL 的 MERGE 函数来动态构建更新对象
        query = """
            LET current_details = DOCUMENT(@@collection, @key).details
            UPDATE @key WITH {
                details: MERGE(current_details, @update_fields)
            } IN @@collection
        """
        bind_vars = {
            "key": entity_uid,
            "update_fields": update_fields,
            "@collection": CoreDBCollections.ENTITIES,
        }
        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.info(f"已完成实体 '{entity_uid}' 的好友请求处理。")
            return True
        except Exception as e:
            logger.error(f"完成实体 '{entity_uid}' 好友请求处理时失败: {e}", exc_info=True)
            return False

    async def update_bot_profile_in_conversation(
        self,
        conversation_entity_uid: str,
        update_type: str,
        new_value: Any,
    ) -> bool:
        """一个专门的公共方法，用于原子性地更新会话实体中 '祂' 的档案信息.

        它使用 AQL 的 MERGE 函数来安全地更新嵌套对象.

        Args:
            conversation_entity_uid: 目标会话实体的 _key。
            update_type: 要更新的字段类型，例如 "card_change"。
            new_value: 要设置的新值。

        Returns:
            操作是否成功。
        """
        # 映射更新类型到数据库字段名
        field_to_update_map = {"card_change": "card", "role_change": "role"}
        db_field_name = field_to_update_map.get(update_type)

        if not db_field_name:
            logger.warning(f"未知的档案更新类型: '{update_type}'，操作已忽略。")
            return False

        # 构建要合并到 bot_profile_in_this_conversation 对象中的数据
        update_data = {
            db_field_name: new_value,
            "updated_at": int(time.time() * 1000),
        }

        # 使用 AQL 的 MERGE 来安全地更新嵌套对象，这能处理字段不存在的初始情况
        query = """
            LET doc = DOCUMENT(@@collection, @key)
            FILTER doc != null
            // MERGE 会智能地合并旧对象和新数据
            LET new_profile = MERGE(doc.bot_profile_in_this_conversation, @update_data)
            UPDATE doc WITH { bot_profile_in_this_conversation: new_profile } IN @@collection
            RETURN true
        """
        bind_vars = {
            "@collection": CoreDBCollections.ENTITIES,
            "key": conversation_entity_uid,
            "update_data": update_data,
        }

        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results:
                logger.info(f"已通过服务层成功更新会话实体 '{conversation_entity_uid}' 的档案。")
                return True
            else:
                logger.warning(
                    f"尝试更新会话实体 '{conversation_entity_uid}' 档案时，"
                    "AQL 查询未返回成功标识（可能是文档不存在）。"
                )
                return False
        except Exception as e:
            logger.error(
                f"通过服务层更新会话实体 '{conversation_entity_uid}' 档案时失败: {e}",
                exc_info=True,
            )
            return False
