# src/message_processing/default_message_processor.py
import datetime
import uuid  # 用于生成 conversation_id 或 message_key (如果需要)
from typing import Any

from aicarus_protocols import MessageBase, Seg  # 从协议库导入
from arango.database import StandardDatabase  # type: ignore
from websockets.server import WebSocketServerProtocol  # type: ignore

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import AlcarusRootConfig  # 可能需要配置信息
from src.database import arangodb_handler  # 调用数据库存储函数

logger = get_logger("AIcarusCore.message_processor")  # 为消息处理器模块获取独立的logger


class DefaultMessageProcessor:
    def __init__(self, db_instance: StandardDatabase, root_config: AlcarusRootConfig) -> None:
        """
        初始化默认消息处理器。
        :param db_instance: ArangoDB 数据库实例。
        :param root_config: 全局配置信息，可能用于生成 conversation_id 或其他逻辑。
        """
        self.db = db_instance
        self.config = root_config
        self.bot_name = root_config.persona.bot_name  # 获取机器人名称，用于生成会话ID
        logger.info("DefaultMessageProcessor 初始化完成。")

    def _generate_conversation_id(self, platform: str, group_id: str | None, user_id: str | None) -> str:
        """
        生成唯一的会话ID。
        对于群聊，格式为: platform_groupid。
        对于私聊，格式为: platform_dm_sorted(user_id, bot_name)。
        确保私聊双方的会话ID一致。
        """
        if group_id:
            # 群聊会话ID
            return f"{platform}_{group_id}"
        elif user_id:
            # 私聊会话ID，对参与者ID（用户和机器人名）进行排序以保证唯一性
            # 假设机器人的唯一标识是其名称，如果机器人有更稳定的ID，应使用那个ID
            participants = sorted([user_id, self.bot_name])
            return f"{platform}_dm_{participants[0]}_{participants[1]}"
        else:
            # 异常情况：无法确定会话方，生成一个随机ID以避免冲突，并记录警告
            logger.warning("无法生成有效的 conversation_id：缺少 group_id 和 user_id。将生成随机ID。")
            return f"{platform}_unknown_{str(uuid.uuid4())}"

    async def process_message(self, message: MessageBase, websocket: WebSocketServerProtocol) -> None:
        """
        处理从适配器收到的单个 AIcarusMessageBase 消息。
        主要职责是解析消息，将其格式化为扁平结构，并调用数据库处理器进行存储。
        """
        if message.message_info.interaction_purpose == "platform_meta":
            logger.debug("跳过平台心跳包消息，不进行存储。")
            return

        logger.info(
            f"消息处理器收到来自适配器 ({websocket.remote_address}) 的消息，类型: {message.message_info.interaction_purpose}"
        )
        logger.debug(f"完整收到的 AicarusMessageBase: {message.to_dict()}")

        if not self.db:
            logger.error("数据库实例未在消息处理器中初始化，无法存储消息。")
            return

        # 1. 提取和转换消息数据为扁平化结构
        platform_id_str = message.message_info.platform or "unknown_platform"
        # 机器人ID，如果消息是机器人自己发送的，或者在私聊中用于构成 conversation_id
        bot_id_str = message.message_info.bot_id or self.bot_name

        sender_user_id_internal_ref: str | None = None  # 指向 Users 集合的引用
        original_sender_platform_id: str | None = None  # 原始平台用户ID
        if message.message_info.user_info and message.message_info.user_info.user_id:
            original_sender_platform_id = message.message_info.user_info.user_id
            # 内部引用格式，例如 "Users/slack_U123XYZ"
            # 假设 Users 集合的 _key 是 platform_originaluserid
            sender_user_id_internal_ref = f"Users/{platform_id_str}_{original_sender_platform_id}"

        original_group_platform_id: str | None = None  # 原始平台群组ID
        group_id_internal_ref: str | None = None  # 指向 Groups 集合的引用
        if message.message_info.group_info and message.message_info.group_info.group_id:
            original_group_platform_id = message.message_info.group_info.group_id
            # 内部引用格式，例如 "Groups/slack_CABCDEFG"
            group_id_internal_ref = f"Groups/{platform_id_str}_{original_group_platform_id}"

        # 生成会话ID
        conversation_id = self._generate_conversation_id(
            platform_id_str,
            original_group_platform_id,  # 使用原始群组ID生成
            original_sender_platform_id,  # 使用原始用户ID生成
        )

        # 处理消息段
        content_segments_to_save = []
        if message.message_segment:
            if message.message_segment.type == "seglist" and isinstance(message.message_segment.data, list):
                for seg_obj_data in message.message_segment.data:
                    try:
                        # 确保每个段都是字典格式，符合Seg.from_dict的期望
                        seg = Seg.from_dict(seg_obj_data if isinstance(seg_obj_data, dict) else seg_obj_data.to_dict())
                        content_segments_to_save.append(seg.to_dict())
                    except Exception as e_seg:
                        logger.warning(f"无法转换消息段: {seg_obj_data}, 错误: {e_seg}")
                        content_segments_to_save.append({"type": "error", "data": f"原始段数据: {str(seg_obj_data)}"})
            else:  # 单个 segment
                try:
                    seg = Seg.from_dict(message.message_segment.to_dict())  # 确保是标准格式
                    content_segments_to_save.append(seg.to_dict())
                except Exception as e_seg_single:
                    logger.warning(f"无法转换单个消息段: {message.message_segment.to_dict()}, 错误: {e_seg_single}")
                    content_segments_to_save.append(
                        {"type": "error", "data": f"原始段数据: {str(message.message_segment.to_dict())}"}
                    )

        # 准备存储到数据库的消息文档
        formatted_message_data: dict[str, Any] = {
            # "_key" 将由 arangodb_handler.save_raw_chat_message 处理
            "platform_message_id": message.message_info.message_id,  # 原始平台的消息ID
            "conversation_id": conversation_id,  # 计算得到的会话ID
            "platform_id_ref": f"Platforms/{platform_id_str}",  # 指向 Platforms 集合的引用
            "group_id_ref": group_id_internal_ref,  # 指向 Groups 集合的引用 (如果是群消息)
            "sender_user_id_ref": sender_user_id_internal_ref,  # 指向 Users 集合的引用 (如果是用户发送)
            "bot_id_ref": None,  # 默认为None，如果是机器人发送的消息再设置
            "timestamp": datetime.datetime.fromtimestamp(message.message_info.time / 1000.0).isoformat()
            + "Z",  # ISO 8601 UTC
            "message_type": message.message_info.interaction_purpose,  # 使用 interaction_purpose 作为消息类型
            "content_segments": content_segments_to_save,  # 结构化的消息内容
            "mentions_bot": True,  # TODO: 需要更准确的机器人提及检测逻辑
            "is_direct_message_to_bot": not bool(original_group_platform_id),  # 简单判断：如果没有群ID，则认为是私聊
            "raw_message_info_dump": message.message_info.to_dict(),  # 存储原始的 message_info 部分
            "raw_message_segment_dump": message.message_segment.to_dict()
            if message.message_segment
            else None,  # 存储原始的 message_segment
        }

        # 如果消息是机器人自己发送的 (需要适配器正确设置 interaction_purpose 或 bot_id)
        if message.message_info.bot_id and message.message_info.bot_id == bot_id_str:  # 检查是否是当前机器人的消息
            formatted_message_data["sender_user_id_ref"] = None  # 机器人发送的消息，发送者不是普通用户
            formatted_message_data["bot_id_ref"] = f"Bots/{bot_id_str}"  # 指向 Bots 集合的引用

        # 2. 调用数据库处理器保存消息
        try:
            saved_message_key = await arangodb_handler.save_raw_chat_message(
                self.db,
                formatted_message_data,  # 传递格式化后的数据
            )
            if saved_message_key:
                logger.info(f"消息 (Key: {saved_message_key}, 会话: {conversation_id}) 已由消息处理器成功存入数据库。")
            else:
                # save_raw_chat_message 内部如果因 key 冲突而已存在，也会返回 key，所以这里主要是针对其他保存失败的情况
                logger.error(f"消息处理器未能保存消息 (会话: {conversation_id}) 到数据库或获取有效的key。")
        except Exception as e_save:
            logger.error(f"消息处理器在调用 save_raw_chat_message 时发生错误: {e_save}", exc_info=True)

        # 对于特定类型的消息，例如平台请求或动作响应，后续的主思考循环可能会基于这些新存储的记录来决策。
        # 此处主要完成消息的标准化存储。
        if message.message_info.interaction_purpose == "platform_request":
            logger.info(f"平台请求类型消息已存储，可供后续思考决策。会话: {conversation_id}")
        elif message.message_info.interaction_purpose == "action_response":
            logger.info(f"动作响应类型消息已存储。会话: {conversation_id}")
            # 注意：对 Action 状态的更新逻辑目前仍在 core_logic.main.py 的 _core_thinking_loop 中处理
            # （当它发现 action_attempted 字段时）。未来也可以考虑将 ActionResponse 的处理也模块化。
