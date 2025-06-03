import asyncio # 异步IO库
import datetime # 日期时间处理
import json # JSON 数据处理
import random # 随机数生成
import re # 正则表达式操作
import threading # 线程相关（用于 stop_event）
import uuid # 生成唯一ID
import os
from typing import Any, Dict, List, Optional, Tuple, Coroutine # 类型提示

# --- 从项目中导入必要的模块和类 ---

# 动作处理器
from src.action.action_handler import ActionHandler

# 日志管理器
from src.common.custom_logging.logger_manager import get_logger

# 受保护的任务执行器
from src.common.protected_runner import (
    execute_protected_task_with_polling,
    TaskTimeoutError,
    TaskCancelledByExternalEventError
)

# 聊天记录格式化工具
from src.common.utils import format_chat_history_for_prompt

# 配置相关的类
from src.config.alcarus_configs import (
    AlcarusRootConfig,
    CoreLogicSettings, # 你在 _core_thinking_loop 中用到了
    PersonaSettings # 你在 CorePromptBuilder 和其他地方用到了
    # LLMClientSettings # 这个在 main.py 里好像没直接用，可以根据需要保留或移除
)
# LLM 客户端 (使用别名 ProcessorClient，因为 main.py 内部用的是这个名字)
from src.llmrequest.llm_processor import Client as ProcessorClient

# 核心通信层接口
from src.core_communication.core_ws_server import CoreWebsocketServer

# 数据库处理器
from src.database.arangodb_handler import ArangoDBHandler

# 子意识/聊天会话管理器
from src.sub_consciousness.chat_session_handler import ChatSessionManager

# 侵入性思维生成器 (这是相对导入，因为 intrusive_thoughts.py 和 main.py 在同一个 core_logic 包里)
from .intrusive_thoughts import IntrusiveThoughtsGenerator

from src.config.config_manager import get_typed_settings


# 获取日志记录器实例，用于在本模块中记录日志
logger = get_logger("AIcarusCore.CoreLogic")# 日志记录器，命名空间为 AIcarusCore.CoreLogic

# --- 辅助类定义 ---

class CorePromptBuilder:
    """
    负责构建主思维LLM调用所需的System Prompt和User Prompt。
    """
    def __init__(self, core_logic_instance: 'CoreLogic'):
        """
        初始化 Prompt 构建器。

        Args:
            core_logic_instance: CoreLogic 类的实例，用于访问配置和状态。
        """
        self.core_logic = core_logic_instance # 保存 CoreLogic 实例的引用
        self.logger = core_logic_instance.logger # 使用 CoreLogic 的日志记录器
        self.root_cfg = core_logic_instance.root_cfg # 访问根配置
        self.initial_state = core_logic_instance.INITIAL_STATE # 访问初始状态常量
        self.prompt_template_str = core_logic_instance.PROMPT_TEMPLATE # 访问用户Prompt模板

    def build_system_prompt(self, current_time_str: str) -> str:
        """
        构建 System Prompt。

        Args:
            current_time_str: 当前格式化后的时间字符串。

        Returns:
            构建完成的 System Prompt 字符串。
        """
        if not self.root_cfg: # 检查根配置是否存在
            self.logger.error("构建System Prompt失败：根配置 (root_cfg) 不可用。") # 记录错误
            return "错误：无法加载人格设定。" # 返回错误提示

        persona_cfg: PersonaSettings = self.root_cfg.persona # 获取人格配置
        
        system_prompt_parts: List[str] = [ # 初始化System Prompt的各个部分列表
            f"当前时间：{current_time_str}", # 添加当前时间
            f"你是{persona_cfg.bot_name}；", # 添加机器人名称
            persona_cfg.description, # 添加机器人描述
            persona_cfg.profile, # 添加机器人档案/侧写
        ]
        system_prompt_str: str = "\n".join(filter(None, system_prompt_parts)) # 将各部分用换行符连接，并过滤空字符串
        
        self.logger.debug(f"--- 主思维LLM接收到的 System Prompt ---\n{system_prompt_str}\n--- System Prompt结束 ---") # 记录构建的System Prompt
        return system_prompt_str # 返回构建的System Prompt

    def build_user_prompt(
        self,
        current_state_for_prompt: Dict[str, Any],
        intrusive_thought_str: str = ""
    ) -> str:
        """
        构建 User Prompt。

        Args:
            current_state_for_prompt: 包含当前状态信息的字典，用于填充模板。
            intrusive_thought_str: 当前周期要注入的侵入性思维字符串。

        Returns:
            构建完成的 User Prompt 字符串。
        """
        task_info_for_template: str = current_state_for_prompt.get( # 获取当前任务信息
            "current_task_info_for_prompt", # 键名
            "你当前没有什么特定的目标或任务。" # 默认值
        )
        
        try:
            user_prompt_str: str = self.prompt_template_str.format( # 使用模板格式化User Prompt
                current_task_info=task_info_for_template, # 填充当前任务信息
                mood=current_state_for_prompt.get("mood", self.initial_state["mood"]), # 填充心情
                previous_thinking=current_state_for_prompt.get("previous_thinking", self.initial_state["previous_thinking"]), # 填充上一次思考
                thinking_guidance=current_state_for_prompt.get("thinking_guidance", self.initial_state["thinking_guidance"]), # 填充思考引导
                action_result_info=current_state_for_prompt.get("action_result_info", self.initial_state["action_result_info"]), # 填充动作结果
                pending_action_status=current_state_for_prompt.get("pending_action_status", self.initial_state["pending_action_status"]), # 填充待处理动作状态
                recent_contextual_information=current_state_for_prompt.get("recent_contextual_information", self.initial_state["recent_contextual_information"]), # 填充最近上下文
                active_sub_mind_latest_activity=current_state_for_prompt.get("active_sub_mind_latest_activity", self.initial_state["active_sub_mind_latest_activity"]), # 填充活跃子思维动态
                intrusive_thought=intrusive_thought_str, # 填充侵入性思维
            )
        except KeyError as e_key_error: # 捕获格式化时可能发生的KeyError
            self.logger.error(f"构建主思维User Prompt时发生KeyError: {e_key_error}。请检查PROMPT_TEMPLATE和current_state_for_prompt的键是否匹配。") # 记录错误
            self.logger.error(f"当前的 current_state_for_prompt 键: {list(current_state_for_prompt.keys())}") # 记录当前状态字典的键
            # 返回一个包含错误信息的Prompt，以便LLM知道发生了什么
            return f"错误：构建User Prompt失败，因为模板变量不匹配。错误详情: {e_key_error}"
        
        self.logger.debug(f"--- 主思维LLM接收到的 User Prompt (截断) ---\n{user_prompt_str[:1500]}...\n--- User Prompt结束 ---") # 记录构建的User Prompt（截断显示）
        return user_prompt_str # 返回构建的User Prompt

class CoreThoughtProcessor:
    """
    负责处理主思维LLM返回的思考结果。
    """
    def __init__(self, core_logic_instance: 'CoreLogic'):
        """
        初始化思考处理器。

        Args:
            core_logic_instance: CoreLogic 类的实例，用于访问依赖的处理器和配置。
        """
        self.core_logic = core_logic_instance # 保存 CoreLogic 实例的引用
        self.logger = core_logic_instance.logger # 使用 CoreLogic 的日志记录器
        self.db_handler = core_logic_instance.db_handler # 访问数据库处理器
        self.action_handler_instance = core_logic_instance.action_handler_instance # 访问动作处理器
        self.chat_session_manager = core_logic_instance.chat_session_manager # 访问聊天会话管理器
        self.core_comm_layer = core_logic_instance.core_comm_layer # 访问核心通信层

    async def process_thought_and_actions(
        self,
        generated_thought_json: Dict[str, Any],
        current_state_for_prompt: Dict[str, Any], # 用于构建保存到数据库的文档
        current_time_formatted_str: str, # 用于构建保存到数据库的文档
        system_prompt_sent: Optional[str], # 用于构建保存到数据库的文档
        full_prompt_text_sent: Optional[str], # 用于构建保存到数据库的文档
        intrusive_thought_to_inject_this_cycle: str, # 用于构建保存到数据库的文档
        formatted_recent_contextual_info: str, # 用于构建保存到数据库的文档
        action_id_whose_result_was_shown_in_last_prompt: Optional[str],
        loop_count: int # 当前循环轮次，用于日志
    ) -> Tuple[Optional[str], List[asyncio.Task]]: # 返回保存的思考文档Key和后台动作任务列表
        """
        处理LLM生成的思考结果，包括保存思考、处理动作、处理子思维指令。

        Args:
            generated_thought_json: LLM返回并已解析的JSON对象。
            current_state_for_prompt: 用于构建数据库文档的当前状态信息。
            current_time_formatted_str: 当前格式化时间。
            system_prompt_sent: 发送给LLM的System Prompt。
            full_prompt_text_sent: 发送给LLM的User Prompt。
            intrusive_thought_to_inject_this_cycle: 本轮注入的侵入性思维。
            formatted_recent_contextual_info: 格式化的最近上下文信息。
            action_id_whose_result_was_shown_in_last_prompt: 上一个在Prompt中显示了结果的动作ID。
            loop_count: 当前主思维循环的轮次。

        Returns:
            一个元组，包含 (保存的思考文档的_key, 后台动作任务列表)。
        """
        self.logger.info(f"主思维循环 {loop_count}: LLM成功返回思考结果。正在处理...") # 记录日志
        
        initiated_action_data_for_db: Optional[Dict[str, Any]] = None # 初始化将存入数据库的动作数据
        action_info_for_task_processing: Optional[Dict[str, Any]] = None # 初始化用于异步任务处理的动作信息
        saved_thought_doc_key: Optional[str] = None # 初始化保存的思考文档的key
        background_action_tasks: List[asyncio.Task] = [] # 初始化后台动作任务列表

        think_output_text = generated_thought_json.get("think") or "未思考" # 获取思考内容
        self.logger.info(f"主思维循环 {loop_count}: 解析后的思考内容: '{think_output_text[:50]}...'") # 记录思考内容（截断）

        # 1. 构建要保存到数据库的思考文档
        document_to_save_in_main_db: Dict[str, Any] = { # 构建文档字典
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), # 时间戳
            "time_injected_to_prompt": current_time_formatted_str, # Prompt中的时间
            "system_prompt_sent": system_prompt_sent if system_prompt_sent else "System Prompt 未能构建", # System Prompt
            "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle, # 注入的侵入性思维
            "mood_input": current_state_for_prompt.get("mood"), # 输入的心情
            "previous_thinking_input": current_state_for_prompt.get("previous_thinking"), # 输入的上一次思考
            "thinking_guidance_input": current_state_for_prompt.get("thinking_guidance"), # 输入的思考引导
            "task_input_info": current_state_for_prompt.get("current_task_info_for_prompt", "无特定任务输入"), # 输入的任务信息
            "action_result_input": current_state_for_prompt.get("action_result_info", ""), # 输入的动作结果
            "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""), # 输入的待处理动作状态
            "recent_contextual_information_input": formatted_recent_contextual_info, # 输入的最近上下文
            "active_sub_mind_latest_activity_input": current_state_for_prompt.get("active_sub_mind_latest_activity"), # 输入的活跃子思维动态
            "full_user_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "User Prompt 未能构建", # User Prompt
            "think_output": generated_thought_json.get("think"), # 输出的思考
            "emotion_output": generated_thought_json.get("emotion"), # 输出的情绪
            "next_think_output": generated_thought_json.get("next_think"), # 输出的下一步思考方向
            "to_do_output": generated_thought_json.get("to_do", ""), # 输出的待办事项
            "done_output": generated_thought_json.get("done", False), # 输出的是否完成
            "action_to_take_output": generated_thought_json.get("action_to_take", ""), # 输出的要执行的动作
            "action_motivation_output": generated_thought_json.get("action_motivation", ""), # 输出的动作动机
            "sub_mind_directives_output": generated_thought_json.get("sub_mind_directives"), # 输出的子思维指令
        }

        # 2. 处理外部动作意图
        action_description_from_llm_raw = generated_thought_json.get("action_to_take") # 获取原始动作描述
        action_description_from_llm_clean = action_description_from_llm_raw.strip() \
            if isinstance(action_description_from_llm_raw, str) else "" # 清理动作描述

        action_motivation_from_llm_raw = generated_thought_json.get("action_motivation") # 获取原始动作动机
        action_motivation_from_llm_clean = action_motivation_from_llm_raw.strip() \
            if isinstance(action_motivation_from_llm_raw, str) else "" # 清理动作动机

        if action_description_from_llm_clean: # 如果有有效的动作描述
            current_action_id = str(uuid.uuid4()) # 生成唯一动作ID
            initiated_action_data_for_db = { # 构建用于数据库的动作数据
                "action_description": action_description_from_llm_clean,
                "action_motivation": action_motivation_from_llm_clean,
                "action_id": current_action_id,
                "status": "PENDING", # 初始状态为待处理
                "result_seen_by_shuang": False, # 是否已被主思维“看到”结果
                "initiated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), # 动作发起时间
            }
            action_info_for_task_processing = { # 构建用于异步任务处理的动作信息
                "action_id": current_action_id,
                "action_description": action_description_from_llm_clean,
                "action_motivation": action_motivation_from_llm_clean,
                "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"), # 当前思考上下文
            }
            self.logger.info( # 记录日志
                f"  >>> 外部行动意图产生: '{action_description_from_llm_clean}' "
                f"(ID: {current_action_id[:8]})"
            )
        
        document_to_save_in_main_db["action_attempted"] = initiated_action_data_for_db # 将动作数据加入主思考文档

        if "_llm_usage_info" in generated_thought_json: # 如果LLM响应包含用量信息
            document_to_save_in_main_db["_llm_usage_info"] = generated_thought_json["_llm_usage_info"] # 加入主思考文档

        # 3. 保存思考文档到数据库
        if self.db_handler: # 检查数据库处理器是否存在
            try:
                saved_thought_doc_key = await self.db_handler.save_thought_document(document_to_save_in_main_db) # 保存文档
                self.logger.info(f"主思维循环 {loop_count}: 思考文档 (Key: {saved_thought_doc_key}) 已保存。") # 记录日志
            except Exception as e_save_thought: # 捕获保存异常
                 self.logger.error(f"主思维循环 {loop_count}: 保存思考文档失败: {e_save_thought}", exc_info=True) # 记录错误
        else:
            self.logger.error(f"主思维循环 {loop_count}: 数据库处理器 (db_handler) 未初始化，无法保存思考文档。")


        # 4. 标记上一个动作的结果为已阅 (只有在本次LLM思考成功后才标记)
        if action_id_whose_result_was_shown_in_last_prompt and self.db_handler:
            try:
                await self.db_handler.mark_action_result_as_seen(action_id_whose_result_was_shown_in_last_prompt) # 标记已阅
                self.logger.info(f"主思维循环 {loop_count}: 动作结果 (ID: {action_id_whose_result_was_shown_in_last_prompt[:8]}) 已标记为已阅。") # 记录日志
            except Exception as e_mark_seen: # 捕获标记异常
                self.logger.error(f"主思维循环 {loop_count}: 标记动作结果为已阅时失败: {e_mark_seen}", exc_info=True) # 记录错误

        # 5. 如果有动作需要处理，则异步启动动作处理流程
        if action_info_for_task_processing and saved_thought_doc_key and self.action_handler_instance:
            self.logger.info(f"主思维循环 {loop_count}: 准备异步处理动作 ID: {action_info_for_task_processing['action_id'][:8]}。") # 记录日志
            action_processing_task: asyncio.Task = asyncio.create_task( # 创建异步任务
                self.action_handler_instance.process_action_flow( # 调用动作处理器的流程
                    action_id=action_info_for_task_processing["action_id"],
                    doc_key_for_updates=saved_thought_doc_key, # 关联的思考文档key，用于更新动作状态
                    action_description=action_info_for_task_processing["action_description"],
                    action_motivation=action_info_for_task_processing["action_motivation"],
                    current_thought_context=action_info_for_task_processing["current_thought_context"],
                )
            )
            background_action_tasks.append(action_processing_task) # 将任务添加到列表
            # action_processing_task.add_done_callback(background_action_tasks.discard) # 这个回调方式可能导致集合在迭代时修改的问题，直接append更安全
            self.logger.info( # 记录日志
                f"      外部动作 '{action_info_for_task_processing['action_description']}' "
                f"(ID: {action_info_for_task_processing['action_id'][:8]}, "
                f"关联思考DocKey: {saved_thought_doc_key}) 已异步启动处理。"
            )
        elif action_info_for_task_processing and not saved_thought_doc_key: # 如果有动作但思考文档保存失败
            self.logger.error(
                f"主思维循环 {loop_count}: 未能获取保存思考文档的 _key，无法为外部动作 ID "
                f"{action_info_for_task_processing['action_id']} 创建处理任务。"
            )
        elif action_info_for_task_processing and not self.action_handler_instance: # 如果有动作但动作处理器未初始化
            self.logger.error(
                f"主思维循环 {loop_count}: ActionHandler 未初始化，无法为外部动作 ID "
                f"{action_info_for_task_processing['action_id']} 创建处理任务。"
            )
        
        # 6. 处理对子思维的指令
        sub_mind_directives_list = generated_thought_json.get("sub_mind_directives") # 获取子思维指令列表
        if isinstance(sub_mind_directives_list, list) and self.chat_session_manager: # 检查是否为列表且会话管理器存在
            self.logger.info(f"主思维循环 {loop_count}: 开始处理 {len(sub_mind_directives_list)} 条子思维指令。") # 记录日志
            for directive_item_dict in sub_mind_directives_list: # 遍历指令
                if isinstance(directive_item_dict, dict): # 检查指令是否为字典
                    target_conversation_id_from_llm = directive_item_dict.get("conversation_id") # 获取目标会话ID
                    directive_action_type = directive_item_dict.get("directive_type") # 获取指令类型
                    
                    if target_conversation_id_from_llm and directive_action_type: # 确保ID和类型存在
                        # --- 解析和转换 conversation_id ---
                        # (这里的转换逻辑与我们上一轮调试时加入的逻辑一致)
                        resolved_target_conversation_id = target_conversation_id_from_llm
                        if not ("_group_" in target_conversation_id_from_llm or "_dm_" in target_conversation_id_from_llm):
                            self.logger.warning(f"  子思维指令中的 conversation_id '{target_conversation_id_from_llm}' 看起来不是内部完整格式。尝试转换...")
                            if target_conversation_id_from_llm.isdigit():
                                _platform = "napcat_qq" # 应该动态获取或从配置获取
                                potential_group_id = f"{_platform}_group_{target_conversation_id_from_llm}"
                                # 检查此ID是否存在于ChatSessionManager中
                                if self.chat_session_manager.get_session(potential_group_id): # 假设有get_session方法
                                    resolved_target_conversation_id = potential_group_id
                                    self.logger.info(f"  已将LLM的短ID '{target_conversation_id_from_llm}' 解析为群聊ID: {resolved_target_conversation_id}")
                                else:
                                    self.logger.warning(f"  无法将短ID '{target_conversation_id_from_llm}' 转换为已知的群聊会话ID (平台:{_platform})。将尝试使用原始ID。")
                            else:
                                self.logger.debug(f"  LLM提供的会话ID '{target_conversation_id_from_llm}' 不是纯数字，将尝试直接使用。")
                        
                        main_thought_for_sub_mind_injection = directive_item_dict.get( # 获取注入给子思维的主导思想
                            "main_thought_for_reply", 
                            generated_thought_json.get("think") # 默认使用当前轮次的思考
                        )
                        self.logger.debug(f"  处理指令: 类型='{directive_action_type}', 目标会话='{resolved_target_conversation_id}', 引导思想='{str(main_thought_for_sub_mind_injection)[:30]}...'")
                        
                        if directive_action_type == "TRIGGER_REPLY": # 如果是触发回复指令
                            if self.chat_session_manager: # 再次确认
                                core_action_from_sub_mind = await self.chat_session_manager.trigger_session_reply(
                                    conversation_id=resolved_target_conversation_id, 
                                    main_thought_context=main_thought_for_sub_mind_injection
                                )
                                if core_action_from_sub_mind and self.core_comm_layer: # 如果子思维生成了回复且通信层存在
                                    self.logger.info(f"    子思维回复动作将发送给适配器 (会话: {resolved_target_conversation_id})。") # 记录日志
                                    await self.core_comm_layer.broadcast_action_to_adapters(core_action_from_sub_mind) # 广播动作
                                elif core_action_from_sub_mind: # 如果生成了回复但通信层不存在
                                    self.logger.warning(f"    子思维生成了回复动作，但核心通信层未设置，无法发送 (会话: {resolved_target_conversation_id})。") # 记录警告
                                else: # 如果子思维未生成回复
                                    self.logger.info(f"    子思维未生成回复动作 (会话: {resolved_target_conversation_id})。") # 记录日志
                        
                        elif directive_action_type == "ACTIVATE_SESSION": # 如果是激活会话指令
                            if self.chat_session_manager: # 再次确认
                                self.chat_session_manager.activate_session(
                                    conversation_id=resolved_target_conversation_id, 
                                    main_thought_context=main_thought_for_sub_mind_injection
                                )
                                self.logger.info(f"    子思维会话 '{resolved_target_conversation_id}' 已激活。") # 记录日志
                        
                        elif directive_action_type == "DEACTIVATE_SESSION": # 如果是停用会话指令
                            if self.chat_session_manager: # 再次确认
                                self.chat_session_manager.deactivate_session(resolved_target_conversation_id)
                                self.logger.info(f"    子思维会话 '{resolved_target_conversation_id}' 已停用。") # 记录日志
                        
                        elif directive_action_type == "SET_CHAT_STYLE": # 如果是设置聊天风格指令
                             style_details_dict = directive_item_dict.get("style_details") # 获取风格详情
                             if isinstance(style_details_dict, dict) and self.chat_session_manager: # 检查是否为字典且管理器存在
                                self.chat_session_manager.set_chat_style_directives(
                                    conversation_id=resolved_target_conversation_id, 
                                    directives=style_details_dict
                                )
                                self.logger.info(f"    为会话 '{resolved_target_conversation_id}' 设置聊天风格: {style_details_dict}") # 记录日志
                             else: # 如果风格详情格式不正确
                                 self.logger.warning(f"    SET_CHAT_STYLE 指令的 style_details 格式不正确或缺失 (会话: {resolved_target_conversation_id})。") # 记录警告
                        else: # 未知指令类型
                            self.logger.warning(
                                f"    未知的子思维指令类型: {directive_action_type} (目标会话: {resolved_target_conversation_id})"
                            ) # 记录警告
                    else: # 指令格式不正确（缺少ID或类型）
                        self.logger.warning(
                            f"    子思维指令格式不正确（缺少conversation_id或directive_type）: {directive_item_dict}"
                        ) # 记录警告
                else: # 指令列表中的项目不是字典
                     self.logger.warning(f"    子思维指令列表中的项目不是字典: {directive_item_dict}") # 记录警告
        else: # 没有子思维指令
            self.logger.debug(f"主思维循环 {loop_count}: 本轮没有子思维指令。") # 记录日志

        return saved_thought_doc_key, background_action_tasks # 返回保存的文档key和后台任务列表

# --- CoreLogic 主类定义 ---

class CoreLogic:
    """
    AIcarus 的核心逻辑处理单元。
    负责主思考循环、状态管理、与LLM的交互以及协调其他模块。
    """
    # 主思维的User Prompt模板字符串
    PROMPT_TEMPLATE: str = """\
你当前的目标/任务是：【{current_task_info}】

{action_result_info}

{pending_action_status}

{recent_contextual_information}

{active_sub_mind_latest_activity}

你的上一轮思考是：{previous_thinking}；

你现在的心情大概是：{mood}；

经过你上一轮的思考，你目前打算的思考方向是：{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。
如果你希望与某个聊天会话的“子思维”进行交互（例如，让它回复消息、激活它、休眠它），请在 sub_mind_directives字段中描述你的指令。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则设为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "sub_mind_directives": [
        {{
            "conversation_id": "string, 目标会话的ID",
            "directive_type": "string, 指令类型，例如 'TRIGGER_REPLY', 'ACTIVATE_SESSION', 'DEACTIVATE_SESSION', 'SET_CHAT_STYLE'",
            "main_thought_for_reply": "string, 【可选】仅当 directive_type 为 TRIGGER_REPLY 或 ACTIVATE_SESSION 时，主思维希望注入给子思维的当前想法上下文",
            "style_details": {{}} "object, 【可选】仅当 directive_type 为 SET_CHAT_STYLE 时，具体的风格指令"
        }}
    ],
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON："""
    
    
    # 主思维的初始状态，用于第一次思考或数据库中没有历史记录时
    INITIAL_STATE: Dict[str, Any] = {
        "mood": "平静。", # 初始心情
        "previous_thinking": "这是你的第一次思考，请开始吧。", # 初始的上一次思考
        "thinking_guidance": "随意发散一下吧。", # 初始的思考引导
        "current_task_info_for_prompt": "你当前没有什么特定的目标或任务。", # 初始任务信息
        "action_result_info": "你上一轮没有执行产生结果的特定行动。", # 初始动作结果
        "pending_action_status": "", # 初始待处理动作状态
        "recent_contextual_information": "最近未感知到任何特定信息或通知。", # 初始上下文信息
        "active_sub_mind_latest_activity": "目前没有活跃的子思维会话，或者它们最近没有活动。", # 初始子思维动态
    }

    def __init__(
        self,
        root_cfg: AlcarusRootConfig,
        db_handler: ArangoDBHandler,
        main_consciousness_llm_client: ProcessorClient,
        intrusive_thoughts_llm_client: ProcessorClient,
        sub_mind_llm_client: ProcessorClient,
        action_decision_llm_client: Optional[ProcessorClient], 
        information_summary_llm_client: Optional[ProcessorClient],
        chat_session_manager: Optional[ChatSessionManager] = None,
        core_comm_layer: Optional[CoreWebsocketServer] = None,
    ):
        self.logger = logger 
        self.root_cfg: AlcarusRootConfig = root_cfg 
        self.db_handler: ArangoDBHandler = db_handler 
        
        self.main_consciousness_llm_client: ProcessorClient = main_consciousness_llm_client 
        self.intrusive_thoughts_llm_client: ProcessorClient = intrusive_thoughts_llm_client 
        self.sub_mind_llm_client: ProcessorClient = sub_mind_llm_client 
        
        self.chat_session_manager: Optional[ChatSessionManager] = chat_session_manager # 类型改为 Optional
        self.core_comm_layer: Optional[CoreWebsocketServer] = core_comm_layer

        # VVVV 把这三个事件的定义挪到这里，提到前面来！ VVVV
        self.stop_event: threading.Event = threading.Event()
        self.async_stop_event: asyncio.Event = asyncio.Event()
        self.sub_mind_update_event: asyncio.Event = asyncio.Event()
        # ^^^^ 事件定义结束 ^^^^

        # 初始化动作处理器 (确保这部分也正确了，只传 root_cfg, 然后用 set_dependencies)
        self.action_handler_instance: Optional[ActionHandler] = None
        if hasattr(self.root_cfg, 'action_handler_settings') and self.root_cfg.action_handler_settings and self.root_cfg.action_handler_settings.enabled:
            self.logger.info("ActionHandler 配置为启用，正在初始化...")
            self.action_handler_instance = ActionHandler(root_cfg=self.root_cfg)
            self.action_handler_instance.set_dependencies(
                db_handler=self.db_handler,
                comm_layer=self.core_comm_layer
            )
            self.logger.info("ActionHandler 已配置并设置了依赖。其LLM客户端将在首次使用时初始化。")
        elif hasattr(self.root_cfg, 'action_handler_settings') and self.root_cfg.action_handler_settings and not self.root_cfg.action_handler_settings.enabled:
            self.logger.info("ActionHandler 在配置中被禁用，将不会被初始化。")
        else:
             self.logger.warning("警告：在 root_cfg 中未找到 action_handler_settings 配置。ActionHandler 将不会被初始化。")

        # 初始化侵入性思维生成器
        self.intrusive_generator_instance: Optional[IntrusiveThoughtsGenerator] = None
        if hasattr(self.root_cfg, 'intrusive_thoughts_module_settings') and \
           self.root_cfg.intrusive_thoughts_module_settings and \
           self.root_cfg.intrusive_thoughts_module_settings.enabled:
            
            self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                llm_client=self.intrusive_thoughts_llm_client, 
                db_handler=self.db_handler, 
                persona_cfg=self.root_cfg.persona, 
                module_settings=self.root_cfg.intrusive_thoughts_module_settings, 
                stop_event=self.stop_event # <--- 现在 self.stop_event 已经定义好了！
            )
            self.logger.info("IntrusiveThoughtsGenerator 已成功初始化。") 
        elif hasattr(self.root_cfg, 'intrusive_thoughts_module_settings') and \
             self.root_cfg.intrusive_thoughts_module_settings and \
             not self.root_cfg.intrusive_thoughts_module_settings.enabled:
            self.logger.info("IntrusiveThoughtsGenerator 在配置中被禁用，将不会被初始化。")
        else:
            self.logger.warning("警告：在 root_cfg 中未找到 intrusive_thoughts_module_settings 配置或其 enabled 状态。IntrusiveThoughtsGenerator 将不会被初始化。")
            
        # 初始化重构后的辅助类实例
        self.prompt_builder = CorePromptBuilder(self) 
        self.thought_processor = CoreThoughtProcessor(self) 

        # # 这几行已经挪到前面去了，这里就不用重复了
        # self.stop_event: threading.Event = threading.Event() 
        # self.async_stop_event: asyncio.Event = asyncio.Event() 
        # self.sub_mind_update_event: asyncio.Event = asyncio.Event()

        self.current_focused_conversation_id: Optional[str] = None 
        self.logger.info("CoreLogic instance created.")
        
    def _process_thought_and_action_state(
        self,
        latest_thought_document: Optional[Dict[str, Any]],
        formatted_recent_contextual_info: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        根据最新的思考文档和上下文信息，处理并准备用于下一个Prompt的状态。
        同时返回上一个在Prompt中展示了结果的动作ID。
        """
        current_state: Dict[str, Any] = {} # 初始化当前状态字典
        action_id_result_shown_in_prompt: Optional[str] = None # 初始化动作ID

        if latest_thought_document: # 如果存在上一次的思考文档
            self.logger.debug("使用数据库中的最新思考文档来构建当前状态。") # 记录日志
            current_state["mood"] = latest_thought_document.get("emotion_output", self.INITIAL_STATE["mood"]) # 获取心情
            current_state["previous_thinking"] = latest_thought_document.get("think_output", self.INITIAL_STATE["previous_thinking"]) # 获取上一次思考
            current_state["thinking_guidance"] = latest_thought_document.get("next_think_output", self.INITIAL_STATE["thinking_guidance"]) # 获取思考引导
            current_state["current_task"] = latest_thought_document.get("to_do_output", "") # 获取待办任务
            
            action_attempted = latest_thought_document.get("action_attempted") # 获取尝试过的动作
            if isinstance(action_attempted, dict): # 如果动作信息是字典
                action_status = action_attempted.get("status", "UNKNOWN") # 获取动作状态
                action_desc = action_attempted.get("action_description", "未知动作") # 获取动作描述
                action_id = action_attempted.get("action_id") # 获取动作ID

                if action_status == "PENDING": # 如果动作待处理
                    current_state["pending_action_status"] = f"你当前有一个待处理的行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'})。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"] # 无结果
                elif action_status in ["PROCESSING_DECISION", "TOOL_EXECUTING", "PROCESSING_SUMMARY"]: # 如果动作正在处理
                    current_state["pending_action_status"] = f"你当前正在处理行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'})，状态: {action_status}。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"] # 无结果
                elif action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]: # 如果动作已完成
                    result_for_shuang = action_attempted.get("final_result_for_shuang", "动作已完成，但没有具体结果文本。") # 获取给“双”的结果
                    current_state["action_result_info"] = (
                        f"你上一轮行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{result_for_shuang}】"
                    )
                    current_state["pending_action_status"] = "" # 无待处理动作
                    if action_id and not action_attempted.get("result_seen_by_shuang", False): # 如果结果未被“看到”
                        action_id_result_shown_in_prompt = action_id # 记录此ID，稍后将标记为已阅
                else: # 其他未知状态
                    current_state["pending_action_status"] = f"你上一轮的行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'}) 状态未知 ({action_status})。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
            else: # 如果没有尝试过的动作
                current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                current_state["pending_action_status"] = self.INITIAL_STATE["pending_action_status"]
        else: # 如果没有上一次的思考文档（例如首次运行）
            self.logger.info("最新的思考文档为空，主思维将使用初始思考状态。") # 记录日志
            current_state = self.INITIAL_STATE.copy() # 使用初始状态
            current_state["current_task"] = "" # 确保初始任务为空字符串，而不是None

        current_state["recent_contextual_information"] = formatted_recent_contextual_info # 设置最近上下文信息

        # 获取活跃子思维的最新动态 (与之前逻辑保持一致)
        if self.chat_session_manager: # 如果会话管理器存在
            # VVVV 就是下面这一行要改 VVVV
            active_sessions_summary = self.chat_session_manager.get_all_active_sessions_summary() # <--- 用这个正确的方法名！
            # ^^^^ 看清楚啦！ ^^^^
            
            # 🐾 小猫爪思考：get_all_active_sessions_summary() 返回的是一个列表 List[Dict[str, Any]]
            # 而 PROMPT_TEMPLATE 里的 {active_sub_mind_latest_activity} 期望的是一个字符串。
            # 所以这里需要把这个列表转换成一个适合放到Prompt里的字符串。
            # 比如，把每个会话的摘要简单地拼接起来。

            if active_sessions_summary:
                # 简单地将每个会话的摘要转换成字符串并用换行符连接
                # 你可能需要根据摘要的具体内容和Prompt的需求来调整这里的格式化方式
                summaries_str_parts = []
                for summary_item in active_sessions_summary:
                    # 例如，只取一部分关键信息，或者直接用 str(summary_item)
                    # 为了简单起见，我们先用 str()，但你最好根据摘要内容优化这里的显示
                    summaries_str_parts.append(f"- 会话ID {summary_item.get('conversation_id', '未知')}: 状态={'活跃' if summary_item.get('is_active') else '不活跃'}, 上次回复='{str(summary_item.get('last_reply_generated', '无'))[:30]}...'")
                current_state["active_sub_mind_latest_activity"] = "\n".join(summaries_str_parts)
            else:
                current_state["active_sub_mind_latest_activity"] = self.INITIAL_STATE["active_sub_mind_latest_activity"]
            
        else: # 如果会话管理器不存在
            current_state["active_sub_mind_latest_activity"] = self.INITIAL_STATE["active_sub_mind_latest_activity"] # 使用初始状态
        
        self.logger.debug(f"在 _process_thought_and_action_state 中：成功处理并返回用于Prompt的状态。Action ID shown: {action_id_result_shown_in_prompt}") # 记录日志
        return current_state, action_id_result_shown_in_prompt # 返回处理后的状态和动作ID

    async def _generate_thought_from_llm(
        self,
        llm_client: ProcessorClient,
        system_prompt_str: str, # 直接接收构建好的 System Prompt
        user_prompt_str: str,   # 直接接收构建好的 User Prompt
        cancellation_event: Optional[asyncio.Event] = None 
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        调用LLM生成思考结果。现在接收已构建好的prompts。
        集成了受保护的任务执行器。
        """
        self.logger.info(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考 (受保护的调用)...") # 记录日志
        if cancellation_event: # 加一段日志看看它的状态
            self.logger.debug(f"传递给 ProtectedRunner 的 cancellation_event 当前状态: {cancellation_event.is_set()}")


        raw_llm_response_text: str = "" # 初始化原始LLM响应文本
        try:
            # 构建 LLM 请求的协程
            llm_request_coro: Coroutine[Any, Any, Dict[str, Any]] = llm_client.make_llm_request( # 注意类型提示
                prompt=user_prompt_str, # 用户Prompt
                system_prompt=system_prompt_str, # 系统Prompt
                is_stream=False, # 非流式
            )

            # 使用受保护任务执行器来运行 LLM 请求
            llm_response_data: Dict[str, Any] = await execute_protected_task_with_polling( # 注意类型提示
                task_coro=llm_request_coro, # 要执行的协程
                task_description="主思维LLM思考生成", # 任务描述
                overall_timeout_seconds=self.root_cfg.core_logic_settings.llm_call_overall_timeout_seconds if self.root_cfg else 120.0, # 总体超时
                polling_interval_seconds=self.root_cfg.core_logic_settings.llm_call_polling_interval_seconds if self.root_cfg else 2.0, # 轮询间隔
                cancellation_event=cancellation_event # 取消事件
            )

            if llm_response_data.get("error"): # 如果LLM响应包含错误
                error_type = llm_response_data.get("type", "UnknownError") # 获取错误类型
                error_message = llm_response_data.get("message", "LLM客户端返回了一个错误") # 获取错误消息
                self.logger.error(f"主思维LLM调用失败 ({error_type}): {error_message}") # 记录错误
                if llm_response_data.get("details"): # 如果有错误详情
                    self.logger.error(f"  错误详情: {str(llm_response_data.get('details'))[:300]}...") # 记录详情
                return None, user_prompt_str, system_prompt_str # 返回None表示失败
            
            if llm_response_data.get("interrupted", False) or \
               llm_response_data.get("finish_reason", "").upper() in ["INTERRUPTED", "INTERRUPTED_BEFORE_CALL"]: # 如果任务被中断
                self.logger.warning(f"主思维LLM思考生成任务被中断。Reason: {llm_response_data.get('finish_reason', 'N/A')}") # 记录警告
                return None, user_prompt_str, system_prompt_str # 返回None

            raw_llm_response_text = llm_response_data.get("text") # 获取LLM响应文本
            if not raw_llm_response_text: # 如果文本为空
                error_message_no_text = "错误：主思维LLM响应中缺少文本内容。" # 定义错误消息
                if llm_response_data: # 如果有响应数据
                    error_message_no_text += f"\n  完整响应: {str(llm_response_data)[:500]}..." # 附加完整响应（截断）
                self.logger.error(error_message_no_text) # 记录错误
                return None, user_prompt_str, system_prompt_str # 返回None

            json_string_to_parse = raw_llm_response_text.strip() # 清理JSON字符串
            if json_string_to_parse.startswith("```json"): # 移除Markdown代码块标记
                json_string_to_parse = json_string_to_parse[7:-3].strip() # 移除```json 和 ```
            elif json_string_to_parse.startswith("```"): # 移除```
                 json_string_to_parse = json_string_to_parse[3:-3].strip() # 移除```
            
            json_string_to_parse = re.sub(r"[,\s]+(?=\}$)", "}", json_string_to_parse) # 清理花括号前的悬空逗号
            json_string_to_parse = re.sub(r",\s*$", "", json_string_to_parse) # 清理末尾的悬空逗号

            parsed_thought_json: Dict[str, Any] = json.loads(json_string_to_parse) # 解析JSON
            self.logger.info("主思维LLM API 响应已成功解析为JSON。") # 记录日志

            if llm_response_data.get("usage"): # 如果有用量信息
                parsed_thought_json["_llm_usage_info"] = llm_response_data["usage"] # 添加到解析结果中

            return parsed_thought_json, user_prompt_str, system_prompt_str # 返回解析结果和Prompts

        except TaskTimeoutError as e_task_timeout: # 捕获任务超时错误
            self.logger.error(f"错误：主思维LLM思考生成任务超时: {e_task_timeout}") # 记录错误
            return None, user_prompt_str, system_prompt_str # 返回None
        except TaskCancelledByExternalEventError as e_task_cancelled: # 捕获外部事件取消错误
            self.logger.warning(f"主思维LLM思考生成任务被外部事件取消: {e_task_cancelled}") # 记录警告
            return None, user_prompt_str, system_prompt_str # 返回None
        except json.JSONDecodeError as e_json: # 捕获JSON解析错误
            self.logger.error(f"错误：解析主思维LLM的JSON响应失败: {e_json}") # 记录错误
            self.logger.error(f"未能解析的文本内容: {raw_llm_response_text}") # 记录原始文本
            return None, user_prompt_str, system_prompt_str # 返回None
        except Exception as e_unexpected: # 捕获其他意外错误
            self.logger.error(
                f"错误：调用主思维LLM或处理其响应时发生意外错误: {e_unexpected}", exc_info=True
            ) # 记录错误
            return None, user_prompt_str, system_prompt_str # 返回None

    async def _core_thinking_loop(self) -> None:
        """
        核心思考循环。主思维在这里不断地感知、思考、决策。
        集成了受保护的LLM调用和中断逻辑。
        使用了 CorePromptBuilder 和 CoreThoughtProcessor 进行职责分离。
        """
        # ... (这部分与我们上一轮调试后的 _core_thinking_loop 逻辑非常相似，
        # 主要区别在于调用 self.prompt_builder 和 self.thought_processor) ...
        if not all([self.root_cfg,
                    self.db_handler,
                    self.main_consciousness_llm_client,
                    self.chat_session_manager,
                    self.prompt_builder, # 确保新组件已初始化
                    self.thought_processor]): # 确保新组件已初始化
            self.logger.critical("核心思考循环无法启动：一个或多个核心组件未初始化。") # 记录严重错误
            return # 退出

        action_id_whose_result_was_shown_in_last_prompt: Optional[str] = None # 初始化
        main_llm_cancellation_event = asyncio.Event() # 主LLM调用的取消事件

        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings # 获取核心逻辑配置
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒" # 时间格式
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds # 思考间隔
        chat_history_duration_minutes: int = getattr(core_logic_cfg, "chat_history_context_duration_minutes", 10) # 聊天历史时长
        polling_interval_seconds: float = getattr(core_logic_cfg, "main_loop_polling_interval_seconds", 0.1) # 主循环轮询间隔

        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 的意识开始流动 (重构版 V1) ---") # 记录日志
        loop_count: int = 0 # 初始化循环计数
        current_main_llm_task: Optional[asyncio.Task[Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]]] = None # 当前LLM任务

        while not self.async_stop_event.is_set(): # 使用异步停止事件
            loop_count += 1 # 循环计数增加
            self.logger.info(f"主思维循环第 {loop_count} 次迭代开始。") # 记录日志
            current_time_formatted_str: str = datetime.datetime.now().strftime(time_format_str) # 获取当前格式化时间
            
            main_llm_cancellation_event.clear() # 清除LLM取消事件

            should_proceed_with_llm_thought: bool = False # 是否进行LLM思考的标志
            
            # --- 等待触发机制 ---
            if current_main_llm_task and not current_main_llm_task.done(): # 如果上轮LLM任务仍在进行
                self.logger.info(f"主思维循环 {loop_count}: 上一轮的主LLM思考任务仍在进行中。优先等待子思维事件或停止信号。") # 记录日志
                
                # 创建等待子思维事件和停止事件的任务
                sub_mind_wait_task = asyncio.create_task(self.sub_mind_update_event.wait(), name="sub_mind_wait_during_llm")
                stop_event_wait_task = asyncio.create_task(self.async_stop_event.wait(), name="stop_event_wait_during_llm")
                
                done_tasks, pending_tasks = await asyncio.wait( # 等待任一事件完成
                    [sub_mind_wait_task, stop_event_wait_task, current_main_llm_task], # 同时等待LLM任务，看它是否会先完成
                    return_when=asyncio.FIRST_COMPLETED,
                    # timeout=polling_interval_seconds # 短暂超时以允许检查
                )

                if self.async_stop_event.is_set(): # 如果检测到停止信号
                    self.logger.info(f"主思维循环 {loop_count}: 检测到停止信号，将取消正在进行的LLM任务并退出。") # 记录日志
                    main_llm_cancellation_event.set() # 设置取消LLM的事件
                    if current_main_llm_task and not current_main_llm_task.done(): # 如果LLM任务仍在运行
                        current_main_llm_task.cancel() # 直接取消任务
                        try:
                            await current_main_llm_task # 等待取消完成
                        except asyncio.CancelledError:
                            self.logger.info(f"主思维循环 {loop_count}: 正在进行的LLM任务已取消。") # 记录日志
                        except Exception as e_cancel:
                            self.logger.error(f"主思维循环 {loop_count}: 取消LLM任务时出错: {e_cancel}", exc_info=True)
                    break # 退出主循环

                triggered_by_sub_mind_while_llm_active = False
                if sub_mind_wait_task in done_tasks: # 如果是子思维事件触发
                     self.logger.info(f"主思维循环 {loop_count}: 子思维事件触发，当前LLM思考将被尝试中断，并立即处理新状态。") # 记录日志
                     main_llm_cancellation_event.set() # 设置取消LLM的事件
                     self.sub_mind_update_event.clear() # 清除事件
                     should_proceed_with_llm_thought = True # 标记需要进行（可能被中断的）LLM思考以更新状态
                     triggered_by_sub_mind_while_llm_active = True
                     if current_main_llm_task in pending_tasks and not current_main_llm_task.done(): # 如果LLM任务仍在运行
                         current_main_llm_task.cancel() # 尝试取消它
                         # 不在此处 await current_main_llm_task，让它在后台取消

                if current_main_llm_task in done_tasks: # 如果LLM任务自行完成了
                    self.logger.info(f"主思维循环 {loop_count}: LLM思考任务在优先等待期间自行完成。") # 记录日志
                    # 结果将在后续的 LLM 思考与后续处理部分被获取和处理
                    should_proceed_with_llm_thought = True # 标记需要处理LLM结果（即使是刚完成的）
                    # 如果是因为LLM完成而唤醒，需要取消其他等待
                    if sub_mind_wait_task in pending_tasks : sub_mind_wait_task.cancel()
                    if stop_event_wait_task in pending_tasks : stop_event_wait_task.cancel()

                if not should_proceed_with_llm_thought and (not current_main_llm_task or current_main_llm_task.done()):
                    # 如果没有被子思维事件中断，LLM任务也已经结束了，那么就不需要再进行一次LLM思考
                    self.logger.debug(f"主思维循环 {loop_count}: LLM任务已结束，且无即时子思维事件，等待下一轮定时器或事件。")
                    # 这里可能需要一个短暂的 sleep 以避免CPU空转，或者依赖外层循环的定时器
                    # 但由于我们总是会进入 should_proceed_with_llm_thought=True 的分支（除非被中断）
                    # 或者在下一轮循环开始时等待定时器，所以这里可以 continue
                    # await asyncio.sleep(0.01) # 避免可能的快速空转
                    # continue

            else: # 没有正在运行的LLM任务，正常等待定时器或子思维事件
                timer_task = asyncio.create_task(asyncio.sleep(float(thinking_interval_sec)), name="timer_task") # 创建定时器任务
                sub_mind_event_task = asyncio.create_task(self.sub_mind_update_event.wait(), name="sub_mind_event_task") # 创建子思维事件等待任务
                stop_event_task = asyncio.create_task(self.async_stop_event.wait(), name="stop_event_task_main_wait") # 停止事件等待

                tasks_to_wait_on = [timer_task, sub_mind_event_task, stop_event_task] # 要等待的任务列表
                done_tasks, pending_tasks = await asyncio.wait( # 等待任一任务完成
                    tasks_to_wait_on, return_when=asyncio.FIRST_COMPLETED
                )

                self.logger.debug(f"主思维循环 {loop_count}: 等待结束，完成的任务: {[t.get_name() for t in done_tasks if hasattr(t, 'get_name')]}") # 看看是谁完成了

                if self.async_stop_event.is_set() or stop_event_task in done_tasks: # 如果检测到停止信号
                    self.logger.info(f"主思维循环 {loop_count}: 检测到停止信号 (在正常等待后)，准备退出。") # 记录日志
                    for task_to_cancel in pending_tasks: # 取消其他挂起任务
                        if not task_to_cancel.done(): task_to_cancel.cancel() # 取消
                    await asyncio.gather(*tasks_to_wait_on, return_exceptions=True) # 等待所有任务完成
                    break # 退出主循环

                if timer_task in done_tasks: # 如果是定时器到期
                    self.logger.info(f"主思维循环 {loop_count}: 定时器到期，准备进行LLM思考。") # 记录日志
                    should_proceed_with_llm_thought = True # 标记需要进行LLM思考
                
                if sub_mind_event_task in done_tasks: # 如果是子思维事件触发
                    self.logger.info(f"主思维循环 {loop_count}: 子思维事件触发，准备进行LLM思考以响应新状态。") # 记录日志
                    self.sub_mind_update_event.clear() # 清除事件
                    should_proceed_with_llm_thought = True # 标记需要进行LLM思考
                
                for task_in_pending in pending_tasks: # 取消其他未完成的等待任务
                    if not task_in_pending.done():
                        self.logger.debug(f"主思维循环 {loop_count}: 准备取消挂起的等待任务: {task_in_pending.get_name() if hasattr(task_in_pending, 'get_name') else '未知任务'}")
                        task_in_pending.cancel()
                if pending_tasks: # 等待取消操作完成
                    await asyncio.gather(*pending_tasks, return_exceptions=True)


            # --- LLM 思考与后续处理 ---
            if should_proceed_with_llm_thought: # 如果需要进行LLM思考
                self.logger.info(f"主思维循环 {loop_count}: 准备获取数据库和上下文信息以进行LLM思考。") # 记录日志
                latest_thought_doc_from_db = await self.db_handler.get_latest_thought_document_raw() if self.db_handler else None # 获取最新思考文档

                formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"] # 初始化上下文
                if self.db_handler: # 如果数据库处理器存在
                    try:
                        raw_context_messages = await self.db_handler.get_recent_chat_messages_for_context( # 获取最近聊天记录
                            duration_minutes=chat_history_duration_minutes, # 时长
                            conversation_id=self.current_focused_conversation_id # 焦点会话ID (可选)
                        )
                        if raw_context_messages: # 如果获取到记录
                            formatted_recent_contextual_info = format_chat_history_for_prompt(raw_context_messages) # 格式化
                        self.logger.debug(f"主思维循环 {loop_count}: 获取上下文信息完成。上下文长度: {len(formatted_recent_contextual_info)}") # 记录日志
                    except Exception as e_hist: # 捕获异常
                        self.logger.error(f"主思维循环 {loop_count}: 获取或格式化最近上下文信息时出错: {e_hist}", exc_info=True) # 记录错误

                current_state_for_prompt, temp_action_id_result_shown = ( # 处理当前状态
                    self._process_thought_and_action_state( # 调用状态处理方法
                        latest_thought_document=latest_thought_doc_from_db, # 最新思考文档
                        formatted_recent_contextual_info=formatted_recent_contextual_info # 格式化上下文
                    )
                )
                self.logger.debug(f"主思维循环 {loop_count}: 处理思考和动作状态完成。") # 记录日志

                # 构建 Prompts
                system_prompt_str = self.prompt_builder.build_system_prompt(current_time_formatted_str) # 构建System Prompt
                user_prompt_str = self.prompt_builder.build_user_prompt( # 构建User Prompt
                    current_state_for_prompt=current_state_for_prompt, # 当前状态
                    intrusive_thought_str=await self._get_intrusive_thought_for_cycle() # 获取侵入性思维
                )

                self.logger.info( # 记录日志
                    f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] "
                    f"{self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 准备调用LLM进行思考 (受保护)..."
                )
                
                # 创建并执行LLM思考任务
                # _generate_thought_from_llm 内部已经使用了 execute_protected_task_with_polling
                llm_call_output_tuple: Optional[Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]] = None
                if not (current_main_llm_task and not current_main_llm_task.done()): # 确保没有正在运行的旧任务
                    current_main_llm_task = asyncio.create_task( # 创建新任务
                        self._generate_thought_from_llm( # 调用生成思考的方法
                            llm_client=self.main_consciousness_llm_client, # 主意识LLM客户端
                            system_prompt_str=system_prompt_str, # System Prompt
                            user_prompt_str=user_prompt_str, # User Prompt
                            cancellation_event=main_llm_cancellation_event # 取消事件
                        ),
                        name=f"MainLLMThoughtTask_Loop{loop_count}" # 任务命名
                    )
                
                try:
                    llm_call_output_tuple = await current_main_llm_task # 等待LLM任务完成
                except asyncio.CancelledError: # 捕获取消错误
                    self.logger.warning(f"主思维循环 {loop_count}: 主LLM思考任务在等待时被取消。") # 记录警告
                    llm_call_output_tuple = None # 设为None
                except Exception as e_llm_task_await: # 捕获其他等待错误
                    self.logger.error(f"主思维循环 {loop_count}: 等待主LLM思考任务时发生错误: {e_llm_task_await}", exc_info=True) # 记录错误
                    llm_call_output_tuple = None # 设为None
                finally:
                    current_main_llm_task = None # 清理任务引用

                generated_thought_json: Optional[Dict[str, Any]] = None # 初始化解析后的JSON
                # full_prompt_text_sent 和 system_prompt_sent 已在 _generate_thought_from_llm 中处理并返回
                
                if llm_call_output_tuple: # 如果LLM调用有返回
                    generated_thought_json, _, _ = llm_call_output_tuple # 解包元组（Prompts已在内部记录）
                    if generated_thought_json: # 如果成功获取到JSON
                        action_id_whose_result_was_shown_in_last_prompt = temp_action_id_result_shown # 更新已阅动作ID
                
                self.logger.info(f"主思维循环 {loop_count}: LLM思考生成调用完成。") # 记录日志

                if generated_thought_json and self.thought_processor: # 如果有思考结果且处理器存在
                    _, background_tasks_from_processor = await self.thought_processor.process_thought_and_actions( # 调用处理器
                        generated_thought_json=generated_thought_json, # 思考结果
                        current_state_for_prompt=current_state_for_prompt, # 当前状态（用于保存）
                        current_time_formatted_str=current_time_formatted_str, # 当前时间（用于保存）
                        system_prompt_sent=system_prompt_str, # System Prompt（用于保存）
                        full_prompt_text_sent=user_prompt_str, # User Prompt（用于保存）
                        intrusive_thought_to_inject_this_cycle=await self._get_intrusive_thought_for_cycle(used=True), # 侵入性思维（标记为已用）
                        formatted_recent_contextual_info=formatted_recent_contextual_info, # 上下文（用于保存）
                        action_id_whose_result_was_shown_in_last_prompt=action_id_whose_result_was_shown_in_last_prompt, # 已阅动作ID
                        loop_count=loop_count # 循环计数
                    )
                    # 注意：这里的 background_action_tasks 是 CoreLogic 级别的，用于在程序退出时等待。
                    # CoreThoughtProcessor 返回的 tasks 应该是它自己创建并需要等待的。
                    # 我们这里不直接使用它返回的 tasks，因为 CoreThoughtProcessor 内部应该自己管理这些任务的生命周期。
                    # 或者，如果 CoreThoughtProcessor 返回的任务确实需要 CoreLogic 来等待，则需要合并。
                    # 为简单起见，假设 CoreThoughtProcessor 内部处理了其异步任务。
                elif not generated_thought_json: # 如果LLM思考失败
                    self.logger.warning(f"主思维循环 {loop_count}: 本轮主思维LLM思考生成失败或无内容。") # 记录警告
                    if action_id_whose_result_was_shown_in_last_prompt and self.db_handler: # 仍然标记旧动作为已阅
                        try:
                            await self.db_handler.mark_action_result_as_seen(action_id_whose_result_was_shown_in_last_prompt) # 标记
                        except Exception as e_mark_seen_after_fail: # 捕获异常
                            self.logger.error(f"主思维循环 {loop_count}: (LLM失败后) 标记动作结果为已阅时失败: {e_mark_seen_after_fail}", exc_info=True) # 记录错误
            else: # 如果本轮不进行LLM思考
                self.logger.info(f"主思维循环 {loop_count}: 本轮不进行新的LLM思考（可能因为上一轮任务仍在进行或无触发条件）。") # 记录日志

            self.logger.info(f"主思维思考循环轮次 {loop_count} 逻辑处理结束。") # 记录日志

            self.logger.debug(f"循环末尾检查: self.async_stop_event.is_set() 的状态是: {self.async_stop_event.is_set()}")
            # ^^^^ 加在这里 ^^^^

            if self.async_stop_event.is_set(): # 检查停止信号
                self.logger.info(f"主思维循环 {loop_count}: 在循环末尾检测到停止信号，准备退出。") # 记录日志
                break # 退出循环
        
        self.logger.info(f"--- {self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 的主思维思考循环已结束 (共 {loop_count} 轮)。") # 记录日志
        # 此处可以加入等待所有后台任务（如ActionHandler中的任务）完成的逻辑，如果需要的话。
        # 例如，如果 self.action_handler_instance 维护了一个活动任务列表。

    async def _get_intrusive_thought_for_cycle(self, used: bool = False) -> str:
        """
        获取当前循环周期要使用的侵入性思维。
        如果标记为used，则表示该思维已被用于Prompt，未来可以加入逻辑避免短期内重复。
        """
        intrusive_thought_to_inject_this_cycle: str = "" # 初始化为空
        if self.root_cfg and self.intrusive_generator_instance and \
           self.intrusive_generator_instance.module_settings.enabled and \
           random.random() < self.intrusive_generator_instance.module_settings.insertion_probability: # 根据概率判断是否注入
            if self.db_handler: # 如果数据库处理器存在
                random_thought_doc = await self.db_handler.get_random_intrusive_thought() # 从数据库随机获取
                if random_thought_doc and "text" in random_thought_doc: # 如果获取成功且有文本
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}" # 格式化
                    if used: # 如果标记为已使用
                        self.logger.debug(f"侵入性思维 '{random_thought_doc['text'][:30]}...' 已被用于Prompt。") # 记录日志（可选）
                        # 可以在这里加入逻辑，例如将此思维ID标记为短期内不重复使用
            else:
                self.logger.warning("无法获取侵入性思维，因为数据库处理器 (db_handler) 未初始化。")
        
        if intrusive_thought_to_inject_this_cycle and not used: # 如果获取到但尚未标记为使用（例如，只是为了日志）
             self.logger.info(f"  本轮注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...") # 记录日志
        return intrusive_thought_to_inject_this_cycle # 返回侵入性思维

    async def start(self) -> asyncio.Task: # <--- 注意这里，返回类型变成了 asyncio.Task
        """
        启动 CoreLogic 的主思考循环和相关后台任务。
        返回核心思考循环的任务对象。
        """
        self.logger.info("CoreLogic 开始启动...")
        # self.stop_event.clear() # 这些事件在 __init__ 中创建，通常 start 时不需要再 clear
        # self.async_stop_event.clear() # 除非你有重启逻辑，否则创建时它们就是 clear 的

        if self.intrusive_generator_instance:
            self.intrusive_generator_instance.start_background_generation()
            self.logger.info("侵入性思维后台生成线程已通过 IntrusiveThoughtsGenerator 启动。")
        
        thinking_loop_task = asyncio.create_task(self._core_thinking_loop(), name="CoreThinkingLoopTask")
        self.logger.info("核心思考循环已作为异步任务启动。")
        return thinking_loop_task 

    async def stop(self) -> None:
        """
        请求停止 CoreLogic 的主思考循环和相关后台任务。
        """
        self.logger.info("CoreLogic 收到停止请求...") # 记录日志
        self.stop_event.set() # 设置同步停止事件（用于停止基于threading的后台任务）
        self.async_stop_event.set() # 设置异步停止事件（用于停止基于asyncio的任务）

        if self.intrusive_generator_instance: # 如果侵入性思维生成器存在
            self.intrusive_generator_instance.stop_generation_thread() # 请求停止其线程
            self.logger.info("已请求停止侵入性思维生成器线程。") # 记录日志
        
        # 此处可以添加等待 _core_thinking_loop 任务结束的逻辑，如果需要确保完全停止
        # 例如，可以查找名为 "CoreThinkingLoopTask" 的任务并等待它
        # 但通常设置 async_stop_event 后，循环会在下一次迭代时退出

        self.logger.info("CoreLogic 停止请求处理完毕。") # 记录日志

async def start_consciousness_flow():
    """
    初始化并启动 AIcarus 的核心意识流程。
    """
    logger.info("开始执行 start_consciousness_flow，准备初始化核心组件...")

    try:
        # 1. 加载配置 (这些步骤和你之前的一样)
        root_cfg = get_typed_settings()
        logger.info("配置加载完毕。")

        db_handler = await ArangoDBHandler.create()
        logger.info("数据库处理器初始化完毕。")

        def _create_llm_client(purpose_key: str, default_provider: str = "gemini") -> ProcessorClient:
            if not root_cfg.providers:
                raise ValueError(f"配置错误：RootConfig 中缺少 'providers' 段。无法为 '{purpose_key}' 创建LLM客户端。")
            provider_settings = getattr(root_cfg.providers, default_provider.lower(), None)
            if not provider_settings or not provider_settings.models:
                raise ValueError(
                    f"配置错误：在 providers 下未找到 '{default_provider}' 的配置或其 'models' 段。无法为 '{purpose_key}' 创建LLM客户端。"
                )
            model_params_cfg = getattr(provider_settings.models, purpose_key, None)
            if not model_params_cfg:
                raise ValueError(
                    f"配置错误：在提供商 '{default_provider}' 的 models 配置下未找到用途键 '{purpose_key}'。无法创建LLM客户端。"
                )
            
            client_args = {
                "model": {"provider": model_params_cfg.provider, "name": model_params_cfg.model_name},
                "abandoned_keys_config": json.loads(os.getenv("LLM_ABANDONED_KEYS", "null")) if os.getenv("LLM_ABANDONED_KEYS") else None,
                "proxy_host": None, # 先设为None，下面再根据配置填充
                "proxy_port": None, # 先设为None
                "image_placeholder_tag": root_cfg.llm_client_settings.image_placeholder_tag,
                "stream_chunk_delay_seconds": root_cfg.llm_client_settings.stream_chunk_delay_seconds,
                "enable_image_compression": root_cfg.llm_client_settings.enable_image_compression,
                "image_compression_target_bytes": root_cfg.llm_client_settings.image_compression_target_bytes,
                "rate_limit_disable_duration_seconds": root_cfg.llm_client_settings.rate_limit_disable_duration_seconds,
            }
            if root_cfg.proxy.use_proxy and root_cfg.proxy.http_proxy_url:
                try:
                    # 一个非常简化的解析代理URL的尝试
                    parsed_url = urlparse(root_cfg.proxy.http_proxy_url) # 需要 from urllib.parse import urlparse
                    client_args["proxy_host"] = parsed_url.hostname
                    client_args["proxy_port"] = parsed_url.port
                except Exception as e_proxy_parse:
                    logger.warning(f"解析代理URL '{root_cfg.proxy.http_proxy_url}' 失败: {e_proxy_parse}。LLM客户端将不使用此配置的代理。")
            
            if model_params_cfg.temperature is not None: client_args["temperature"] = model_params_cfg.temperature
            if model_params_cfg.max_output_tokens is not None: client_args["maxOutputTokens"] = model_params_cfg.max_output_tokens
            if model_params_cfg.top_p is not None: client_args["top_p"] = model_params_cfg.top_p
            if model_params_cfg.top_k is not None: client_args["top_k"] = model_params_cfg.top_k
            client_args_cleaned = {k: v for k, v in client_args.items() if v is not None}
            logger.info(f"正在为 '{purpose_key}' (提供商: {model_params_cfg.provider}, 模型: {model_params_cfg.model_name}) 创建LLM客户端...")
            return ProcessorClient(**client_args_cleaned)

        # 需要导入 urlparse
        from urllib.parse import urlparse

        main_consciousness_llm_client = _create_llm_client("main_consciousness", "gemini")
        intrusive_thoughts_llm_client = _create_llm_client("intrusive_thoughts", "gemini")
        sub_mind_llm_client = _create_llm_client("sub_mind_chat_reply", "gemini")
        action_decision_llm_client_for_core_logic = None # ActionHandler 会自己创建
        information_summary_llm_client_for_core_logic = None
        logger.info("核心LLM客户端们初始化(尝试)完毕。ActionHandler的LLM客户端将由其自身管理。")

        core_comm_layer: Optional[CoreWebsocketServer] = None
        # (这里的通信层初始化逻辑和你之前的一样，如果需要就取消注释并实现它)
        logger.info("核心WebSocket通信层(如果配置了的话)的初始化逻辑已跳过(示例)。")


        # --- 这是关键的修改部分 ---
        # 步骤 1: 先创建 CoreLogic 实例，但暂时不给它 ChatSessionManager
        _core_logic_instance = CoreLogic(
            root_cfg=root_cfg,
            db_handler=db_handler,
            main_consciousness_llm_client=main_consciousness_llm_client,
            intrusive_thoughts_llm_client=intrusive_thoughts_llm_client,
            sub_mind_llm_client=sub_mind_llm_client,
            action_decision_llm_client=action_decision_llm_client_for_core_logic,
            information_summary_llm_client=information_summary_llm_client_for_core_logic,
            chat_session_manager=None, # <--- 核心改动：先传入 None
            core_comm_layer=core_comm_layer
        )
        logger.info("CoreLogic 实例已创建 (ChatSessionManager 暂未设置)。")

        # 步骤 2: 现在 _core_logic_instance 已经是一个完整的 CoreLogic 对象了，
        # 我们可以用它来初始化 ChatSessionManager
        chat_session_manager_instance = ChatSessionManager(core_logic_ref=_core_logic_instance)
        logger.info("ChatSessionManager 实例已创建，并已引用 CoreLogic 实例。")

        # 步骤 3: 把创建好的 ChatSessionManager 实例设置回 CoreLogic 实例
        _core_logic_instance.chat_session_manager = chat_session_manager_instance
        logger.info("ChatSessionManager 已成功设置到 CoreLogic 实例中。初始化完成！")
        # --- 修改结束 ---

        logger.info("准备启动 CoreLogic 并等待其核心循环...")
        thinking_task = await _core_logic_instance.start() # 调用 start 并获取返回的任务
        logger.info("CoreLogic 的 start 方法已执行，核心思考循环任务已创建。")
        
        if thinking_task: # 确保任务成功创建并返回
            logger.info("正在等待核心思考循环任务完成 (这通常意味着程序将持续运行直到被中断)...")
            try:
                await thinking_task # <--- 重要！在这里等待核心任务执行完毕
            except asyncio.CancelledError:
                logger.info("核心思考循环任务被取消。")
            except Exception as e_loop:
                logger.error(f"核心思考循环任务执行时发生错误: {e_loop}", exc_info=True)
        else:
            logger.error("CoreLogic 的 start 方法未能返回有效的任务对象！程序可能无法正常运行。")

        logger.info("start_consciousness_flow 执行流程即将结束 (如果核心循环已结束或未正确等待)。")

    except ValueError as ve:
        logger.critical(f"初始化核心流程时配置或参数错误: {ve}", exc_info=True)
        print(f"程序启动失败：配置错误 - {ve}")
    except Exception as e:
        logger.critical(f"初始化或运行核心流程时发生未预料的严重错误: {e}", exc_info=True)
        print(f"程序启动时发生严重内部错误: {e}")
        import traceback
        traceback.print_exc()
