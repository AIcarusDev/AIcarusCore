# src/database/services/entity_graph_service.py (本体论重构 V1.0)
import time
from typing import Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from arangoasync.collection import EdgeCollection, StandardCollection
from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
    EntityDocument,
    EntityProfileDocument,
    MembershipProperties,
)

logger = get_logger(__name__)

# 保持这个特殊的自我Profile ID
SELF_PROFILE_ID = "aic_person_0"


class EntityGraphService:
    """此类负责管理实体(Entity)与实体侧写(EntityProfile)之间的关系图谱.

    它提供了查找或创建客观实体(Entity)和主观侧写(Profile)的方法，
    并确保它们之间存在正确的'represents'（表征）关系。
    同时，它也管理实体在会话中的存在（is_present_in）关系。

    Attributes:
        conn_manager (ArangoDBConnectionManager): 数据库连接管理器实例，用于获取集合。
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        """初始化实体图谱服务."""
        self.conn_manager = conn_manager

    async def _get_collection(
        self, name: str, is_edge: bool = False
    ) -> StandardCollection | EdgeCollection:
        """一个懒人工具，用来获取集合实例。现在它知道边集合要特殊对待了."""
        return await self.conn_manager.get_collection(name, is_edge=is_edge)

    async def find_or_create_profile_and_entity(
        self, user_info: ProtocolUserInfo, platform: str
    ) -> tuple[str | None, str | None]:
        """根据用户信息，查找或创建客观实体(Entity)和主观侧写(Profile)，并确保它们'represents'关系.

        Args:
            user_info (ProtocolUserInfo): 包含用户信息的协议对象。
            platform (str): 用户所在的平台标识。

        Returns:
            tuple[str | None, str | None]: 返回 (profile_id, entity_uid)。
        """
        if not user_info or not user_info.user_id:
            logger.warning("提供的UserInfo不完整，无法查找或创建Profile/Entity。")
            return None, None

        entities_collection = await self._get_collection(CoreDBCollections.ENTITIES)
        entity_uid = f"{platform}_{user_info.user_id}"

        # 1. 先找客观实体
        entity_doc = await entities_collection.get(entity_uid)

        if entity_doc:
            # 找到了客观实体，现在反向查找它被哪个主观侧写所'表征'
            logger.debug(f"找到了已存在的客观实体: {entity_uid}")

            # 更新一下实体昵称
            if (
                user_info.user_nickname
                and entity_doc.get("last_known_nickname") != user_info.user_nickname
            ):
                await entities_collection.update(
                    {"_key": entity_uid, "last_known_nickname": user_info.user_nickname}
                )

            # AQL图遍历查询，从实体节点出发，反向查找表征它的“侧写”
            query = """
                FOR p IN 1..1 INBOUND @entity_id @@represents_edge_coll
                    RETURN { profile_id: p._key }
            """
            bind_vars = {
                "entity_id": f"{CoreDBCollections.ENTITIES}/{entity_uid}",
                "@represents_edge_coll": CoreDBCollections.REPRESENTS,
            }
            profile_results = await self.conn_manager.execute_query(query, bind_vars)

            if profile_results and (profile_id := profile_results[0].get("profile_id")):
                logger.debug(f"实体 {entity_uid} 已被 Profile '{profile_id}' 所表征。")
                return profile_id, entity_uid

            # 数据不一致的警告，为现有实体创建一个新的侧写并关联
            logger.warning(
                f"数据不一致！实体 {entity_uid} 存在但没有关联的Profile。将为其创建新的Profile。"
            )
            return await self._create_profile_for_existing_entity(entity_doc)
        else:
            # 没找到实体，创建新的侧写和实体，并关联它们
            logger.debug(f"未找到实体: {entity_uid}，将创建新的 Profile 和 Entity。")
            return await self._create_new_profile_with_entity(user_info, platform)

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

            LET profile_result = (
                INSERT profile_doc IN @@profiles_coll
                RETURN NEW
            )[0]

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
            if results and isinstance(results, list) and len(results) > 0:
                result = results[0]
                profile_id = result.get("profile_id")
                returned_entity_uid = result.get("entity_uid")
                if profile_id and returned_entity_uid:
                    logger.info(
                        f"AQL事务成功：为现有实体 '{returned_entity_uid}' "
                        f"创建并关联了新的 Profile '{profile_id}'。"
                    )
                    return profile_id, returned_entity_uid

            logger.error(
                f"为现有实体创建Profile的AQL事务执行后未能返回有效的ID, 返回结果: {results}"
            )
            return None, None

        except Exception as e:
            logger.error(f"为现有实体创建Profile的AQL事务执行失败: {e}", exc_info=True)
            return None, None

    async def _create_new_profile_with_entity(
        self,
        user_info: ProtocolUserInfo,
        platform: str,
        is_self: bool = False,
    ) -> tuple[str | None, str | None]:
        """内部工具：创建一个新的Profile，一个新的Entity，并用'represents'边连接."""
        profile = (
            EntityProfileDocument.create_new()
            if not is_self
            else EntityProfileDocument(_key=SELF_PROFILE_ID, profile_id=SELF_PROFILE_ID)
        )
        entity = EntityDocument.from_user_info(user_info, platform)

        query = """
            LET profile_doc = @profile_doc
            LET entity_doc = @entity_doc
            LET timestamp = @timestamp

            LET profile_result = (
                UPSERT { _key: profile_doc._key } INSERT profile_doc UPDATE {} IN @@profiles_coll RETURN NEW
            )[0]

            LET entity_result = (
                UPSERT { _key: entity_doc._key } INSERT entity_doc UPDATE {} IN @@entities_coll RETURN NEW
            )[0]

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
            if results and isinstance(results, list) and len(results) > 0:
                result = results[0]
                profile_id = result.get("profile_id")
                entity_uid = result.get("entity_uid")
                if profile_id and entity_uid:
                    logger.info(
                        f"AQL事务成功：创建/关联了 Profile '{profile_id}' "
                        f"和 Entity '{entity_uid}'。"
                    )
                    return profile_id, entity_uid

            logger.error(f"AQL事务执行后未能返回有效的ID, 返回结果: {results}")
            return None, None

        except Exception as e:
            logger.error(f"创建 Profile 和 Entity 的AQL事务执行失败: {e}", exc_info=True)
            return None, None

    async def update_robot_presence_in_conversation(
        self,
        entity_uid: str,
        conversation_id: str,
        platform: str,
        conversation_name: str | None,
        card_name: str | None,
        role: str | None,
    ) -> bool:
        """专门更新祂在某个群里的存在信息（主要是群名片）."""
        from_vertex = f"{CoreDBCollections.ENTITIES}/{entity_uid}"
        to_vertex = f"{CoreDBCollections.CONVERSATIONS}/{conversation_id}"
        edge_key = f"{entity_uid}_in_{conversation_id}"

        # 确保会话文档存在
        conv_collection = await self._get_collection(CoreDBCollections.CONVERSATIONS)
        if not await conv_collection.has(conversation_id):
            await conv_collection.insert(
                {
                    "_key": conversation_id,
                    "conversation_id": conversation_id,
                    "platform": platform,
                    "name": conversation_name,
                    "type": "group",
                    "created_at": int(time.time() * 1000),
                    "updated_at": int(time.time() * 1000),
                }
            )
            logger.info(f"发现未知会话 '{conversation_id}'，已为其创建档案。")

        props = MembershipProperties(
            group_name=conversation_name,
            cardname=card_name,
            permission_level=role,
            last_active_timestamp=int(time.time() * 1000),
        )

        edge_doc = {"_key": edge_key, "_from": from_vertex, "_to": to_vertex, **props.to_dict()}

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
            "@collection": CoreDBCollections.IS_PRESENT_IN,  # 假设关系名为 is_present_in
        }

        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.debug(
                f"成功更新祂的存在关系: Entity '{entity_uid}' in Conversation '{conversation_id}'"
            )
            return True
        except Exception as e:
            logger.error(f"更新祂的存在关系时失败: {e}", exc_info=True)
            return False

    async def update_presence_in_conversation(
        self,
        entity_uid: str,
        conversation_id: str,
        user_info: ProtocolUserInfo,
        conversation_name: str | None,
    ) -> None:
        """更新实体在会话中的存在信息（边属性）."""
        await self._get_collection(CoreDBCollections.IS_PRESENT_IN, is_edge=True)
        from_vertex = f"{CoreDBCollections.ENTITIES}/{entity_uid}"
        to_vertex = f"{CoreDBCollections.CONVERSATIONS}/{conversation_id}"
        edge_key = f"{entity_uid}_in_{conversation_id}"

        props = MembershipProperties(
            group_name=conversation_name,
            cardname=user_info.user_cardname,
            permission_level=user_info.permission_level,
            title=user_info.user_titlename,
            last_active_timestamp=int(time.time() * 1000),
        )

        edge_doc = {"_key": edge_key, "_from": from_vertex, "_to": to_vertex, **props.to_dict()}

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
            "@collection": CoreDBCollections.IS_PRESENT_IN,
        }

        try:
            await self.conn_manager.execute_query(query, bind_vars)
            logger.debug(
                f"成功更新存在关系: Entity '{entity_uid}' in Conversation '{conversation_id}'"
            )
        except Exception as e:
            logger.error(f"更新存在关系时失败: {e}", exc_info=True)

    async def get_profile_details_by_entity(
        self, platform: str, platform_id: str
    ) -> dict[str, Any] | None:
        """根据平台和平台ID，获取这个“侧写”的完整信息，包括其表征的所有实体."""
        entity_uid = f"{platform}_{platform_id}"

        query = """
            LET entity = DOCUMENT(@@entities_coll, @entity_uid)
            FILTER entity != null

            // 找到这个实体被哪个侧写所表征
            LET profile = (
                FOR p IN 1..1 INBOUND entity @@represents_coll
                    RETURN p
            )[0]
            FILTER profile != null

            // 找到这个侧写表征的所有实体
            LET all_entities = (
                FOR e IN 1..1 OUTBOUND profile @@represents_coll
                    RETURN e
            )

            // 找到这些实体在所有会话中的存在信息
            LET all_presences = (
                FOR e IN all_entities
                    FOR conv, edge IN 1..1 OUTBOUND e @@is_present_in_coll
                        RETURN {
                            presence_id: edge._key,
                            entity_uid: e.entity_uid,
                            group_id: conv.conversation_id,
                            platform: conv.platform,
                            group_name: edge.group_name,
                            cardname: edge.cardname,
                            permission_level: edge.permission_level
                        }
            )

            RETURN {
                profile_id: profile.profile_id,
                profile_data: profile.profile,
                entities: all_entities,
                presences: all_presences,
                metadata: {
                    created_at: profile.created_at,
                    updated_at: profile.updated_at
                }
            }
        """
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
                RETURN {
                    platform: entity.platform,
                    platform_id: entity.platform_id,
                    nickname: entity.nickname
                }
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

    async def get_self_entity_for_platform(self, platform_id: str) -> dict[str, Any] | None:
        """[核心重构] 根据平台ID，获取祂自身在该平台上的客观实体(Entity)信息."""
        if not platform_id:
            return None

        query = """
            LET self_profile = DOCUMENT(@@profiles_coll, @self_profile_key)
            FILTER self_profile != null
            FOR entity IN 1..1 OUTBOUND self_profile @@represents_coll
                FILTER entity.platform == @platform_id
                LIMIT 1
                RETURN {
                    platform: entity.platform,
                    platform_id: entity.platform_id,
                    nickname: entity.nickname,
                    entity_uid: entity.entity_uid
                }
        """
        bind_vars = {
            "@profiles_coll": CoreDBCollections.ENTITY_PROFILES,
            "self_profile_key": SELF_PROFILE_ID,
            "@represents_coll": CoreDBCollections.REPRESENTS,
            "platform_id": platform_id,
        }
        try:
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results and isinstance(results, list) and len(results) > 0:
                logger.debug(f"成功为平台 '{platform_id}' 获取到祂自身客观实体信息。")
                return results[0]
            logger.warning(f"未能为平台 '{platform_id}' 找到祂自身客观实体信息。")
            return None
        except Exception as e:
            logger.error(f"为平台 '{platform_id}' 获取自身客观实体信息时失败: {e}", exc_info=True)
            return None

    def _initialize_self_profile_from_persona(self) -> None:
        """[未来接口] 基于<persona>配置，通过LLM调用来填充 SELF_PROFILE_ID 的主观侧写."""
        # TODO: Implement the logic to populate the self profile based on persona settings.
        # This might involve:
        # 1. Reading a persona configuration file.
        # 2. Making a call to an LLM to generate descriptive profile data.
        # 3. Updating the SELF_PROFILE_ID document in the ENTITY_PROFILES collection.
        logger.info("初始化自身 Profile 的功能将在未来实现。")
        pass
