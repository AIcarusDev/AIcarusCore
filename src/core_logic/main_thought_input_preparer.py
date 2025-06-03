# src/core_logic/main_thought_input_preparer.py
import asyncio
import datetime
import random
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger
from src.common.utils import format_chat_history_for_prompt
from src.database.arangodb_handler import ArangoDBHandler
# 🐾 小懒猫加的：导入 ChatSessionManager，因为 prepare_current_state_for_prompt 需要用到
from src.sub_consciousness.chat_session_handler import ChatSessionManager

if TYPE_CHECKING:
    from src.plugins.intrusive_thoughts_plugin import IntrusiveThoughtsGenerator


logger = get_logger("AIcarusCore.CoreLogic.InputPreparer") # 新日志名称

class MainThoughtInputPreparer:
    """
    负责准备主思维LLM所需的各种输入数据，包括当前状态、上下文信息和侵入性思维。
    """

    # 🐾 小懒猫加的：这里需要复制 CoreLogic 的 INITIAL_STATE，或者从 CoreLogic 那里传过来
    # 为了简化，这里直接复制一份，或者更优雅的做法是让 CoreLogic 把这个常量作为参数传给它
    # 但考虑到是常量，直接在这里定义一份也没什么大问题，但最好在初始化时确认数据一致性
    INITIAL_STATE: Dict[str, Any] = {
        "mood": "平静。",
        "previous_thinking": "这是你的第一次思考，请开始吧。",
        "thinking_guidance": "随意发散一下吧。",
        "current_task_info_for_prompt": "你当前没有什么特定的目标或任务。",
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
        "active_sub_mind_latest_activity": "目前没有活跃的子思维会话，或者它们最近没有活动。",
    }

    def __init__(
        self,
        db_handler: ArangoDBHandler,
        chat_session_manager: Optional[ChatSessionManager],
        intrusive_generator_instance: Optional['IntrusiveThoughtsGenerator'],
        logger_instance: Any, # 仍然传入logger，保持一致性
        core_logic_settings: Any # 🐾 小懒猫加的：需要传入 CoreLogicSettings 来获取 chat_history_context_duration_minutes
    ):
        self.db_handler = db_handler
        self.chat_session_manager = chat_session_manager
        self.intrusive_generator_instance = intrusive_generator_instance
        self.logger = logger_instance
        self.core_logic_settings = core_logic_settings # 保存设置

        self.logger.info("MainThoughtInputPreparer 实例创建完成。")

    async def prepare_current_state_for_prompt(
        self,
        latest_thought_document: Optional[Dict[str, Any]],
        current_focused_conversation_id: Optional[str] # 🐾 小懒猫加的：这个参数需要从 CoreLogic 传递过来
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        准备用于LLM Prompt的当前状态字典，包括从DB获取最新思考和上下文。
        从 CoreLogic 的 _process_thought_and_action_state 方法中移动过来。
        """
        current_state: Dict[str, Any] = {}
        action_id_result_shown_in_prompt: Optional[str] = None

        if latest_thought_document:
            self.logger.debug("使用数据库中的最新思考文档来构建当前状态。")
            current_state["mood"] = latest_thought_document.get("emotion_output", self.INITIAL_STATE["mood"])
            current_state["previous_thinking"] = latest_thought_document.get("think_output", self.INITIAL_STATE["previous_thinking"])
            current_state["thinking_guidance"] = latest_thought_document.get("next_think_output", self.INITIAL_STATE["thinking_guidance"])
            current_state["current_task"] = latest_thought_document.get("to_do_output", "")

            action_attempted = latest_thought_document.get("action_attempted")
            if isinstance(action_attempted, dict):
                action_status = action_attempted.get("status", "UNKNOWN")
                action_desc = action_attempted.get("action_description", "未知动作")
                action_id = action_attempted.get("action_id")

                if action_status == "PENDING":
                    current_state["pending_action_status"] = f"你当前有一个待处理的行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'})。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                elif action_status in ["PROCESSING_DECISION", "TOOL_EXECUTING", "PROCESSING_SUMMARY"]:
                    current_state["pending_action_status"] = f"你当前正在处理行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'})，状态: {action_status}。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                elif action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]:
                    result_for_shuang = action_attempted.get("final_result_for_shuang", "动作已完成，但没有具体结果文本。")
                    current_state["action_result_info"] = (
                        f"你上一轮行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{result_for_shuang}】"
                    )
                    current_state["pending_action_status"] = ""
                    if action_id and not action_attempted.get("result_seen_by_shuang", False):
                        action_id_result_shown_in_prompt = action_id
                else:
                    current_state["pending_action_status"] = f"你上一轮的行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'}) 状态未知 ({action_status})。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
            else:
                current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                current_state["pending_action_status"] = self.INITIAL_STATE["pending_action_status"]
        else:
            self.logger.info("最新的思考文档为空，主思维将使用初始思考状态。")
            current_state = self.INITIAL_STATE.copy()
            current_state["current_task"] = ""

        # 🐾 小懒猫加的：从 CoreLogicSettings 获取 chat_history_context_duration_minutes
        chat_history_duration_minutes = getattr(self.core_logic_settings, "chat_history_context_duration_minutes", 10)

        formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"]
        if self.db_handler:
            try:
                raw_context_messages = await self.db_handler.get_recent_chat_messages_for_context(
                    duration_minutes=chat_history_duration_minutes,
                    conversation_id=current_focused_conversation_id # 🐾 小懒猫加的：使用传入的会话ID
                )
                if raw_context_messages:
                    formatted_recent_contextual_info = format_chat_history_for_prompt(raw_context_messages)
                self.logger.debug(f"获取上下文信息完成。上下文长度: {len(formatted_recent_contextual_info)}")
            except Exception as e_hist:
                self.logger.error(f"获取或格式化最近上下文信息时出错: {e_hist}", exc_info=True)

        current_state["recent_contextual_information"] = formatted_recent_contextual_info

        if self.chat_session_manager:
            active_sessions_summary = self.chat_session_manager.get_all_active_sessions_summary()

            if active_sessions_summary:
                summaries_str_parts = []
                for summary_item in active_sessions_summary:
                    summaries_str_parts.append(f"- 会话ID {summary_item.get('conversation_id', '未知')}: 状态={'活跃' if summary_item.get('is_active') else '不活跃'}, 上次回复='{str(summary_item.get('last_reply_generated', '无'))[:30]}...'")
                current_state["active_sub_mind_latest_activity"] = "\n".join(summaries_str_parts)
            else:
                current_state["active_sub_mind_latest_activity"] = self.INITIAL_STATE["active_sub_mind_latest_activity"]
        else:
            current_state["active_sub_mind_latest_activity"] = self.INITIAL_STATE["active_sub_mind_latest_activity"]

        self.logger.debug(f"在 prepare_current_state_for_prompt 中：成功处理并返回用于Prompt的状态。Action ID shown: {action_id_result_shown_in_prompt}")
        return current_state, action_id_result_shown_in_prompt

    async def get_intrusive_thought(self) -> str:
        """
        获取一个随机侵入性思维，并标记为已使用。
        从 CoreLogic 的 _get_intrusive_thought_for_cycle 方法中移动过来。
        """
        intrusive_thought_text: str = ""
        random_thought_doc: Optional[Dict[str, Any]] = None

        if self.intrusive_generator_instance and \
           self.intrusive_generator_instance.module_settings.enabled and \
           random.random() < self.intrusive_generator_instance.module_settings.insertion_probability:
            if self.db_handler:
                random_thought_doc = await self.db_handler.get_random_intrusive_thought()
                if random_thought_doc and "text" in random_thought_doc:
                    intrusive_thought_text = f"你突然有一个神奇的念头：{random_thought_doc['text']}"
                    if "_key" in random_thought_doc:
                        try:
                            await self.db_handler.update_intrusive_thought_status(
                                random_thought_doc["_key"], used=True
                            )
                            self.logger.debug(f"侵入性思维 '{random_thought_doc['text'][:30]}...' (Key: {random_thought_doc['_key']}) 已被标记为已使用。")
                        except Exception as e_mark:
                            self.logger.error(f"标记侵入性思维为已使用失败 (Key: {random_thought_doc['_key']}): {e_mark}", exc_info=True)
                else:
                    self.logger.debug("未能从侵入性思维池中抽取到未使用的侵入性思维。")
            else:
                self.logger.warning("无法获取侵入性思维，因为数据库处理器 (db_handler) 未初始化。")

        if intrusive_thought_text:
             self.logger.info(f"  本轮注入侵入性思维: {intrusive_thought_text[:60]}...")
        return intrusive_thought_text