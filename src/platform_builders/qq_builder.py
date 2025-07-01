# 这是QQ平台的翻译官，武装到牙齿的最终版！
import uuid
from typing import Any, Dict, List, Optional

from aicarus_protocols import Event, Seg, SegBuilder
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class QQBuilder(BasePlatformBuilder):
    """
    专门伺候 Napcat QQ 这个平台的翻译官。
    它知道所有 QQ 平台能干的“脏活累活”，并能把核心的“通用指令”翻译成它们。
    """

    @property
    def platform_id(self) -> str:
        return "napcat_qq"

    def build_action_event(self, intent_data: Dict[str, Any]) -> Optional[Event]:
        """
        把通用指令翻译成 QQ 平台的具体动作事件。
        """
        # 从 LLM 的决策里拿出动作类型和参数
        # 比如 tool_name_chosen = "platform.napcat_adapter_default_instance.send_message"
        # 那么 action_type 就是 "send_message"
        full_action_name = intent_data.get("full_action_name", "")
        action_type = full_action_name.split('.')[-1]

        params = intent_data.get("params", {})

        # --- 根据动作类型，调用不同的“翻译”方法 ---
        action_builders = {
            "send_message": self._build_send_message,
            "recall_message": self._build_recall_message,
            "poke_user": self._build_poke_user,
            "handle_friend_request": self._build_handle_friend_request,
            "handle_group_request": self._build_handle_group_request,
            "get_group_info": self._build_get_group_info,
            "get_bot_profile": self._build_get_bot_profile,
        }

        builder_func = action_builders.get(action_type)
        if builder_func:
            return builder_func(params)

        logger.warning(f"QQBuilder 还不知道怎么翻译这个动作: {action_type}")
        return None

    # --- 下面是每个动作的具体“翻译”实现 ---

    def _build_send_message(self, params: Dict[str, Any]) -> Optional[Event]:
        conversation_id = params.get("conversation_id")
        content_list = params.get("content") # LLM现在应该返回一个消息段列表

        if not conversation_id or not content_list:
            logger.error("构建发送消息事件失败：缺少 conversation_id 或 content。")
            return None

        # 把通用消息段列表转换成 aicarus_protocols.Seg 对象列表
        # 这一步是关键，假设LLM返回的 content 是 [{'type': 'text', 'text': '你好'}] 这样的结构
        try:
            message_segs = [Seg(type=seg.get("type"), data=seg.get("data", {})) for seg in content_list]
        except Exception:
            logger.error("构建发送消息事件失败：content 格式不正确。")
            return None

        # 你的适配器 send_handler_aicarus.py 里会根据 conversation_info.type 判断是群聊还是私聊
        # 所以我们这里也需要这个信息。LLM 应该提供这个，或者我们根据ID猜。
        # 这里我们假设 LLM 会提供 conversation_type
        conv_type = params.get("conversation_type", "group") # 默认为群聊

        return Event(
            event_id=str(uuid.uuid4()),
            event_type="action.message.send", # 适配器那边认的是这个
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=message_segs,
            conversation_info={
                "conversation_id": conversation_id,
                "type": conv_type,
            }
        )

    def _build_recall_message(self, params: Dict[str, Any]) -> Optional[Event]:
        target_message_id = params.get("target_message_id")
        if not target_message_id:
            return None
        
        # 你的 action_definitions.py -> RecallMessageHandler 需要的是 'action.message.recall'
        # 并且参数在第一个Seg的data里
        recall_seg = Seg(type="action.message.recall", data={"target_message_id": target_message_id})
        
        return Event(
            event_id=str(uuid.uuid4()),
            event_type="action.message.recall",
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=[recall_seg]
        )

    def _build_poke_user(self, params: Dict[str, Any]) -> Optional[Event]:
        user_id = params.get("user_id")
        conversation_id = params.get("conversation_id") # QQ的戳一戳是在群里戳，所以需要群号
        if not user_id or not conversation_id:
            return None

        # 你的 action_definitions.py -> PokeUserHandler 需要 'action.user.poke'
        poke_seg = Seg(type="action.user.poke", data={"target_user_id": user_id, "target_group_id": conversation_id})

        return Event(
            event_id=str(uuid.uuid4()),
            event_type="action.user.poke",
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=[poke_seg]
        )

    def _build_handle_friend_request(self, params: Dict[str, Any]) -> Optional[Event]:
        request_flag = params.get("request_flag")
        approve = params.get("approve", False)
        remark = params.get("remark")
        if not request_flag:
            return None
            
        action_type = "action.request.friend.approve" if approve else "action.request.friend.reject"
        req_seg = Seg(type=action_type, data={"request_flag": request_flag, "remark": remark})

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=action_type,
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=[req_seg]
        )
        
    def _build_handle_group_request(self, params: Dict[str, Any]) -> Optional[Event]:
        request_flag = params.get("request_flag")
        approve = params.get("approve", False)
        reason = params.get("reason")
        # 你的适配器需要这个来区分是“加群申请”还是“被邀请”
        original_request_sub_type = params.get("original_request_sub_type") # "join_application" or "invite_received"
        
        if not request_flag or not original_request_sub_type:
            return None
            
        action_type = "action.request.conversation.approve" if approve else "action.request.conversation.reject"
        req_seg = Seg(type=action_type, data={
            "request_flag": request_flag, 
            "reason": reason,
            "original_request_sub_type": original_request_sub_type
        })
        
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=action_type,
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=[req_seg]
        )

    def _build_get_group_info(self, params: Dict[str, Any]) -> Optional[Event]:
        group_id = params.get("group_id")
        if not group_id:
            return None

        # 这个动作需要一个 conversation_info 来传递群号
        return Event(
            event_id=str(uuid.uuid4()),
            event_type="action.conversation.get_info",
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=[], # 这个动作不需要内容
            conversation_info={
                "conversation_id": group_id,
                "type": "group"
            }
        )

    # --- 在文件末尾，加上这个新的翻译方法！ ---
    def _build_get_bot_profile(self, params: Dict[str, Any]) -> Optional[Event]:
        """
        翻译“获取机器人档案”这个指令。
        """
        group_id = params.get("group_id")

        action_seg = Seg(
            type="action.bot.get_profile",
            data={"group_id": group_id} if group_id else {}
        )

        return Event(
            event_id=str(uuid.uuid4()),
            event_type="action.bot.get_profile",
            platform=self.platform_id,
            bot_id=config.persona.bot_name,
            content=[action_seg]
        )

    def get_action_schema_for_llm(self) -> List[Dict[str, Any]]:
        # 这就是给LLM看的“说明书”，告诉它QQ平台有哪些功能可以用
        # 名字必须是 `platform.{self.platform_id}.{action_name}` 格式
        prefix = f"platform.{self.platform_id}"

        # 先定义一个列表，把所有功能的说明书都放进去
        all_schemas = [
            {
                "type": "function",
                "function": {
                    "name": f"{prefix}.send_message",
                    "description": "向指定的QQ群或好友发送一条或多条消息。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "conversation_id": {"type": "string", "description": "目标群号或QQ号。"},
                            "conversation_type": {"type": "string", "description": "会话类型，'group' 或 'private'。"},
                            "content": {
                                "type": "array",
                                "description": "一个消息段(Segment)列表，定义了要发送的内容。比如 `[{'type': 'text', 'data': {'text': '你好'}}]`。",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "description": "消息段类型，如 'text', 'at', 'image'。"},
                                        "data": {"type": "object", "description": "该消息段的具体数据。"}
                                    }
                                }
                            }
                        },
                        "required": ["conversation_id", "conversation_type", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": f"{prefix}.poke_user",
                    "description": "在指定的QQ群里戳一个成员。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                             "conversation_id": {"type": "string", "description": "目标群号。"},
                             "user_id": {"type": "string", "description": "要戳的用户的QQ号。"}
                        },
                        "required": ["conversation_id", "user_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": f"{prefix}.recall_message",
                    "description": "撤回一条已发送的消息。",
                    "parameters": {
                        "type": "object",
                        "properties": { "target_message_id": {"type": "string", "description": "要撤回的消息的ID。"} },
                        "required": ["target_message_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": f"{prefix}.get_bot_profile",
                    "description": "获取机器人自身在一个或所有群聊中的档案信息，如群名片、角色等。这是一个系统级检查动作。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group_id": {
                                "type": "string",
                                "description": "（可选）如果提供，则只获取指定群聊中的档案。如果不提供，则获取所有群聊的档案。"
                            }
                        }
                    }
                }
            }
            # 如果未来还有其他动作，就照着上面的格式，在这里加一个逗号 , 然后再加一个新的字典 {}
        ]
        
        # 最后，把这个装满了说明书的列表整个返回出去！
        return all_schemas
