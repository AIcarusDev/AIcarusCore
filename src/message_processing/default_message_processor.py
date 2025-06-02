# src/message_processing/default_message_processor.py
import datetime
import uuid
from typing import Any

from aicarus_protocols import MessageBase, Seg

# StandardDatabase is no longer directly needed here as ArangoDBHandler encapsulates it.
# from arango.database import StandardDatabase
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import AlcarusRootConfig

# arangodb_handler module is imported to get the ArangoDBHandler class
from src.database.arangodb_handler import ArangoDBHandler  # Import the class

logger = get_logger("AIcarusCore.message_processor")


class DefaultMessageProcessor:
    def __init__(self, db_handler: ArangoDBHandler, root_config: AlcarusRootConfig) -> None:
        """
        初始化默认消息处理器。
        :param db_handler: ArangoDBHandler 实例，用于数据库交互。
        :param root_config: 全局配置信息。
        """
        self.db_handler = db_handler  # Store the ArangoDBHandler instance
        self.config = root_config
        self.bot_name = root_config.persona.bot_name
        logger.info("DefaultMessageProcessor 初始化完成。")

    def _generate_conversation_id(self, platform: str, group_id: str | None, user_id: str | None) -> str:
        """
        生成唯一的会话ID。
        """
        if group_id:
            return f"{platform}_{group_id}"
        elif user_id:
            participants = sorted([user_id, self.bot_name])
            return f"{platform}_dm_{participants[0]}_{participants[1]}"
        else:
            logger.warning("无法生成有效的 conversation_id：缺少 group_id 和 user_id。将生成随机ID。")
            return f"{platform}_unknown_{str(uuid.uuid4())}"

    async def process_message(self, message: MessageBase, websocket: WebSocketServerProtocol) -> None:
        """
        处理从适配器收到的单个 AIcarusMessageBase 消息。
        """
        if message.message_info.interaction_purpose == "platform_meta":
            logger.debug("跳过平台心跳包消息，不进行存储。")
            return

        logger.info(
            f"消息处理器收到来自适配器 ({websocket.remote_address}) 的消息，类型: {message.message_info.interaction_purpose}"
        )
        logger.debug(f"完整收到的 AicarusMessageBase: {message.to_dict()}")

        if not self.db_handler:  # Check for the ArangoDBHandler instance
            logger.error("数据库处理器 (ArangoDBHandler) 未在消息处理器中初始化，无法存储消息。")
            return

        platform_id_str = message.message_info.platform or "unknown_platform"
        bot_id_str = message.message_info.bot_id or self.bot_name

        sender_user_id_internal_ref: str | None = None
        original_sender_platform_id: str | None = None
        if message.message_info.user_info and message.message_info.user_info.user_id:
            original_sender_platform_id = message.message_info.user_info.user_id
            sender_user_id_internal_ref = f"Users/{platform_id_str}_{original_sender_platform_id}"

        original_group_platform_id: str | None = None
        group_id_internal_ref: str | None = None
        if message.message_info.group_info and message.message_info.group_info.group_id:
            original_group_platform_id = message.message_info.group_info.group_id
            group_id_internal_ref = f"Groups/{platform_id_str}_{original_group_platform_id}"

        conversation_id = self._generate_conversation_id(
            platform_id_str,
            original_group_platform_id,
            original_sender_platform_id,
        )

        content_segments_to_save = []
        if message.message_segment:
            if message.message_segment.type == "seglist" and isinstance(message.message_segment.data, list):
                for seg_obj_data in message.message_segment.data:
                    try:
                        seg = Seg.from_dict(seg_obj_data if isinstance(seg_obj_data, dict) else seg_obj_data.to_dict())
                        content_segments_to_save.append(seg.to_dict())
                    except Exception as e_seg:
                        logger.warning(f"无法转换消息段: {seg_obj_data}, 错误: {e_seg}")
                        content_segments_to_save.append({"type": "error", "data": f"原始段数据: {str(seg_obj_data)}"})
            else:
                try:
                    seg = Seg.from_dict(message.message_segment.to_dict())
                    content_segments_to_save.append(seg.to_dict())
                except Exception as e_seg_single:
                    logger.warning(f"无法转换单个消息段: {message.message_segment.to_dict()}, 错误: {e_seg_single}")
                    content_segments_to_save.append(
                        {"type": "error", "data": f"原始段数据: {str(message.message_segment.to_dict())}"}
                    )

        formatted_message_data: dict[str, Any] = {
            "platform_message_id": message.message_info.message_id,
            "conversation_id": conversation_id,
            "platform_id_ref": f"Platforms/{platform_id_str}",
            "group_id_ref": group_id_internal_ref,
            "sender_user_id_ref": sender_user_id_internal_ref,
            "bot_id_ref": None,
            "timestamp": datetime.datetime.fromtimestamp(message.message_info.time / 1000.0).isoformat() + "Z",
            "message_type": message.message_info.interaction_purpose,
            "content_segments": content_segments_to_save,
            "mentions_bot": True,  # TODO: Implement actual mention detection
            "is_direct_message_to_bot": not bool(original_group_platform_id),
            "raw_message_info_dump": message.message_info.to_dict(),
            "raw_message_segment_dump": message.message_segment.to_dict() if message.message_segment else None,
        }

        if message.message_info.bot_id and message.message_info.bot_id == bot_id_str:
            formatted_message_data["sender_user_id_ref"] = None
            formatted_message_data["bot_id_ref"] = f"Bots/{bot_id_str}"

        try:
            # Call the method on the ArangoDBHandler instance
            # The ArangoDBHandler.save_raw_chat_message method now only needs message_data
            saved_message_key = await self.db_handler.save_raw_chat_message(formatted_message_data)
            if saved_message_key:
                logger.info(f"消息 (Key: {saved_message_key}, 会话: {conversation_id}) 已由消息处理器成功存入数据库。")
            else:
                logger.error(f"消息处理器未能保存消息 (会话: {conversation_id}) 到数据库或获取有效的key。")
        except Exception as e_save:
            logger.error(f"消息处理器在调用 save_raw_chat_message 时发生错误: {e_save}", exc_info=True)

        if message.message_info.interaction_purpose == "platform_request":
            logger.info(f"平台请求类型消息已存储，可供后续思考决策。会话: {conversation_id}")
        elif message.message_info.interaction_purpose == "action_response":
            logger.info(f"动作响应类型消息已存储。会话: {conversation_id}")
