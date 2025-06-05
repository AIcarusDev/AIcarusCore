# src/action/action_handler.py
import asyncio
import json
import os
from typing import Any, List, Dict # 确保导入了 List 和 Dict
from urllib.parse import urlparse

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import LLMClientSettings, ModelParams, ProxySettings # 确保导入这些配置类
from src.config.global_config import global_config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.tools.tool_registry import get_tool_schemas, get_tool_function
from .prompts import ACTION_DECISION_PROMPT_TEMPLATE, INFORMATION_SUMMARY_PROMPT_TEMPLATE

class ActionHandler:
    """
    【最终版】
    负责编排AI的行动决策流程：
    1. 调用LLM进行工具选择。
    2. 从工具注册表调度并执行工具。
    3. 对需要总结的工具结果（如网页搜索）调用LLM进行总结。
    4. 将最终结果和状态更新回思考文档。
    """
    def __init__(self) -> None:
        """
        初始化 ActionHandler 实例。
        """
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.root_cfg = global_config # 获取全局配置
        self.action_llm_client: ProcessorClient | None = None # 用于动作决策的LLM客户端
        self.summary_llm_client: ProcessorClient | None = None # 用于信息总结的LLM客户端
        self.core_communication_layer: CoreWebsocketServer | None = None # WebSocket通信层，用于发送平台动作
        self.thought_storage_service: ThoughtStorageService | None = None # 思考存储服务，用于更新动作状态
        self.logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService | None = None,
        comm_layer: CoreWebsocketServer | None = None
    ) -> None:
        """
        设置 ActionHandler 运行所需的依赖。

        Args:
            thought_service: ThoughtStorageService 实例。
            comm_layer: CoreWebsocketServer 实例。
        """
        self.thought_storage_service = thought_service
        self.core_communication_layer = comm_layer
        self.logger.info("ActionHandler 的依赖已成功设置 (thought_service, comm_layer)。")

    def _create_llm_client_from_config(self, purpose_key: str) -> ProcessorClient | None:
        """
        根据全局配置和指定的用途键 (purpose_key) 创建一个LLM客户端实例。

        Args:
            purpose_key: 字符串，指示LLM客户端的用途（例如 "action_decision", "information_summary"）。
                         这个键对应于 `AlcarusRootConfig.llm_models` 中的字段名。

        Returns:
            一个 ProcessorClient 实例，如果创建成功；否则返回 None。
        """
        if not self.root_cfg:
            self.logger.critical("Root config 未加载。无法创建LLM客户端。")
            return None
        try:
            # 确保 llm_models 配置段存在
            if not self.root_cfg.llm_models:
                self.logger.error("配置错误：AlcarusRootConfig 中缺少 'llm_models' 配置段。")
                return None

            # 从 llm_models 中获取指定用途的模型参数配置
            model_params_cfg = getattr(self.root_cfg.llm_models, purpose_key, None)
            if not isinstance(model_params_cfg, ModelParams):
                self.logger.error(
                    f"配置错误：在 AlcarusRootConfig.llm_models 下未找到模型用途键 '{purpose_key}' 对应的有效 ModelParams 配置，或类型不匹配。"
                )
                return None

            actual_provider_name_str: str = model_params_cfg.provider
            actual_model_name_str: str = model_params_cfg.model_name

            if not actual_provider_name_str or not actual_model_name_str:
                self.logger.error(
                    f"配置错误：模型 '{purpose_key}' (提供商: {actual_provider_name_str or '未知'}) 未指定 'provider' 或 'model_name'。"
                )
                return None

            general_llm_settings_obj: LLMClientSettings = self.root_cfg.llm_client_settings
            proxy_settings_obj: ProxySettings = self.root_cfg.proxy
            
            final_proxy_host: str | None = None
            final_proxy_port: int | None = None
            if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
                try:
                    parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                    final_proxy_host = parsed_url.hostname
                    final_proxy_port = parsed_url.port
                    if not final_proxy_host or final_proxy_port is None: # 确保端口号也不是None
                        final_proxy_host, final_proxy_port = None, None # 如果不完整则重置
                except Exception:
                    self.logger.warning(f"解析代理URL '{proxy_settings_obj.http_proxy_url}' 失败。")
                    final_proxy_host, final_proxy_port = None, None

            model_for_client_constructor = {"provider": actual_provider_name_str.upper(), "name": actual_model_name_str}
            
            model_specific_kwargs: dict[str, Any] = {}
            if model_params_cfg.temperature is not None: model_specific_kwargs["temperature"] = model_params_cfg.temperature
            if model_params_cfg.max_output_tokens is not None: model_specific_kwargs["maxOutputTokens"] = model_params_cfg.max_output_tokens
            if model_params_cfg.top_p is not None: model_specific_kwargs["top_p"] = model_params_cfg.top_p
            if model_params_cfg.top_k is not None: model_specific_kwargs["top_k"] = model_params_cfg.top_k

            # 准备 ProcessorClient 构造函数的参数
            # 注意：vars(general_llm_settings_obj) 会将 dataclass 实例转换为字典
            processor_constructor_args = {
                "model": model_for_client_constructor,
                "proxy_host": final_proxy_host,
                "proxy_port": final_proxy_port,
                **vars(general_llm_settings_obj), # 通用LLM客户端设置
                **model_specific_kwargs,        # 特定模型的参数会覆盖通用设置中的同名参数
            }
            # 移除值为 None 的参数，以使用 ProcessorClient 内部的默认值
            final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
            
            client_instance = ProcessorClient(**final_constructor_args)
            self.logger.info(f"成功创建 ProcessorClient 实例用于 '{purpose_key}' (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider}).")
            return client_instance

        except AttributeError as e_attr: # 通常指示配置结构问题
            self.logger.error(
                f"配置访问错误 (AttributeError) 创建LLM客户端 (用途: {purpose_key}) 时: {e_attr}", exc_info=True
            )
            return None
        except Exception as e: # 捕获其他所有未知错误
            self.logger.error(f"创建LLM客户端 (用途: {purpose_key}) 时发生未知错误: {e}", exc_info=True)
            return None

    async def initialize_llm_clients(self) -> None:
        """
        按需初始化本模块所需的LLM客户端 (动作决策和信息总结)。
        如果客户端已存在，则不会重复创建。
        """
        if self.action_llm_client and self.summary_llm_client:
            # 如果两个客户端都已存在，则提前返回
            return

        self.logger.info("正在为行动处理模块按需初始化LLM客户端...")
        
        # 初始化动作决策LLM客户端
        if not self.action_llm_client:
            self.action_llm_client = self._create_llm_client_from_config(purpose_key="action_decision")
            if not self.action_llm_client:
                self.logger.critical("行动决策LLM客户端初始化失败。")
                raise RuntimeError("行动决策LLM客户端初始化失败。")

        # 初始化信息总结LLM客户端
        if not self.summary_llm_client:
            self.summary_llm_client = self._create_llm_client_from_config(purpose_key="information_summary")
            if not self.summary_llm_client:
                self.logger.critical("信息总结LLM客户端初始化失败。")
                raise RuntimeError("信息总结LLM客户端初始化失败。")

        self.logger.info("行动处理模块的LLM客户端按需初始化完成。")

    async def _summarize_tool_result_async(
        self,
        original_query: str, # 工具执行前的原始查询或动作描述
        original_motivation: str, # 工具执行前的原始动机
        tool_output: Any # 工具执行后返回的原始输出
    ) -> str:
        """
        调用信息总结LLM客户端 (self.summary_llm_client) 来总结工具的输出。

        Args:
            original_query: 触发工具执行的原始查询或动作描述。
            original_motivation: 执行该动作的原始动机。
            tool_output: 工具执行后返回的原始数据。

        Returns:
            总结后的文本字符串。如果总结失败，则返回包含错误信息的字符串。
        """
        if not self.summary_llm_client:
            self.logger.error("信息总结LLM客户端未初始化，无法进行信息总结。")
            return f"错误：信息总结功能当前不可用。原始工具输出: {str(tool_output)[:200]}..." # 返回部分原始输出作为提示

        self.logger.info(f"正在调用LLM对工具结果进行信息总结。原始意图: '{original_query[:50]}...'")
        
        # 将工具的原始输出格式化为字符串，以便传递给LLM
        # 如果工具输出是复杂对象（如列表或字典），使用json.dumps可以很好地序列化
        try:
            raw_tool_output_str = json.dumps(tool_output, indent=2, ensure_ascii=False)
        except TypeError: # 如果无法JSON序列化，则退回到使用str()
            raw_tool_output_str = str(tool_output)

        # 构建总结用的Prompt
        summary_prompt = INFORMATION_SUMMARY_PROMPT_TEMPLATE.format(
            original_query_or_action=original_query,
            original_motivation=original_motivation,
            raw_tool_output=raw_tool_output_str
        )

        # 调用LLM进行总结
        response = await self.summary_llm_client.make_llm_request(
            prompt=summary_prompt,
            is_stream=False # 总结通常不需要流式输出
        )

        if response.get("error"):
            error_message = f"总结信息时LLM调用失败: {response.get('message', '未知API错误')}"
            self.logger.error(error_message)
            return error_message # 返回错误信息给上层
        
        summary_text = response.get("text")
        if summary_text is None or not summary_text.strip():
            # 如果LLM没有返回文本，或者返回了空文本
            self.logger.warning("信息总结LLM调用成功，但未返回有效的文本内容。")
            return "未能从工具结果中总结出有效信息。"
        
        self.logger.info(f"信息总结完成。摘要 (前100字符): {summary_text[:100]}...")
        return summary_text.strip()


    async def process_action_flow(
        self,
        action_id: str, # 本次动作的唯一ID
        doc_key_for_updates: str, # 关联的思考文档的_key，用于状态更新
        action_description: str, # AI意图执行的动作描述
        action_motivation: str, # AI执行此动作的动机
        current_thought_context: str, # AI当前的思考上下文
        relevant_adapter_messages_context: str = "无相关外部消息或请求。" # 从适配器获取的相关消息上下文
    ) -> None:
        """
        处理AI行动流程的核心方法。
        """
        self.logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 开始处理行动流程 ---")
        self.logger.debug(f"  意图动作: '{action_description}', 动机: '{action_motivation}'")
        self.logger.debug(f"  思考上下文: '{current_thought_context[:100]}...'")
        self.logger.debug(f"  适配器消息上下文: '{relevant_adapter_messages_context[:100]}...'")


        if not self.thought_storage_service:
            self.logger.critical(f"严重错误 [Action ID: {action_id}]: ThoughtStorageService 未初始化，无法更新动作状态。")
            # 这种情况下，无法记录失败状态到数据库
            return
            
        # 1. 确保LLM客户端已初始化
        try:
            await self.initialize_llm_clients() # 按需初始化
        except Exception as e_init:
            self.logger.critical(f"严重错误 [Action ID: {action_id}]: LLM客户端初始化失败: {e_init}", exc_info=True)
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates, action_id, {
                    "status": "CRITICAL_FAILURE", 
                    "error_message": f"行动模块LLM客户端初始化失败: {str(e_init)}",
                    "final_result_for_shuang": f"你尝试执行动作 '{action_description}' 时，系统遇到严重的初始化错误，无法继续。"
                }
            )
            return

        # 2. 调用LLM进行工具选择决策
        tools_schema_for_llm = get_tool_schemas() # 从工具注册表获取Schema
        decision_prompt_text = ACTION_DECISION_PROMPT_TEMPLATE.format(
            tools_json_string=json.dumps(tools_schema_for_llm, indent=2, ensure_ascii=False),
            current_thought_context=current_thought_context,
            action_description=action_description,
            action_motivation=action_motivation,
            relevant_adapter_messages_context=relevant_adapter_messages_context,
        )
        
        self.logger.info(f"--- [Action ID: {action_id}] 请求行动决策LLM选择工具 ---")
        if not self.action_llm_client: # 双重检查，理论上 initialize_llm_clients 会处理
             self.logger.critical(f"严重错误 [Action ID: {action_id}]: action_llm_client 在调用前仍未初始化!")
             await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates, action_id, {"status": "CRITICAL_FAILURE", "error_message": "内部错误：决策LLM客户端丢失"}
            )
             return

        decision_response = await self.action_llm_client.make_llm_request(
            prompt=decision_prompt_text, 
            is_stream=False, 
            tools=tools_schema_for_llm # 将Schema传递给LLM
        )
        
        # 3. 解析LLM的决策并准备执行工具
        final_result_for_shuang: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。" # 默认失败信息
        action_was_successful: bool = False
        tool_name_chosen: str | None = None
        tool_arguments: Dict[str, Any] = {}

        if decision_response.get("error"):
            final_result_for_shuang = f"行动决策LLM调用失败: {decision_response.get('message', '未知API错误')}"
            self.logger.error(f"[Action ID: {action_id}] {final_result_for_shuang}")
        elif decision_response.get("tool_calls") and isinstance(decision_response["tool_calls"], list) and len(decision_response["tool_calls"]) > 0:
            try:
                # 通常我们只期望LLM返回一个工具调用
                first_tool_call = decision_response["tool_calls"][0]
                if first_tool_call.get("type") == "function" and first_tool_call.get("function"):
                    tool_name_chosen = first_tool_call["function"].get("name")
                    arguments_str = first_tool_call["function"].get("arguments", "{}")
                    tool_arguments = json.loads(arguments_str) # 解析参数字符串为字典
                    self.logger.info(f"[Action ID: {action_id}] LLM决策调用工具: '{tool_name_chosen}', 参数: {arguments_str}")
                else:
                    final_result_for_shuang = "LLM返回的工具调用格式不正确。"
                    self.logger.warning(f"[Action ID: {action_id}] {final_result_for_shuang} 响应: {first_tool_call}")
            except json.JSONDecodeError as e:
                final_result_for_shuang = f"解析工具调用参数时JSON解码失败: {e}. 原始参数: '{arguments_str}'"
                self.logger.error(f"[Action ID: {action_id}] {final_result_for_shuang}", exc_info=True)
            except Exception as e_parse: # 捕获其他可能的解析错误
                final_result_for_shuang = f"解析工具调用时发生未知错误: {e_parse}"
                self.logger.error(f"[Action ID: {action_id}] {final_result_for_shuang}", exc_info=True)
        else: # LLM没有选择工具，可能返回了文本
            llm_text_output = decision_response.get("text", "").strip()
            if llm_text_output:
                final_result_for_shuang = f"行动决策失败，LLM未能选择一个可用工具，而是返回了以下文本：'{llm_text_output}'"
                self.logger.warning(f"[Action ID: {action_id}] {final_result_for_shuang}")
            else:
                final_result_for_shuang = "行动决策失败，LLM的响应中既不包含工具调用，也不包含任何文本解释。"
                self.logger.error(f"[Action ID: {action_id}] LLM响应异常，完整响应: {decision_response}")

        # 4. 如果成功选择了工具，则执行它
        if tool_name_chosen:
            actual_tool_function = get_tool_function(tool_name_chosen) # 从注册表获取函数
            if actual_tool_function:
                self.logger.info(f"[Action ID: {action_id}] 准备执行工具: '{tool_name_chosen}'...")
                try:
                    # 为需要特殊依赖的工具注入依赖
                    # 例如，发送消息的工具需要通信层
                    if tool_name_chosen in ["send_reply_message"]: # 可以维护一个需要comm_layer的工具列表
                        if self.core_communication_layer:
                            tool_arguments["comm_layer"] = self.core_communication_layer
                        else:
                            raise RuntimeError(f"工具 '{tool_name_chosen}' 需要 core_communication_layer，但它未被设置。")
                    
                    # 异步执行工具函数
                    tool_execution_result = await actual_tool_function(**tool_arguments)
                    self.logger.info(f"[Action ID: {action_id}] 工具 '{tool_name_chosen}' 执行完毕。")
                    
                    # 处理工具执行结果
                    if isinstance(tool_execution_result, dict) and tool_execution_result.get("status") == "failure":
                        # 如果工具内部明确返回失败状态
                        final_result_for_shuang = f"工具 '{tool_name_chosen}' 执行失败: {tool_execution_result.get('reason', '未知工具内部错误')}"
                        action_was_successful = False
                    elif isinstance(tool_execution_result, str):
                        # 如果工具直接返回字符串结果
                        final_result_for_shuang = tool_execution_result
                        action_was_successful = True
                    else:
                        # 如果工具返回了复杂结果（例如搜索结果列表），则需要进行总结
                        self.logger.info(f"[Action ID: {action_id}] 工具 '{tool_name_chosen}' 返回了复杂结果，准备调用信息总结LLM。")
                        final_result_for_shuang = await self._summarize_tool_result_async(
                            original_query=action_description, # 使用原始动作描述作为总结上下文
                            original_motivation=action_motivation, # 使用原始动机作为总结上下文
                            tool_output=tool_execution_result # 传递工具的原始输出
                        )
                        # 假设总结总是“成功”的（即使总结结果是“未找到信息”）
                        action_was_successful = True 
                        if "错误：" in final_result_for_shuang or "失败：" in final_result_for_shuang: # 根据总结结果判断是否真的算成功
                            action_was_successful = False


                except Exception as e_tool_exec:
                    final_result_for_shuang = f"执行工具 '{tool_name_chosen}' 时发生系统异常: {e_tool_exec}"
                    self.logger.error(f"[Action ID: {action_id}] {final_result_for_shuang}", exc_info=True)
                    action_was_successful = False
            else:
                # LLM选择了一个注册表中不存在的工具
                final_result_for_shuang = f"行动决策失败，因为LLM选择了一个无法识别或未注册的工具: '{tool_name_chosen}'"
                self.logger.error(f"[Action ID: {action_id}] {final_result_for_shuang}")
                action_was_successful = False
        
        # 5. 更新最终的动作状态到思考文档
        update_payload = {
            "status": "COMPLETED_SUCCESS" if action_was_successful else "COMPLETED_FAILURE",
            "final_result_for_shuang": final_result_for_shuang,
            "error_message": "" if action_was_successful else final_result_for_shuang # 如果失败，将最终结果也作为错误信息
        }
        await self.thought_storage_service.update_action_status_in_thought_document(
            doc_key_for_updates, 
            action_id, 
            update_payload
        )
        self.logger.info(f"--- [Action ID: {action_id}] 行动流程结束，最终状态: {'成功' if action_was_successful else '失败'} ---")
        if not action_was_successful:
            self.logger.warning(f"  失败详情: {final_result_for_shuang}")