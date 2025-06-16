# src/tools/platform_actions.py
import time
import uuid

from aicarus_protocols import (
    ConversationInfo as ProtocolConversationInfo,
)
from aicarus_protocols import (
    ConversationType,
    Seg,
    SegBuilder,
)
from aicarus_protocols import (
    Event as ProtocolEvent,
)

from src.common.custom_logging.logger_manager import get_logger
from src.core_communication.core_ws_server import CoreWebsocketServer

logger = get_logger("AIcarusCore.tools.platform_actions")


async def send_reply_message(
    comm_layer: CoreWebsocketServer,
    message_content_text: str,
    target_adapter_id: str,  # 主人请看，这里加了一个参数，指定要调教哪个小可爱！
    target_user_id: str | None = None,
    target_group_id: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict:
    """
    构造并发送一条回复消息给特定的适配器。现在更精准了哦～

    Args:
        comm_layer: CoreWebsocketServer 的实例，用于发送事件。
        message_content_text: 要发送的纯文本消息内容。
        target_adapter_id: 目标适配器的ID，我们要把消息塞给它！
        target_user_id: 目标用户的ID (私聊)。
        target_group_id: 目标群组的ID (群聊)。
        reply_to_message_id: (可选) 如果是回复特定消息，提供其ID。

    Returns:
        一个包含执行结果的字典。
    """
    if not comm_layer:
        error_msg = "核心通信层未初始化，无法发送回复消息。"
        logger.error(error_msg)
        return {"status": "failure", "reason": error_msg}

    if not target_adapter_id:  # 必须告诉我要调教谁！不然人家的棒棒往哪里塞嘛～
        error_msg = "发送回复消息时，必须提供 target_adapter_id。"
        logger.error(error_msg)
        return {"status": "failure", "reason": error_msg}

    if not target_user_id and not target_group_id:  # 当然，也要告诉我具体要对哪个小穴或者哪个群穴下手～
        error_msg = "发送回复消息时，必须提供 target_user_id 或 target_group_id。"
        logger.error(error_msg)
        return {"status": "failure", "reason": error_msg}

    try:
        # 1. 构造消息内容 Segments
        content_segs: list[Seg] = []
        if reply_to_message_id:
            content_segs.append(SegBuilder.reply(message_id=reply_to_message_id))
        content_segs.append(SegBuilder.text(message_content_text))

        # 2. 构造目标会话信息
        action_conv_info: ProtocolConversationInfo
        if target_group_id:
            action_conv_info = ProtocolConversationInfo(
                conversation_id=str(target_group_id), type=ConversationType.GROUP
            )
        else:  # 私聊
            action_conv_info = ProtocolConversationInfo(
                conversation_id=str(target_user_id), type=ConversationType.PRIVATE
            )

        # 3. 构造动作事件
        # 注意: platform 和 bot_id 应从配置中获取，这里暂时使用占位符
        action_event = ProtocolEvent(
            event_id=f"action_send_reply_{uuid.uuid4()}",
            event_type="action.message.send",
            time=int(time.time() * 1000.0),
            platform="default_platform",  # 应从配置中获取
            bot_id="default_bot",  # 应从配置中获取
            conversation_info=action_conv_info,
            content=content_segs,
        )

        # 4. 发送动作 (精准投喂！不再是广播了哦～ 用我们新的棒棒！)
        send_success = await comm_layer.send_action_to_adapter_by_id(target_adapter_id, action_event)

        if send_success:
            success_msg = f"向适配器 '{target_adapter_id}' 发送消息指令已成功发出。目标用户/群组: {target_group_id or target_user_id}"
            logger.info(success_msg)
            return {"status": "success", "message": success_msg}
        else:
            # 如果没成功，就要告诉主人哦
            error_msg = f"向适配器 '{target_adapter_id}' 发送消息指令失败。它可能不在线，或者ID给错了哦～"
            logger.error(error_msg)
            return {"status": "failure", "reason": error_msg}

    except Exception as e:
        error_msg = f"发送消息时发生错误: {e}"
        logger.error(error_msg, exc_info=True)
        return {"status": "failure", "reason": error_msg}
