# AIcarusCore/src/sub_consciousness/chat_session_handler.py
import asyncio
import datetime
import collections
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from aicarus_protocols import MessageBase, Seg, UserInfo, GroupInfo, BaseMessageInfo
from src.config.alcarus_configs import PersonaSettings
from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.common.custom_logging.logger_manager import get_logger
from src.database.arangodb_handler import ArangoDBHandler

if TYPE_CHECKING:
    from src.core_logic.consciousness_loop import CoreLogic as CurrentCoreLogic # 修正导入路径

logger = get_logger("AIcarusCore.sub_consciousness.ChatSessionHandler")

class ChatSession:
    """
    代表一个独立的聊天会话（子思维）实例。
    负责管理特定会话的上下文、生成回复等。
    """
    def __init__(
        self,
        conversation_id: str,
        platform_id: str,
        bot_id: str,
        core_persona_settings: PersonaSettings,
        llm_client: LLMProcessorClient,
        db_handler: ArangoDBHandler,
        main_mind_trigger_event: asyncio.Event,
        initial_group_info: Optional[GroupInfo] = None,
        initial_user_info: Optional[UserInfo] = None,
    ):
        self.conversation_id: str = conversation_id
        self.platform_id: str = platform_id
        self.bot_id: str = bot_id
        self.core_persona_settings: PersonaSettings = core_persona_settings
        self.llm_client: LLMProcessorClient = llm_client
        self.db_handler: ArangoDBHandler = db_handler
        self.main_mind_trigger_event: asyncio.Event = main_mind_trigger_event

        self.is_active: bool = False
        self.last_interaction_time: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
        self.chat_context: collections.deque[Dict[str, Any]] = collections.deque(maxlen=20)

        self.last_reply_generated: Optional[str] = None
        self.last_reply_main_thought_context: Optional[str] = None
        self.last_reply_reasoning: Optional[str] = None
        self.last_reply_mood: Optional[str] = None

        self.group_info: Optional[GroupInfo] = initial_group_info
        self.user_info: Optional[UserInfo] = initial_user_info

        self.current_chat_style_directives: Optional[Dict[str, Any]] = None

        logger.info(f"ChatSession 实例已创建 (会话ID: {self.conversation_id}, 平台: {self.platform_id})")

    def activate(self, main_thought_context: Optional[str] = None) -> None:
        """激活此子思维实例，并可选择性接收主思维的当前想法。"""
        self.is_active = True
        self.last_interaction_time = datetime.datetime.now(datetime.timezone.utc)
        if main_thought_context:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}) 已被激活。主思维引导: '{main_thought_context[:50]}...'")
        else:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}) 已被激活。")

    def deactivate(self) -> None:
        """将此子思维实例设为非活跃（休眠）。"""
        self.is_active = False
        logger.info(f"ChatSession (会话ID: {self.conversation_id}) 已被设为非活跃（休眠）。")

    def add_interaction_to_context(self, interaction_data: Dict[str, Any]) -> None:
        """
        将新的交互记录（已处理为结构化字典）添加到聊天上下文中。
        interaction_data 预期包含: 'type' ('user_message' 或 'platform_event'),
                                'timestamp' (ISO 8601 UTC 字符串),
                                'sender_id' (如果适用),
                                'content_segments' (对消息而言, List[Dict]),
                                'event_details' (对事件而言, Dict)
        """
        self.chat_context.append(interaction_data)
        self.last_interaction_time = datetime.datetime.now(datetime.timezone.utc)
        logger.debug(f"ChatSession (会话ID: {self.conversation_id}): 新增交互到上下文。当前上下文数量: {len(self.chat_context)}")

    async def _get_last_bot_activity_for_prompt(self) -> Tuple[Optional[str], Optional[str]]:
        """
        从数据库获取机器人在此会话中最近的发言及其当时的想法。
        这是方案B的核心部分。
        """
        if not self.db_handler:
            return None, None
        
        last_activity_doc = await self.db_handler.get_sub_mind_last_activity(self.conversation_id)
        if last_activity_doc:
            last_reply = last_activity_doc.get("parsed_reply_text") # 应该从这里取
            last_reasoning = last_activity_doc.get("parsed_reasoning") # 应该从这里取
            logger.debug(f"ChatSession (会话ID: {self.conversation_id}): 从DB获取到上次活动: 回复='{str(last_reply)[:30]}...', 想法='{str(last_reasoning)[:30]}...'")
            return last_reply, last_reasoning
        return None, None

    async def generate_reply(self, main_thought_context: Optional[str] = None) -> Optional[MessageBase]:
        """
        根据当前上下文和主思维引导，异步生成聊天回复。
        [MODIFIED_V2]:
        - Added more detailed logging.
        - Added logic to add the bot's own reply to its chat_context.
        """
        if not self.is_active:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}) 未激活，不生成回复。")
            return None

        if not self.llm_client:
            logger.error(f"ChatSession (会话ID: {self.conversation_id}): LLM客户端未设置，无法生成回复。")
            return None

        # 1. 构建 System Prompt (基于核心人设)
        system_prompt_parts = [
            f"你现在是{self.core_persona_settings.bot_name}，{self.core_persona_settings.description}",
            self.core_persona_settings.profile,
        ]
        if self.group_info:
            system_prompt_parts.append(f"你正在群聊 '{self.group_info.group_name}' 中。")
            my_card_in_group = self.core_persona_settings.bot_name
            if self.user_info and self.user_info.user_id == self.bot_id and self.user_info.user_cardname:
                 pass
            system_prompt_parts.append(f"你在这个群的昵称是 '{my_card_in_group}'。")

        elif self.user_info:
            system_prompt_parts.append(f"你正在与用户 '{self.user_info.user_nickname or self.user_info.user_id}' 私聊。")
        system_prompt_str = "\n".join(filter(None, system_prompt_parts))
        logger.debug(f"ChatSession (会话ID: {self.conversation_id}): 构建的System Prompt: {system_prompt_str}")

        # 2. 构建 User Prompt (参考 枫_chat_test.md)
        user_prompt_yaml_parts = ["你现在正在群聊中，以下是群聊天记录及相关内容：", "```yaml"]

        # 添加 group_info (如果存在)
        if self.group_info:
            user_prompt_yaml_parts.append("\ngroup_info:")
            user_prompt_yaml_parts.append(f"  group_name: \"{self.group_info.group_name or '未知群名'}\"")

        # 添加 user_info (包含机器人自己和对话中的其他用户)
        all_users_in_context: Dict[str, Dict[str, Optional[str]]] = {}
        all_users_in_context[f"{self.bot_id}（你）"] = {
            "sender_nickname": self.core_persona_settings.bot_name,
            "sender_group_card": self.core_persona_settings.bot_name,
            "sender_group_titlename": None,
            "sender_group_permission": "成员"
        }
        if self.user_info:
             all_users_in_context[self.user_info.user_id or "unknown_user"] = {
                "sender_nickname": self.user_info.user_nickname,
                "sender_group_card": self.user_info.user_cardname,
                "sender_group_titlename": self.user_info.user_titlename,
                "sender_group_permission": self.user_info.permission_level or self.user_info.role
            }
        for interaction in self.chat_context:
            sender_id = interaction.get("sender_id")
            if sender_id and sender_id != self.bot_id and sender_id not in all_users_in_context:
                all_users_in_context[sender_id] = {
                    "sender_nickname": interaction.get("sender_nickname", f"用户_{sender_id}"),
                    "sender_group_card": interaction.get("sender_group_card"),
                    "sender_group_titlename": interaction.get("sender_group_titlename"),
                    "sender_group_permission": interaction.get("sender_group_permission")
                }
        if all_users_in_context:
            user_prompt_yaml_parts.append("\nuser_info:")
            for uid, u_info in all_users_in_context.items():
                user_prompt_yaml_parts.append(f"  \"{uid}\":")
                for key, val in u_info.items():
                    if val is not None:
                        user_prompt_yaml_parts.append(f"    {key}: \"{val}\"")


        # 添加 chat_history
        user_prompt_yaml_parts.append("\nchat_history:")
        for interaction in self.chat_context:
            user_prompt_yaml_parts.append(f"  - time: \"{interaction.get('timestamp', '')}\"")
            user_prompt_yaml_parts.append(f"    post_type: {interaction.get('type', 'message')}")
            if interaction.get('sub_type'):
                user_prompt_yaml_parts.append(f"    sub_type: {interaction.get('sub_type')}")
            if interaction.get('message_id'):
                 user_prompt_yaml_parts.append(f"    message_id: {interaction.get('message_id')}")

            if interaction.get('type') == 'user_message' and interaction.get('content_segments'):
                user_prompt_yaml_parts.append("    message:")
                for seg_dict in interaction.get('content_segments', []):
                    user_prompt_yaml_parts.append(f"      - type: {seg_dict.get('type')}")
                    seg_data = seg_dict.get('data', {})
                    if isinstance(seg_data, str) and seg_dict.get('type') == 'text':
                        seg_data = {"text": seg_data}
                    elif not isinstance(seg_data, dict):
                        seg_data = {"raw": str(seg_data)}

                    user_prompt_yaml_parts.append("        data:")
                    for k, v_data in seg_data.items():
                        user_prompt_yaml_parts.append(f"          {k}: \"{v_data}\"")

            elif interaction.get('type') == 'platform_event' and interaction.get('event_details'):
                event_details = interaction.get('event_details', {})
                user_prompt_yaml_parts.append(f"    notice_type: {event_details.get('notice_type', 'unknown_event')}")
                if event_details.get('operator_id'):
                    user_prompt_yaml_parts.append(f"    operator_id: {event_details.get('operator_id')}")
                if event_details.get('user_id'):
                    user_prompt_yaml_parts.append(f"    user_id: {event_details.get('user_id')}")

            elif interaction.get('type') == 'bot_message' and interaction.get('content_segments'):
                user_prompt_yaml_parts.append("    message:")
                for seg_dict in interaction.get('content_segments', []):
                    user_prompt_yaml_parts.append(f"      - type: {seg_dict.get('type')}")
                    seg_data = seg_dict.get('data', {})
                    if isinstance(seg_data, str) and seg_dict.get('type') == 'text':
                        seg_data = {"text": seg_data}
                    elif not isinstance(seg_data, dict):
                        seg_data = {"raw": str(seg_data)}
                    user_prompt_yaml_parts.append("        data:")
                    for k, v_data in seg_data.items():
                        user_prompt_yaml_parts.append(f"          {k}: \"{v_data}\"")


            if interaction.get('sender_id'):
                user_prompt_yaml_parts.append(f"    sender_id: {interaction.get('sender_id')}")
            
            if interaction.get('extra_info'):
                user_prompt_yaml_parts.append("    extra_info:")
                if isinstance(interaction.get('extra_info'), dict):
                    for k_extra, v_extra in interaction.get('extra_info', {}).items():
                         user_prompt_yaml_parts.append(f"      {k_extra}: \"{v_extra}\"")


        user_prompt_yaml_parts.append("```")
        current_time_for_prompt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        user_prompt_yaml_parts.append(f"\n当前时间：{current_time_for_prompt}")

        last_bot_reply, last_bot_reasoning = await self._get_last_bot_activity_for_prompt()
        if last_bot_reply:
            user_prompt_yaml_parts.append(f"\n你最近的发言内容是：\n\"{last_bot_reply}\"")
        else:
            user_prompt_yaml_parts.append("\n你最近没有在这个会话中发言。")
        if last_bot_reasoning:
            user_prompt_yaml_parts.append(f"\n你当时发言时的想法是：\n\"{last_bot_reasoning}\"")
        else:
            user_prompt_yaml_parts.append("\n你上次发言时未记录具体想法。")

        if main_thought_context:
            user_prompt_yaml_parts.append(f"\n[💭 主导意识的当前想法/关注点：\"{main_thought_context}\"]")
        
        if self.current_chat_style_directives:
            style_prompt = self.current_chat_style_directives.get("style_prompt_additions", [])
            goal_prompt = self.current_chat_style_directives.get("short_term_goal_for_reply")
            if style_prompt:
                user_prompt_yaml_parts.append("\n请注意以下聊天风格指引：")
                for hint in style_prompt:
                    user_prompt_yaml_parts.append(f"- {hint}")
            if goal_prompt:
                user_prompt_yaml_parts.append(f"\n你本次回复的短期目标是：{goal_prompt}")

        user_prompt_yaml_parts.append(
            "\n现在请你请输出你现在的内心想法，心情，是否要发言，发言的动机，和要发言的内容等等。"
            "\n请**严格**使用以下json格式输出内容，**不需要**输出markdown语句等多余内容，**仅输出**纯json内容："
            "\n```json\n"
            "{\n"
            "    \"mood\":\"此处填写你现在的心情，与造成这个心情的原因\",\n"
            "    \"reasoning\":\"此处填写你此时的内心想法，衔接你刚才的想法继续思考，应该自然流畅真实\",\n"
            "    \"reply_willing\":\"此处决定是否发言，布尔值，true为发言，false为先不发言\",\n"
            "    \"motivation\":\"此处填写发言/不发言的动机\",\n"
            "    \"at_someone\":\"【可选】仅在reply_willing为True时有效，当目前群聊比较混乱，需要明确对某人说话的时使用，填写你想@的人的qq号，如果需要@多个人，请用逗号隔开，如果不需要则留null\",\n"
            "    \"quote_reply\":\"【可选】仅在reply_willing为True时有效，当需要明确回复某条消息时使用，填写你想具体回复的消息的message_id，只能回复一条，如果不需要则留null\",\n"
            "    \"reply_text\":\"此处填写你完整的发言内容，应该尽可能简短，自然，口语化，多简短都可以。若已经@某人或引用回复某条消息，则建议省略主语。若reply_willing为False，则留null\",\n"
            "    \"poke\":\"【可选】qq戳一戳功能，无太大实际意义，多半是娱乐作用，或是试图引起某人注意，填写目标qq号，如果不需要则留null\",\n"
            "    \"action_to_take\": \"【可选】描述你当前最想做的、需要与外界交互的具体动作，例如上网查询某信息，如果无，则为null\", \n"
            "    \"action_motivation\": \"【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null\"\n"
            "}\n"
            "```"
        )
        user_prompt_str = "\n".join(user_prompt_yaml_parts)
        logger.debug(f"ChatSession (会话ID: {self.conversation_id}): 构建的User Prompt (截断):\n{user_prompt_str[:1000]}...")

        llm_response_data: Optional[Dict[str, Any]] = None
        try:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): 准备调用LLM...")
            llm_response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt_str,
                system_prompt=system_prompt_str,
                is_stream=False
            )
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): LLM调用完成。")
        except Exception as e_llm_call:
            logger.error(f"ChatSession (会话ID: {self.conversation_id}): 调用LLM时发生错误: {e_llm_call}", exc_info=True)
            return None

        if not llm_response_data or llm_response_data.get("error"):
            error_msg = llm_response_data.get('message', 'LLM调用失败或未返回有效数据') if llm_response_data else 'LLM调用失败或未返回有效数据'
            logger.error(f"ChatSession (会话ID: {self.conversation_id}): LLM未能成功生成回复。错误: {error_msg}")
            return None

        raw_llm_output_text = llm_response_data.get("text", "").strip()
        if not raw_llm_output_text:
            logger.warning(f"ChatSession (会话ID: {self.conversation_id}): LLM返回的文本内容为空。")
            return None
        
        parsed_llm_json: Optional[Dict[str, Any]] = None
        try:
            if raw_llm_output_text.startswith("```json"):
                json_str_to_parse = raw_llm_output_text[7:-3].strip()
            elif raw_llm_output_text.startswith("```"):
                 json_str_to_parse = raw_llm_output_text[3:-3].strip()
            else:
                json_str_to_parse = raw_llm_output_text
            
            parsed_llm_json = json.loads(json_str_to_parse)
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): 成功解析LLM的JSON输出。Reply_willing: {parsed_llm_json.get('reply_willing')}, Reply_text: '{str(parsed_llm_json.get('reply_text'))[:30]}...'")
        except json.JSONDecodeError as e_json_decode:
            logger.error(f"ChatSession (会话ID: {self.conversation_id}): 解析LLM输出的JSON失败: {e_json_decode}. 原始文本: {raw_llm_output_text}")
            self.main_mind_trigger_event.set()
            return None

        self.last_reply_generated = parsed_llm_json.get("reply_text")
        self.last_reply_main_thought_context = main_thought_context
        self.last_reply_reasoning = parsed_llm_json.get("reasoning")
        self.last_reply_mood = parsed_llm_json.get("mood")

        activity_log_data = {
            "conversation_id": self.conversation_id,
            "platform_id": self.platform_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
            "llm_input_system_prompt": system_prompt_str,
            "llm_input_user_prompt": user_prompt_str,
            "main_thought_context_injected": main_thought_context,
            "llm_output_json_str": raw_llm_output_text,
            "parsed_mood": self.last_reply_mood,
            "parsed_reasoning": self.last_reply_reasoning,
            "parsed_reply_willing": parsed_llm_json.get("reply_willing", False),
            "parsed_reply_text": self.last_reply_generated,
            "parsed_at_someone": parsed_llm_json.get("at_someone"),
            "parsed_quote_reply": parsed_llm_json.get("quote_reply"),
            "parsed_poke": parsed_llm_json.get("poke"),
            "parsed_action_to_take": parsed_llm_json.get("action_to_take"),
            "parsed_action_motivation": parsed_llm_json.get("action_motivation"),
        }
        if self.db_handler:
            logger.debug(f"ChatSession (会话ID: {self.conversation_id}): 准备保存子思维活动日志...")
            await self.db_handler.save_sub_mind_activity(activity_log_data)
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): 子思维活动日志已保存。")

        else:
            logger.warning(f"ChatSession (会话ID: {self.conversation_id}): DB Handler未设置，无法保存子思维活动日志。")

        if parsed_llm_json.get("reply_willing") and self.last_reply_generated:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): LLM决定回复，内容: '{self.last_reply_generated[:50]}...'")
            bot_reply_interaction_record: Dict[str, Any] = {
                "type": "bot_message", 
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
                "message_id": f"bot_reply_{self.conversation_id}_{uuid.uuid4()}",
                "sender_id": self.bot_id, 
                "sender_nickname": self.core_persona_settings.bot_name,
                "content_segments": [{"type": "text", "data": {"text": self.last_reply_generated}}],
                "extra_info": {
                    "mood_at_reply": self.last_reply_mood,
                    "reasoning_for_reply": self.last_reply_reasoning,
                    "motivation_for_reply": parsed_llm_json.get("motivation")
                }
            }
            self.add_interaction_to_context(bot_reply_interaction_record)
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): 已将机器人自身的回复添加到内部上下文中。当前上下文数量: {len(self.chat_context)}")
        elif parsed_llm_json.get("reply_willing"):
            logger.warning(f"ChatSession (会话ID: {self.conversation_id}): LLM决定回复 (reply_willing=true)，但 reply_text 为空。")
        else:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): LLM决定不回复 (reply_willing={parsed_llm_json.get('reply_willing')})。")

        core_action_to_send: Optional[MessageBase] = None
        if parsed_llm_json.get("reply_willing") and self.last_reply_generated:
            segments_for_reply: List[Seg] = []
            at_target_str = parsed_llm_json.get("at_someone")
            if at_target_str:
                target_ids = [uid.strip() for uid in at_target_str.split(',') if uid.strip()]
                for target_id in target_ids:
                    segments_for_reply.append(Seg(type="at", data={"user_id": target_id}))
                    segments_for_reply.append(Seg(type="text", data=" "))
            
            segments_for_reply.append(Seg(type="text", data=self.last_reply_generated))

            action_data_send_msg: Dict[str, Any] = {"segments": [s.to_dict() for s in segments_for_reply]}
            if self.group_info and self.group_info.group_id:
                action_data_send_msg["target_group_id"] = self.group_info.group_id
            elif self.user_info and self.user_info.user_id:
                action_data_send_msg["target_user_id"] = self.user_info.user_id
            
            quote_reply_msg_id = parsed_llm_json.get("quote_reply")
            if quote_reply_msg_id:
                action_data_send_msg["reply_to_message_id"] = str(quote_reply_msg_id)

            core_action_seg = Seg(type="action:send_message", data=action_data_send_msg)
            
            action_message_info = BaseMessageInfo(
                platform=self.platform_id,
                bot_id=self.bot_id,
                interaction_purpose="core_action",
                time=int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
                message_id=f"sub_mind_reply_{self.conversation_id}_{uuid.uuid4()}",
                group_info=self.group_info,
                user_info=self.user_info if not self.group_info else None,
                additional_config={"protocol_version": "1.2.0"}
            )
            core_action_to_send = MessageBase(
                message_info=action_message_info,
                message_segment=Seg(type="seglist", data=[core_action_seg.to_dict()])
            )
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): 已构建发送消息的core_action。")

        poke_target_id = parsed_llm_json.get("poke")
        if poke_target_id:
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): LLM决定戳一戳目标 '{poke_target_id}'。")
            poke_action_data: Dict[str, Any] = {"target_user_id": str(poke_target_id)}
            if self.group_info and self.group_info.group_id:
                poke_action_data["target_group_id"] = self.group_info.group_id
            
            poke_action_seg = Seg(type="action:send_poke", data=poke_action_data)
            
            if core_action_to_send:
                if isinstance(core_action_to_send.message_segment.data, list):
                    core_action_to_send.message_segment.data.append(poke_action_seg.to_dict())
                else:
                    core_action_to_send.message_segment.data = [poke_action_seg.to_dict()]
            else:
                action_message_info_poke = BaseMessageInfo(
                     platform=self.platform_id,
                     bot_id=self.bot_id,
                     interaction_purpose="core_action",
                     time=int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
                     message_id=f"sub_mind_poke_{self.conversation_id}_{uuid.uuid4()}",
                     group_info=self.group_info,
                     user_info=self.user_info if not self.group_info else None,
                     additional_config={"protocol_version": "1.2.0"}
                )
                core_action_to_send = MessageBase(
                    message_info=action_message_info_poke,
                    message_segment=Seg(type="seglist", data=[poke_action_seg.to_dict()])
                )
            logger.info(f"ChatSession (会话ID: {self.conversation_id}): 已构建/追加戳一戳的core_action。")

        self.main_mind_trigger_event.set()
        logger.debug(f"ChatSession (会话ID: {self.conversation_id}): 已设置main_mind_trigger_event。")

        return core_action_to_send


    def get_status_summary(self) -> Dict[str, Any]:
        """返回此子思维会话的当前状态摘要，供主思维使用。"""
        return {
            "conversation_id": self.conversation_id,
            "platform_id": self.platform_id,
            "is_active": self.is_active,
            "last_interaction_time": self.last_interaction_time.isoformat() + "Z",
            "context_size": len(self.chat_context),
            "last_reply_generated": self.last_reply_generated,
            "last_reply_main_thought_context": self.last_reply_main_thought_context,
            "last_reply_reasoning": self.last_reply_reasoning,
            "last_reply_mood": self.last_reply_mood,
            "current_chat_style_directives": self.current_chat_style_directives
        }


class ChatSessionManager:
    """
    管理所有 ChatSession 实例。
    """
    def __init__(self, core_logic_ref: 'CurrentCoreLogic'): # 使用修正后的类型提示
        self.active_sessions: Dict[str, ChatSession] = {}
        self.core_logic_ref: 'CurrentCoreLogic' = core_logic_ref
        self.core_persona_settings: PersonaSettings = core_logic_ref.root_cfg.persona
        self.sub_mind_llm_client: LLMProcessorClient = core_logic_ref.sub_mind_llm_client
        self.db_handler: ArangoDBHandler = core_logic_ref.db_handler

        logger.info("ChatSessionManager 初始化完成。")

    def _generate_internal_conversation_id(self, platform_id: str, group_id: Optional[str], user_id: Optional[str], bot_id: str) -> str:
        """
        根据平台、群组ID、用户ID和机器人ID生成内部统一的会话ID。
        确保对于同一个私聊或群聊，生成的ID是固定的。
        """
        if group_id:
            return f"{platform_id}_group_{group_id}"
        elif user_id:
            participants = sorted([user_id, bot_id])
            return f"{platform_id}_dm_{participants[0]}_{participants[1]}"
        else:
            logger.error("无法生成内部会话ID：同时缺少 group_id 和 user_id。")
            return f"{platform_id}_unknown_{uuid.uuid4()}"


    async def get_or_create_session(
        self,
        message: MessageBase
    ) -> ChatSession:
        """
        根据传入的 MessageBase 对象获取或创建一个新的 ChatSession 实例。
        """
        msg_info = message.message_info
        platform_id = msg_info.platform
        bot_id_from_msg = msg_info.bot_id

        group_id: Optional[str] = None
        if msg_info.group_info and msg_info.group_info.group_id:
            group_id = msg_info.group_info.group_id
        
        user_id_of_sender: Optional[str] = None
        if msg_info.user_info and msg_info.user_info.user_id:
            user_id_of_sender = msg_info.user_info.user_id

        internal_conv_id = self._generate_internal_conversation_id(platform_id, group_id, user_id_of_sender, bot_id_from_msg)


        if internal_conv_id not in self.active_sessions:
            logger.info(f"未找到会话ID '{internal_conv_id}' 的ChatSession实例，将创建新的实例。")
            
            initial_group_info_for_session: Optional[GroupInfo] = None
            if group_id and msg_info.group_info:
                initial_group_info_for_session = msg_info.group_info
            
            initial_user_info_for_session: Optional[UserInfo] = None
            if not group_id and user_id_of_sender and msg_info.user_info:
                initial_user_info_for_session = msg_info.user_info


            self.active_sessions[internal_conv_id] = ChatSession(
                conversation_id=internal_conv_id,
                platform_id=platform_id,
                bot_id=bot_id_from_msg,
                core_persona_settings=self.core_persona_settings,
                llm_client=self.sub_mind_llm_client,
                db_handler=self.db_handler,
                main_mind_trigger_event=self.core_logic_ref.sub_mind_update_event,
                initial_group_info=initial_group_info_for_session,
                initial_user_info=initial_user_info_for_session
            )
            logger.info(f"已为会话ID '{internal_conv_id}' 创建并注册了新的ChatSession实例。")
        else:
            logger.debug(f"已找到会话ID '{internal_conv_id}' 的现有ChatSession实例。")
            if group_id and msg_info.group_info and self.active_sessions[internal_conv_id].group_info != msg_info.group_info:
                self.active_sessions[internal_conv_id].group_info = msg_info.group_info
                logger.debug(f"会话 {internal_conv_id} 的群组信息已更新。")


        return self.active_sessions[internal_conv_id]

    async def handle_incoming_user_message(self, message: MessageBase) -> None:
        """
        处理来自 DefaultMessageProcessor 的用户消息。
        仅更新对应 ChatSession 的上下文，不直接触发回复。
        """
        session = await self.get_or_create_session(message)
        
        interaction_record: Dict[str, Any] = {
            "type": "user_message",
            "timestamp": datetime.datetime.fromtimestamp(message.message_info.time / 1000.0, tz=datetime.timezone.utc).isoformat() + "Z",
            "message_id": message.message_info.message_id,
            "sender_id": message.message_info.user_info.user_id if message.message_info.user_info else "unknown_sender",
            "sender_nickname": message.message_info.user_info.user_nickname if message.message_info.user_info else None,
            "sender_group_card": message.message_info.user_info.user_cardname if message.message_info.user_info else None,
            "sender_group_titlename": message.message_info.user_info.user_titlename if message.message_info.user_info else None,
            "sender_group_permission": message.message_info.user_info.permission_level or \
                                     (message.message_info.user_info.role if message.message_info.user_info else None),
            "content_segments": [seg.to_dict() for seg in message.message_segment.data] if message.message_segment and isinstance(message.message_segment.data, list) else [],
            "sub_type": message.message_info.sub_type,
        }
        session.add_interaction_to_context(interaction_record)
        logger.info(f"ChatSessionManager: 用户消息已添加到会话 '{session.conversation_id}' 的上下文中。")

    async def handle_incoming_platform_event(self, event_message: MessageBase) -> None:
        """
        处理来自 DefaultMessageProcessor 的平台事件（如戳一戳、成员变动等）。
        仅更新对应 ChatSession 的上下文。
        """
        session = await self.get_or_create_session(event_message)
        
        event_details_dict: Dict[str, Any] = {}
        event_type_from_seg = "unknown_platform_event"

        if event_message.message_segment and isinstance(event_message.message_segment.data, list) and event_message.message_segment.data:
            first_seg = event_message.message_segment.data[0]
            if isinstance(first_seg, Seg) and isinstance(first_seg.data, dict):
                event_details_dict = first_seg.data.copy()
                event_type_from_seg = first_seg.type
            elif isinstance(first_seg, dict):
                 event_details_dict = first_seg.get("data", {}).copy()
                 event_type_from_seg = first_seg.get("type", "unknown_platform_event")


        interaction_record: Dict[str, Any] = {
            "type": "platform_event",
            "event_type_detail": event_type_from_seg,
            "timestamp": datetime.datetime.fromtimestamp(event_message.message_info.time / 1000.0, tz=datetime.timezone.utc).isoformat() + "Z",
            "message_id": event_message.message_info.message_id,
            "actor_id": event_message.message_info.user_info.user_id if event_message.message_info.user_info else None,
            "actor_nickname": event_message.message_info.user_info.user_nickname if event_message.message_info.user_info else None,
            "event_details": event_details_dict,
        }
        session.add_interaction_to_context(interaction_record)
        logger.info(f"ChatSessionManager: 平台事件 ({event_type_from_seg}) 已添加到会话 '{session.conversation_id}' 的上下文中。")

    async def trigger_session_reply(
        self,
        conversation_id: str,
        main_thought_context: Optional[str] = None
    ) -> Optional[MessageBase]:
        session = self.active_sessions.get(conversation_id)
        if session:
            if not session.is_active:
                logger.info(f"ChatSessionManager: 会话 '{conversation_id}' 当前未激活，将在触发回复前先激活它。")
                session.activate(main_thought_context)

            if session.is_active:
                logger.info(f"ChatSessionManager: 正在为已激活的会话 '{conversation_id}' 触发子思维回复生成。主思维引导: '{str(main_thought_context)[:50]}...'")
                core_action_message = await session.generate_reply(main_thought_context)
                if core_action_message:
                    logger.info(f"ChatSessionManager: 会话 '{conversation_id}' 的子思维已成功生成回复动作。")
                    return core_action_message
                else:
                    logger.warning(f"ChatSessionManager: 会话 '{conversation_id}' 的子思维未能生成回复动作。")
                    return None
            else:
                logger.error(f"ChatSessionManager: 尝试激活会话 '{conversation_id}' 后，它仍然处于非激活状态。无法触发回复。")
                return None
        else:
            logger.warning(f"ChatSessionManager: 尝试为不存在的会话 '{conversation_id}' 触发回复。")
            return None

    def activate_session(self, conversation_id: str, main_thought_context: Optional[str] = None) -> None:
        """激活指定会话的子思维。"""
        session = self.active_sessions.get(conversation_id)
        if session:
            session.activate(main_thought_context)
            logger.info(f"ChatSessionManager: 已激活会话 '{conversation_id}' 的子思维。")
        else:
            logger.warning(f"ChatSessionManager: 尝试激活不存在的会话 '{conversation_id}'。")

    def deactivate_session(self, conversation_id: str) -> None:
        """停用指定会话的子思维。"""
        session = self.active_sessions.get(conversation_id)
        if session:
            session.deactivate()
            logger.info(f"ChatSessionManager: 已停用会话 '{conversation_id}' 的子思维。")
        else:
            logger.warning(f"ChatSessionManager: 尝试停用不存在的会话 '{conversation_id}'。")
            
    def set_chat_style_directives(self, conversation_id: str, directives: Dict[str, Any]) -> None:
        """为指定会话设置聊天风格或短期目标指令。"""
        session = self.active_sessions.get(conversation_id)
        if session:
            session.current_chat_style_directives = directives
            logger.info(f"ChatSessionManager: 已为会话 '{conversation_id}' 设置聊天指令: {directives}")
        else:
            logger.warning(f"ChatSessionManager: 尝试为不存在的会话 '{conversation_id}' 设置聊天指令。")


    def get_session_summary(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """获取单个会话的状态摘要。"""
        session = self.active_sessions.get(conversation_id)
        if session:
            return session.get_status_summary()
        return None

    def get_all_active_sessions_summary(self) -> List[Dict[str, Any]]:
        """获取所有当前活跃的子思维会话的状态摘要列表。"""
        summaries = []
        for session_id, session_instance in self.active_sessions.items():
            summaries.append(session_instance.get_status_summary())
        return summaries

    async def _periodic_cleanup_task(self, cleanup_interval_seconds: int = 3600, inactive_threshold_hours: int = 24):
        """后台任务，定期检查并休眠/清理长时间不活跃的会话。"""
        while True:
            await asyncio.sleep(cleanup_interval_seconds)
            logger.info("ChatSessionManager: 开始执行非活跃会话定期清理...")
            now = datetime.datetime.now(datetime.timezone.utc)
            inactive_threshold = datetime.timedelta(hours=inactive_threshold_hours)
            
            sessions_to_deactivate = []

            for conv_id, session_instance in list(self.active_sessions.items()):
                if session_instance.is_active and (now - session_instance.last_interaction_time > inactive_threshold):
                    sessions_to_deactivate.append(conv_id)

            for conv_id in sessions_to_deactivate:
                self.deactivate_session(conv_id)
                logger.info(f"ChatSessionManager: 会话 '{conv_id}' 因长时间未活动已被自动休眠。")
            logger.info("ChatSessionManager: 非活跃会话定期清理完成。")