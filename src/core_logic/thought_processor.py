import asyncio
import datetime
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.database.arangodb_handler import ArangoDBHandler
from src.sub_consciousness.chat_session_handler import ChatSessionManager
class CoreThoughtProcessor:
    """
    负责处理主思维LLM返回的思考结果。
    """
    def __init__(self, db_handler: ArangoDBHandler, action_handler_instance: Optional[ActionHandler],
                 chat_session_manager: Optional[ChatSessionManager], core_comm_layer: Optional[CoreWebsocketServer],
                 logger_instance: Any): # 就是这里，新增这个参数！
        print(f"DEBUG: CoreThoughtProcessor __init__ called with logger_instance: {logger_instance is not None}")
        """
        初始化思考处理器。

        Args:
            db_handler: 数据库处理器实例。
            action_handler_instance: 动作处理器实例。
            chat_session_manager: 聊天会话管理器实例。
            core_comm_layer: 核心通信层实例。
            logger_instance: 日志记录器实例。
        """
        self.logger = logger_instance # 接收并存储日志记录器实例
        self.db_handler = db_handler
        self.action_handler_instance = action_handler_instance
        self.chat_session_manager = chat_session_manager
        self.core_comm_layer = core_comm_layer

    async def process_thought_and_actions(
        self,
        generated_thought_json: Dict[str, Any],
        current_state_for_prompt: Dict[str, Any],
        current_time_formatted_str: str,
        system_prompt_sent: Optional[str],
        full_prompt_text_sent: Optional[str],
        intrusive_thought_to_inject_this_cycle: str,
        formatted_recent_contextual_info: str,
        action_id_whose_result_was_shown_in_last_prompt: Optional[str],
        loop_count: int
    ) -> Tuple[Optional[str], List[asyncio.Task]]:
        """
        处理LLM生成的思考结果，包括保存思考、处理动作、处理子思维指令。
        """
        self.logger.info(f"主思维循环 {loop_count}: LLM成功返回思考结果。正在处理...")

        initiated_action_data_for_db: Optional[Dict[str, Any]] = None
        action_info_for_task_processing: Optional[Dict[str, Any]] = None
        saved_thought_doc_key: Optional[str] = None
        background_action_tasks: List[asyncio.Task] = []

        think_output_text = generated_thought_json.get("think") or "未思考"
        self.logger.info(f"主思维循环 {loop_count}: 解析后的思考内容: '{think_output_text[:50]}...'")

        document_to_save_in_main_db: Dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "time_injected_to_prompt": current_time_formatted_str,
            "system_prompt_sent": system_prompt_sent if system_prompt_sent else "System Prompt 未能构建",
            "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle,
            "mood_input": current_state_for_prompt.get("mood"),
            "previous_thinking_input": current_state_for_prompt.get("previous_thinking"),
            "thinking_guidance_input": current_state_for_prompt.get("thinking_guidance"),
            "task_input_info": current_state_for_prompt.get("current_task_info_for_prompt", "无特定任务输入"),
            "action_result_input": current_state_for_prompt.get("action_result_info", ""),
            "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""),
            "recent_contextual_information_input": formatted_recent_contextual_info,
            "active_sub_mind_latest_activity_input": current_state_for_prompt.get("active_sub_mind_latest_activity"),
            "full_user_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "User Prompt 未能构建",
            "think_output": generated_thought_json.get("think"),
            "emotion_output": generated_thought_json.get("emotion"),
            "next_think_output": generated_thought_json.get("next_think"),
            "to_do_output": generated_thought_json.get("to_do", ""),
            "done_output": generated_thought_json.get("done", False),
            "action_to_take_output": generated_thought_json.get("action_to_take", ""),
            "action_motivation_output": generated_thought_json.get("action_motivation", ""),
            "sub_mind_directives_output": generated_thought_json.get("sub_mind_directives"),
        }

        action_description_from_llm_raw = generated_thought_json.get("action_to_take")
        action_description_from_llm_clean = action_description_from_llm_raw.strip() \
            if isinstance(action_description_from_llm_raw, str) else ""

        action_motivation_from_llm_raw = generated_thought_json.get("action_motivation")
        action_motivation_from_llm_clean = action_motivation_from_llm_raw.strip() \
            if isinstance(action_motivation_from_llm_raw, str) else ""

        if action_description_from_llm_clean:
            current_action_id = str(uuid.uuid4())
            initiated_action_data_for_db = {
                "action_description": action_description_from_llm_clean,
                "action_motivation": action_motivation_from_llm_clean,
                "action_id": current_action_id,
                "status": "PENDING",
                "result_seen_by_shuang": False,
                "initiated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            action_info_for_task_processing = {
                "action_id": current_action_id,
                "action_description": action_description_from_llm_clean,
                "action_motivation": action_motivation_from_llm_clean,
                "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"),
            }
            self.logger.info(
                f"  >>> 外部行动意图产生: '{action_description_from_llm_clean}' "
                f"(ID: {current_action_id[:8]})"
            )

        document_to_save_in_main_db["action_attempted"] = initiated_action_data_for_db

        if "_llm_usage_info" in generated_thought_json:
            document_to_save_in_main_db["_llm_usage_info"] = generated_thought_json["_llm_usage_info"]

        if self.db_handler:
            try:
                saved_thought_doc_key = await self.db_handler.save_thought_document(document_to_save_in_main_db)
                self.logger.info(f"主思维循环 {loop_count}: 思考文档 (Key: {saved_thought_doc_key}) 已保存。")
            except Exception as e_save_thought:
                 self.logger.error(f"主思维循环 {loop_count}: 保存思考文档失败: {e_save_thought}", exc_info=True)
        else:
            self.logger.error(f"主思维循环 {loop_count}: 数据库处理器 (db_handler) 未初始化，无法保存思考文档。")

        if action_id_whose_result_was_shown_in_last_prompt and self.db_handler:
            try:
                await self.db_handler.mark_action_result_as_seen(action_id_whose_result_was_shown_in_last_prompt)
                self.logger.info(f"主思维循环 {loop_count}: 动作结果 (ID: {action_id_whose_result_was_shown_in_last_prompt[:8]}) 已标记为已阅。")
            except Exception as e_mark_seen:
                self.logger.error(f"主思维循环 {loop_count}: 标记动作结果为已阅时失败: {e_mark_seen}", exc_info=True)

        if action_info_for_task_processing and saved_thought_doc_key and self.action_handler_instance:
            self.logger.info(f"主思维循环 {loop_count}: 准备异步处理动作 ID: {action_info_for_task_processing['action_id'][:8]}。")
            action_processing_task: asyncio.Task = asyncio.create_task(
                self.action_handler_instance.process_action_flow(
                    action_id=action_info_for_task_processing["action_id"],
                    doc_key_for_updates=saved_thought_doc_key,
                    action_description=action_info_for_task_processing["action_description"],
                    action_motivation=action_info_for_task_processing["action_motivation"],
                    current_thought_context=action_info_for_task_processing["current_thought_context"],
                )
            )
            background_action_tasks.append(action_processing_task)
            self.logger.info(
                f"      外部动作 '{action_info_for_task_processing['action_description']}' "
                f"(ID: {action_info_for_task_processing['action_id'][:8]}, "
                f"关联思考DocKey: {saved_thought_doc_key}) 已异步启动处理。"
            )
        elif action_info_for_task_processing and not saved_thought_doc_key:
            self.logger.error(
                f"主思维循环 {loop_count}: 未能获取保存思考文档的 _key，无法为外部动作 ID "
                f"{action_info_for_task_processing['action_id']} 创建处理任务。"
            )
        elif action_info_for_task_processing and not self.action_handler_instance:
            self.logger.error(
                f"主思维循环 {loop_count}: ActionHandler 未初始化，无法为外部动作 ID "
                f"{action_info_for_task_processing['action_id']} 创建处理任务。"
            )

        sub_mind_directives_list = generated_thought_json.get("sub_mind_directives")
        if isinstance(sub_mind_directives_list, list) and self.chat_session_manager and self.core_comm_layer:
            self.logger.info(f"主思维循环 {loop_count}: 开始处理 {len(sub_mind_directives_list)} 条子思维指令。")
            for directive_item_dict in sub_mind_directives_list:
                if isinstance(directive_item_dict, dict):
                    target_conversation_id_from_llm = directive_item_dict.get("conversation_id")
                    directive_action_type = directive_item_dict.get("directive_type")

                    if target_conversation_id_from_llm and directive_action_type:
                        resolved_target_conversation_id = target_conversation_id_from_llm
                        if not ("_group_" in target_conversation_id_from_llm or "_dm_" in target_conversation_id_from_llm):
                            self.logger.warning(f"  子思维指令中的 conversation_id '{target_conversation_id_from_llm}' 看起来不是内部完整格式。尝试转换...")
                            if target_conversation_id_from_llm.isdigit():
                                _platform = "napcat_qq"
                                potential_group_id = f"{_platform}_group_{target_conversation_id_from_llm}"
                                temp_session_check = self.chat_session_manager.active_sessions.get(potential_group_id)
                                if temp_session_check:
                                    resolved_target_conversation_id = potential_group_id
                                    self.logger.info(f"  已将LLM的短ID '{target_conversation_id_from_llm}' 解析为群聊ID: {resolved_target_conversation_id}")
                                else:
                                    self.logger.warning(f"  无法将短ID '{target_conversation_id_from_llm}' 转换为已知的群聊会话ID (平台:{_platform})。将尝试使用原始ID。")
                            else:
                                self.logger.debug(f"  LLM提供的会话ID '{target_conversation_id_from_llm}' 不是纯数字，将尝试直接使用。")

                        main_thought_for_sub_mind_injection = directive_item_dict.get(
                            "main_thought_for_reply",
                            generated_thought_json.get("think")
                        )
                        self.logger.debug(f"  处理指令: 类型='{directive_action_type}', 目标会话='{resolved_target_conversation_id}', 引导思想='{str(main_thought_for_sub_mind_injection)[:30]}...'")

                        if directive_action_type == "TRIGGER_REPLY":
                            core_action_from_sub_mind = await self.chat_session_manager.trigger_session_reply(
                                conversation_id=resolved_target_conversation_id,
                                main_thought_context=main_thought_for_sub_mind_injection
                            )
                            if core_action_from_sub_mind:
                                self.logger.info(f"    子思维回复动作将发送给适配器 (会话: {resolved_target_conversation_id})。")
                                await self.core_comm_layer.broadcast_action_to_adapters(core_action_from_sub_mind)
                            else:
                                self.logger.info(f"    子思维未生成回复动作 (会话: {resolved_target_conversation_id})。")

                        elif directive_action_type == "ACTIVATE_SESSION":
                            self.chat_session_manager.activate_session(
                                conversation_id=resolved_target_conversation_id,
                                main_thought_context=main_thought_for_sub_mind_injection
                            )
                            self.logger.info(f"    子思维会话 '{resolved_target_conversation_id}' 已激活。")

                        elif directive_action_type == "DEACTIVATE_SESSION":
                            self.chat_session_manager.deactivate_session(resolved_target_conversation_id)
                            self.logger.info(f"    子思维会话 '{resolved_target_conversation_id}' 已停用。")

                        elif directive_action_type == "SET_CHAT_STYLE":
                             style_details_dict = directive_item_dict.get("style_details")
                             if isinstance(style_details_dict, dict):
                                self.chat_session_manager.set_chat_style_directives(
                                    conversation_id=resolved_target_conversation_id,
                                    directives=style_details_dict
                                )
                                self.logger.info(f"    为会话 '{resolved_target_conversation_id}' 设置聊天风格: {style_details_dict}")
                             else:
                                 self.logger.warning(f"    SET_CHAT_STYLE 指令的 style_details 格式不正确或缺失 (会话: {resolved_target_conversation_id})。")
                        else:
                            self.logger.warning(
                                f"    未知的子思维指令类型: {directive_action_type} (目标会话: {resolved_target_conversation_id})"
                            )
                    else:
                        self.logger.warning(
                            f"    子思维指令格式不正确（缺少conversation_id或directive_type）: {directive_item_dict}"
                        )
                else:
                     self.logger.warning(f"    子思维指令列表中的项目不是字典: {directive_item_dict}")
        else:
            self.logger.debug(f"主思维循环 {loop_count}: 本轮没有子思维指令。")

        return saved_thought_doc_key, background_action_tasks