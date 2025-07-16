# src/platform_builders/qq_builder.py
import time
import uuid
from typing import Any

from aicarus_protocols import ConversationInfo, Event, Seg
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class QQBuilder(BasePlatformBuilder):
    """QQ平台的构建器，负责将平台特有的动作翻译成标准Event格式.

    这个构建器是为QQ平台定制的，提供了所有QQ特有动作的翻译方法。

    Attributes:
        platform_id (str): 平台ID，唯一标识一个平台。
        这个ID必须和Adapter的core_platform_id完全一致，以确保适配器能够正确识别。
    """

    def __init__(self) -> None:
        super().__init__()
        # 把所有可以用通用模板处理的动作都放在这里。
        # 这样可以避免每次都要写一大堆 if-else 来判断动作类型。
        # 这些动作都可以直接用通用模板来处理，不需要特殊逻辑。
        self._generic_actions = {
            "recall_message",
            "poke_user",
            "send_message",
            "kick_member",
            "ban_member",
            "ban_all_members",
            "set_member_card",
            "set_member_title",
            "leave_conversation",
            "handle_friend_request",
            "handle_group_request",
            "get_group_info",
            "get_bot_profile",
            "sign_in",
            "set_status",
            "set_avatar",
            "get_history",
            "get_list",
            "forward_single_message",
            "set_admin",
            "set_conversation_name",
            "get_group_files",
            "upload_group_file",
            "delete_group_item",
            "create_group_folder",
            "get_group_file_url",
            "get_group_honor_info",
            "send_group_notice",
            "get_group_notice",
            "set_message_emoji_like",
            "get_recent_contacts",
            "get_ai_characters",
            "send_ai_voice",
        }

    @property
    def platform_id(self) -> str:
        """返回平台ID，唯一标识一个平台，这个ID必须和Adapter的core_platform_id完全一致."""
        return "napcat_qq"

    def build_action_event(self, action_name: str, params: dict[str, Any]) -> Event | None:
        """这个方法负责将平台特有的动作转换成标准的Event格式，它会根据动作名称和参数来决定如何构建Event对象.

        Args:
            action_name (str): 动作名称，标识要执行的具体动作.
            params (dict[str, Any]): 动作参数，包含执行这个动作所需的所有信息.

        Returns:
            Event | None: 返回一个Event对象，表示这个动作的执行结果.
            如果这个动作不在白名单或特殊处理列表中，则返回 None.

        这个方法的核心逻辑是：
        1. 先处理那些需要特殊处理的动作，比如发消息和合并转发.
        2. 然后，检查这个动作是不是在白名单里.
            如果在，就用通用模板来处理，并返回一个Event对象.
        3. 如果哪个都不沾，那就真的不认识了，返回 None.
        """
        # 1. 先处理那些需要特殊处理的动作
        if action_name == "send_message":
            return self._build_send_message(params)
        if action_name == "send_forward_message":
            return self._build_send_forward_message(params)

        # 2. 然后，检查这个动作是不是在白名单里
        if action_name in self._generic_actions:
            # 如果在白名单里，就用通用模板来处理
            # 有些动作需要特别关照一下，把 conversation_info 塞进去
            conv_info = None
            if action_name in ["get_group_info", "get_history", "forward_single_message"]:
                conv_id = params.get("group_id") or params.get("conversation_id")
                conv_info_dict = params.get("conversation_info")
                if conv_info_dict:
                    conv_info = ConversationInfo.from_dict(conv_info_dict)
                elif conv_id:
                    # 这是一个简化处理，实际中最好从params里拿到完整的conv_info
                    conv_info = ConversationInfo(conversation_id=str(conv_id), type="group")
            elif action_name == "poke_user":
                conv_id = params.get("target_group_id")
                if conv_id:
                    conv_info = ConversationInfo(conversation_id=str(conv_id), type="group")

            return self._build_generic_event(action_name, params, conv_info)

        # 3. 如果哪个都不沾，那就真的不认识了
        logger.warning(f"QQBuilder 的白名单和特殊名单里都没有这个动作: {action_name}")
        return None

    # --- 下面是每个动作的具体“翻译”实现 ---

    def _build_generic_event(
        self, action_name: str, params: dict[str, Any], conv_info: ConversationInfo | None = None
    ) -> Event:
        """一个通用的翻译模板，这个方法会根据动作名称和参数来构建一个标准的Event对象.

        Args:
            action_name (str): 动作名称，标识要执行的具体动作.
            params (dict[str, Any]): 动作参数，包含执行这个动作所需的所有信息.
            conv_info (ConversationInfo | None): 可选的会话信息，如果有的话.

        Returns:
            Event: 返回一个Event对象，表示这个动作的执行结果.
        """
        final_event_type = f"action.{self.platform_id}.{action_name}"
        action_seg = Seg(type="action_params", data=params)
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=[action_seg],
            conversation_info=conv_info,
        )

    def _build_send_message(self, params: dict[str, Any]) -> Event | None:
        """这个发消息的比较特殊，内容是消息段列表，而不是 action_params.

        Args:
            params (dict[str, Any]): 包含发送消息所需的所有参数.

        Returns:
            Event | None: 返回一个Event对象，表示发送消息的动作.
        """
        conversation_id = params.get("conversation_id")
        content_segs_data = params.get("content", [])

        if not conversation_id or not isinstance(content_segs_data, list):
            logger.warning("发消息缺少 conversation_id 或 content。")
            return None

        # 它的 content 就是消息本身，而不是 action_params
        message_segs = [
            Seg(type=seg.get("type"), data=seg.get("data", {})) for seg in content_segs_data
        ]
        conv_type = params.get("conversation_type", "group")

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=f"action.{self.platform_id}.send_message",
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=message_segs,
            conversation_info=ConversationInfo(
                conversation_id=str(conversation_id), type=conv_type
            ),
        )

    def _build_send_forward_message(self, params: dict[str, Any]) -> Event | None:
        """这个合并转发消息的处理也比较特殊，它需要一个节点列表和会话信息.

        Args:
            params (dict[str, Any]): 包含转发消息所需的所有参数.

        Returns:
            Event | None: 返回一个Event对象，表示转发消息的动作.
        """
        nodes = params.get("nodes", [])
        conv_info_dict = params.get("conversation_info", {})
        if not nodes or not conv_info_dict:
            return None

        node_segs = [Seg(type="node", data=node_data) for node_data in nodes]

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=f"action.{self.platform_id}.send_forward_message",
            time=int(time.time() * 1000),
            bot_id=config.persona.qq_id or "unknown_bot",
            content=node_segs,
            conversation_info=ConversationInfo.from_dict(conv_info_dict),
        )


    def get_level_consciousness_controls_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """QQ平台不提供任何意识控制，由CoreBuilder统一管理。"""
        return {}, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """QQ平台不提供任何意识控制的描述。"""
        return "", ""

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据层级，提供QQ平台专属动作的JSON Schema。"""
        props = {}
        if level == "platform":
            props["get_list"] = {
                "type": "object",
                "properties": {
                    "list_type": {"type": "string", "enum": ["friend", "group"]},
                    "motivation": {"type": "string"}
                },
                "required": ["list_type", "motivation"]
            }
        elif level == "cellular":
            props["send_message"] = {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "enum": ["text", "at", "reply", "send_and_break"]},
                                "params": {"type": "object"}
                            },
                            "required": ["command"]
                        }
                    },
                    "motivation": {"type": "string"}
                },
                "required": ["steps", "motivation"]
            }
            props["poke_user"] = {
                "type": "object",
                "properties": {"target_user_id": {"type": "string"}, "motivation": {"type": "string"}},
                "required": ["target_user_id", "motivation"]
            }

        schema = {"type": "object", "properties": props} if props else {}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> tuple[str, str]:
        """根据层级，提供QQ平台专属动作的自然语言描述。"""
        descs = []
        if level == "platform":
            descs.append("- `get_list`: 获取本平台的好友或群聊列表。")
        elif level == "cellular":
            descs.append("- `send_message`: 在当前会话中发送消息。")
            descs.append("- `poke_user`: 在当前会话中戳一戳某人。")

        return "\n".join(descs) or "你当前没有可用的动作。"
