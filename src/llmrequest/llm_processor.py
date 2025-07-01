# 文件: llmrequest/llm_processor.py
# 我要修改这个文件，哼！

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Unpack  # 确保 Unpack 被导入

from src.common.custom_logging.logging_config import get_logger  # type: ignore # 假设这个导入是有效的，但找不到存根

from .utils_model import APIKeyError, GenerationParams, LLMClientError, NetworkError
from .utils_model import LLMClient as UnderlyingLLMClient

# 获取日志记录器实例
logger = get_logger(__name__)

# 定义回调函数类型，用于处理流式数据的块
# 参数：块数据，块类型（例如 'chunk', 'finish', 'error'），元数据字典
ChunkCallbackType = Callable[[Any, str, dict[str, Any] | None], Coroutine[Any, Any, None]]


class StreamInterruptError(Exception):
    """自定义异常，用于表示流处理被用户或程序逻辑主动中断。"""

    def __init__(self, message: str = "流处理被中断。", partial_data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.partial_data = partial_data  # 存储中断时可能已经处理或接收到的部分数据 #


class _StreamingWorkflowManager:
    """
    管理单个流式 LLM 请求的复杂细节，包括处理中断逻辑和基于块的回调。
    此类不直接暴露给外部用户，由 llm_processor.Client 内部使用。
    """

    def __init__(
        self,
        llm_client: UnderlyingLLMClient,  # 依赖注入底层的 LLMClient 实例 #
        chunk_callback: ChunkCallbackType | None = None,  # 用户提供的可选回调函数，用于处理每个数据块 #
    ) -> None:
        # 类型检查，确保传入的是正确的底层LLM客户端实例
        if not isinstance(llm_client, UnderlyingLLMClient):
            raise TypeError("llm_client 必须是 UnderlyingLLMClient 的一个实例。")

        self.llm_client: UnderlyingLLMClient = llm_client  # 持有底层LLM客户端的引用 #
        self.chunk_callback: ChunkCallbackType | None = chunk_callback  # 存储回调函数 #
        self._task_interruption_events: dict[str, asyncio.Event] = {}  # 存储每个 task_id 对应的中断事件 #

        # 用于方便地访问当前正在由 process_streaming_task 方法处理的任务的ID
        self.current_processing_task_id: str | None = None

        logger.info("_StreamingWorkflowManager 初始化完成。")
        if self.chunk_callback:
            logger.info(f"已注册流式数据块回调函数: {self.chunk_callback.__name__}")
        else:
            logger.info("未注册流式数据块回调函数。将使用 UnderlyingLLMClient 的默认流式行为（通常是打印到控制台）。")

        # # 这是一个重要的提示，关于当前回调机制的实现程度  # 并非重要，此为预期内行为
        # logger.debug( #
        #     "请注意：为了使 _StreamingWorkflowManager 的 chunk_callback 能够真正地以编程方式处理 *每一个单独的* 流式数据块， " #
        #     "utils_model.py 中的 UnderlyingLLMClient 类（特别是 _handle_streaming_response_for_style 方法）可能需要进行修改以支持此功能 " #
        #     "(例如，通过接受一个块接收器回调并逐块调用它，而不是自己打印或累积完整文本)。" #
        #     "当前此处的 chunk_callback 主要设计用于在流结束、出错或被中断时获得通知和最终/部分结果。" #
        # )

    async def _internal_chunk_handler(
        self,
        chunk_data: dict[str, Any] | str,  # 数据块内容 #
        chunk_type: str,  # 数据块类型，如 'finish', 'error', 'interrupted_finish' #
        metadata: dict[str, Any] | None = None,  # 相关的元数据 #
    ) -> None:
        """
        内部辅助方法，用于调用用户提供的 chunk_callback (如果存在)。
        这是对回调调用的一层封装，增加了错误处理。
        """
        if self.chunk_callback:  # 仅当回调函数被设置时才调用 #
            try:
                # 异步调用用户提供的回调函数
                await self.chunk_callback(chunk_data, chunk_type, metadata)
            except Exception as e:
                # 记录回调函数执行时发生的任何错误，但不应让回调的错误中断主流程
                logger.error(f"用户提供的 chunk_callback (类型: {chunk_type}) 执行时出错: {e}", exc_info=True)

    def _get_interruption_event(self, task_id: str) -> asyncio.Event:
        """
        为给定的 task_id 检索或创建一个 asyncio.Event 对象。
        此事件用于从外部发出中断信号。
        """
        # 如果该 task_id 还没有对应的事件，则创建一个新的
        if task_id not in self._task_interruption_events:
            self._task_interruption_events[task_id] = asyncio.Event()
        return self._task_interruption_events[task_id]

    def _clear_interruption_event(self, task_id: str) -> None:
        """
        当一个任务处理完毕（成功、失败或中断后），清除其对应的中断事件。
        这有助于释放资源并避免旧事件影响新任务（如果 task_id 可能被重用）。
        """
        if task_id in self._task_interruption_events:
            del self._task_interruption_events[task_id]

    async def interrupt_task(self, task_id: str) -> None:
        """
        向指定的流式任务（通过 task_id 标识）发送中断信号。
        如果任务正在运行，这将使其尝试优雅地停止。
        """
        if task_id in self._task_interruption_events:
            event: asyncio.Event = self._task_interruption_events[task_id]
            if not event.is_set():  # 仅当事件尚未被设置时才设置它 #
                logger.info(f"发送中断信号给流式任务 ID: {task_id}")
                event.set()  # 设置事件，通知正在监听此事件的协程 #
            else:
                logger.info(f"流式任务 ID: {task_id} 的中断信号先前已被设置。")
        else:
            # 如果任务ID未知或任务已完成（事件已被清除），则记录警告
            logger.warning(f"尝试中断一个未知的或已完成/清理的流式任务 ID: {task_id}")

    async def interrupt_current_processing_task(self) -> None:
        """
        一个便捷方法，用于中断当前正在由 process_streaming_task 方法处理的任务。
        """
        if self.current_processing_task_id:
            await self.interrupt_task(self.current_processing_task_id)
        else:
            logger.warning("当前没有正在通过 _StreamingWorkflowManager 处理的流式任务可以中断。")

    async def process_streaming_task(
        self,
        task_id: str,  # 任务的唯一标识符 #
        prompt: str,  # LLM 的文本提示 #
        # ███ 小懒猫改动开始 ███
        system_prompt: str | None = None,  # 新增 system_prompt 参数，哎，真麻烦
        # ███ 小懒猫改动结束 ███
        # 以下参数与 UnderlyingLLMClient.make_request 的参数对应
        is_multimodal: bool = False,
        image_inputs: list[str] | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_retries: int = 3,
        image_mime_type_override: str | None = None,
        **additional_generation_params: Unpack[GenerationParams],  # 其他特定于模型的生成参数 #
    ) -> dict[str, Any]:
        """
        处理单个流式 LLM 任务的核心逻辑。
        它会调用底层的 LLMClient 进行实际的 API 请求，并管理中断和回调。
        """
        self.current_processing_task_id = task_id  # 标记当前正在处理的任务ID #
        interruption_event: asyncio.Event = self._get_interruption_event(task_id)
        interruption_event.clear()  # 确保在任务开始时，中断事件是未设置状态 #

        logger.info(f"开始处理流式任务 ID: {task_id} (通过 _StreamingWorkflowManager)")
        if system_prompt:
            logger.info(
                f"  附带 System Prompt (前50字符): {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
            )

        final_result: dict[str, Any] = {}  # 用于存储最终结果的字典 #

        try:
            logger.debug(f"准备为流式任务 {task_id} 调用 UnderlyingLLMClient.make_request")

            # 调用底层 LLMClient 的 make_request 方法进行流式请求
            # 注意 is_stream 参数固定为 True
            result_from_llm_client: dict[str, Any] = await self.llm_client.make_request(
                prompt=prompt,
                # ███ 小懒猫改动开始 ███
                system_prompt=system_prompt,  # 把这个参数也传下去，累死我了
                # ███ 小懒猫改动结束 ███
                is_stream=True,  # 明确指示进行流式处理 #
                is_multimodal=is_multimodal,
                image_inputs=image_inputs,
                temp=temp,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                interruption_event=interruption_event,  # 将中断事件传递给底层客户端 #
                max_retries=max_retries,
                image_mime_type_override=image_mime_type_override,
                **additional_generation_params,  # 透传其他生成参数 #
            )
            final_result = result_from_llm_client  # 保存从底层客户端返回的结果 #

            # 准备回调函数的元数据
            callback_metadata: dict[str, Any] = {
                "task_id": task_id,
                "interrupted": final_result.get("interrupted", False),  # 结果中是否标记了中断 #
            }

            # 根据底层客户端返回的结果，决定回调类型和内容
            if final_result.get("error"):
                # 如果底层客户端返回错误
                error_type: str = final_result.get("type", "UnknownError")
                error_message: str = final_result.get("message", "UnderlyingLLMClient 中发生未知错误。")
                status_code: int | None = final_result.get("status_code")
                logger.error(
                    f"流式任务 {task_id} 处理失败 (来自 UnderlyingLLMClient): "
                    f"类型={error_type}, 状态码={status_code}, 消息='{error_message}'"
                )
                # 调用错误类型的回调
                await self._internal_chunk_handler(
                    {
                        "error_type": error_type,
                        "message": error_message,
                        "status_code": status_code,
                        "details": final_result.get("details"),  # 任何额外的错误详情 #
                    },
                    "error",  # 块类型为 'error' #
                    callback_metadata,
                )
            elif final_result.get("interrupted"):
                # 如果任务被中断
                logger.info(f"流式任务 {task_id} 被成功中断。将通过回调返回部分数据。")
                # 调用中断完成类型的回调
                await self._internal_chunk_handler(final_result, "interrupted_finish", callback_metadata)
            else:
                # 如果任务正常完成
                logger.info(f"流式任务 {task_id} 由 UnderlyingLLMClient 处理完成。")
                # 调用正常完成类型的回调
                await self._internal_chunk_handler(final_result, "finish", callback_metadata)

            return final_result  # 返回最终结果 #

        except (APIKeyError, NetworkError, LLMClientError) as e:
            # 捕获在调用底层客户端时可能发生的、已定义的客户端级别错误
            logger.error(
                f"流式任务 {task_id} 中发生可捕获的 UnderlyingLLMClient 错误: {type(e).__name__} - {e}",
                exc_info=True,
            )
            error_type_val: str = type(e).__name__
            message_val: str = str(e)
            status_code_val: int | None = getattr(e, "status_code", None)  # 尝试获取状态码 #

            final_result = {"error": True, "type": error_type_val, "message": message_val, "interrupted": False}
            if status_code_val is not None:
                final_result["status_code"] = status_code_val

            # 发生错误时，也尝试调用回调
            await self._internal_chunk_handler(final_result, "error", {"task_id": task_id, "interrupted": False})
            return final_result
        except Exception as e:
            # 捕获任何其他在流处理过程中发生的意外错误
            logger.error(f"流式任务 {task_id} 中发生未预期的严重错误: {e}", exc_info=True)
            final_result = {"error": True, "type": "UnhandledException", "message": str(e), "interrupted": False}
            # 发生严重错误时，也尝试调用回调
            await self._internal_chunk_handler(final_result, "error", {"task_id": task_id, "interrupted": False})
            return final_result
        finally:
            # 无论任务成功与否，最后都清除中断事件并重置当前处理的任务ID
            self._clear_interruption_event(task_id)
            if self.current_processing_task_id == task_id:
                self.current_processing_task_id = None


class Client:  # 这是 llm_processor.Client，是暴露给外部使用者的高级客户端 #
    """
    用于向 LLM 发出请求的统一高级客户端。
    它封装了底层 LLM API 交互的复杂性（通过 UnderlyingLLMClient），
    并提供了对流式请求的中断管理和回调机制（通过 _StreamingWorkflowManager）。
    这个类的构造函数现在负责创建和配置其内部使用的 UnderlyingLLMClient 实例。
    """

    def __init__(
        self,
        *,  # 强制所有后续参数都必须是关键字参数，以提高代码调用的清晰度 #
        # --- 用于配置内部 UnderlyingLLMClient 的参数 ---
        model: dict,  # 必需参数，指定模型提供商和名称，例如: {"provider": "GEMINI", "name": "gemini-pro"} #
        # 以下是 UnderlyingLLMClient 的可选配置参数，如果调用方不提供，
        # UnderlyingLLMClient 内部会使用其自身的默认值或从环境变量加载。
        abandoned_keys_config: list[str] | None = None,  # 废弃的API密钥列表 #
        proxy_host: str | None = None,  # 代理服务器主机地址 #
        proxy_port: int | None = None,  # 代理服务器端口号 #
        image_placeholder_tag: str | None = None,  # 图文混排时图像的占位符标签 #
        stream_chunk_delay_seconds: float | None = None,  # 流式输出时每个数据块之间的模拟延迟 #
        enable_image_compression: bool | None = None,  # 是否启用图像压缩 #
        image_compression_target_bytes: int | None = None,  # 图像压缩的目标字节大小 #
        rate_limit_disable_duration_seconds: int | None = None,  # API密钥因速率限制被临时禁用的时长 #
        # --- 用于流式处理的回调 ---
        chunk_callback: ChunkCallbackType | None = None,  # 可选的回调函数，用于处理流式响应的各个部分 #
        # --- 其他特定于模型的生成参数 (例如 temperature, max_output_tokens) ---
        # 这些参数将直接传递给 UnderlyingLLMClient 的构造函数或其请求方法。
        **kwargs: Unpack[GenerationParams],
    ) -> None:
        """
        初始化高级 LLM 客户端。
        此构造函数现在负责基于传入的参数创建其内部使用的 UnderlyingLLMClient 实例。
        """

        # 步骤1：准备用于实例化 UnderlyingLLMClient 的参数字典
        # 我们将明确传入的参数和 **kwargs 中的参数合并
        underlying_client_constructor_args: dict[str, Any] = {
            "model": model,  # 'model' 是必需的 #
            **kwargs,  # 将 kwargs 中的所有参数（如 temperature, maxOutputTokens 等）加入 #
        }

        # 对于可选参数，只有当调用者明确提供了非 None 值时，才将其加入构造参数字典，
        # 否则让 UnderlyingLLMClient 使用其内部定义的默认值或从环境变量加载。
        if abandoned_keys_config is not None:
            underlying_client_constructor_args["abandoned_keys_config"] = abandoned_keys_config
        if proxy_host is not None:
            underlying_client_constructor_args["proxy_host"] = proxy_host
        if proxy_port is not None:
            underlying_client_constructor_args["proxy_port"] = proxy_port
        if image_placeholder_tag is not None:
            underlying_client_constructor_args["image_placeholder_tag"] = image_placeholder_tag
        if stream_chunk_delay_seconds is not None:
            underlying_client_constructor_args["stream_chunk_delay_seconds"] = stream_chunk_delay_seconds
        if enable_image_compression is not None:
            underlying_client_constructor_args["enable_image_compression"] = enable_image_compression
        if image_compression_target_bytes is not None:
            underlying_client_constructor_args["image_compression_target_bytes"] = image_compression_target_bytes
        if rate_limit_disable_duration_seconds is not None:
            underlying_client_constructor_args["rate_limit_disable_duration_seconds"] = (
                rate_limit_disable_duration_seconds
            )

        # 步骤2：实例化底层的 UnderlyingLLMClient
        # 这个实例将由当前的 ProcessorClient 实例持有和使用
        self.llm_client: UnderlyingLLMClient = UnderlyingLLMClient(**underlying_client_constructor_args)

        # 步骤3：实例化流式工作流管理器，并传入已创建的底层LLM客户端
        self._streaming_manager: _StreamingWorkflowManager = _StreamingWorkflowManager(
            llm_client=self.llm_client,  # 将创建的底层客户端实例传递给流管理器 #
            chunk_callback=chunk_callback,  # 传递用户提供的回调函数 #
        )

        logger.info(
            f"LLM Processor Client 初始化完成。内部已创建并持有一个 UnderlyingLLMClient "
            f"(模型: {self.llm_client.model_name}, 提供商: {self.llm_client.provider})。"
        )

    async def make_llm_request(
        self,
        *,  # 强制关键字参数 #
        prompt: str | None = None,  # 对于嵌入请求，此参数可能为 None #
        # ███ 小懒猫改动开始 ███
        system_prompt: str | None = None,  # 新增 system_prompt 参数，又是我！
        # ███ 小懒猫改动结束 ███
        is_stream: bool,  # 指示是否进行流式请求 #
        task_id: str | None = None,  # 流式请求需要 task_id 以支持中断 #
        # 以下是与 UnderlyingLLMClient.make_request 和 get_embedding 对应的参数
        is_multimodal: bool = False,
        image_inputs: list[str] | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        image_mime_type_override: str | None = None,
        max_retries: int = 3,
        text_to_embed: str | None = None,  # 特定于嵌入请求 #
        **additional_generation_params: Unpack[GenerationParams],  # 其他特定于模型的生成参数 #
    ) -> dict[str, Any]:
        """
        向 LLM 发出请求（可能是文本补全、视觉问答、工具调用或嵌入）。
        此方法会根据参数决定是进行流式处理、非流式处理还是嵌入请求，
        并将请求路由到相应的内部处理器。
        """
        logger.info(
            f"LLM Processor Client 收到 make_llm_request 调用: "
            f"流式={is_stream}, TaskID={task_id if task_id else 'N/A'}, "
            f"嵌入={'是' if text_to_embed else '否'}, 多模态={is_multimodal}"
        )
        if system_prompt:
            logger.info(
                f"  make_llm_request 收到 System Prompt (前50字符): {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
            )

        # 优先处理嵌入请求的逻辑
        if text_to_embed:
            if is_stream:
                logger.warning("嵌入请求通常是非流式的。参数 'is_stream=True' 在此场景下将被忽略。")
            # 对于嵌入请求，其他一些参数（如 prompt, tools, image_inputs）通常不适用
            if prompt and prompt.strip():
                logger.warning("同时提供了 'prompt' 和 'text_to_embed'；对于嵌入请求，'prompt' 将被忽略。")
            if tools or image_inputs:
                logger.warning("为嵌入请求提供了 'tools' 或 'image_inputs'；这些参数将被忽略。")
            if system_prompt:
                logger.warning("为嵌入请求提供了 'system_prompt'；此参数将被忽略。哼，白传了！")

            # 准备传递给底层 get_embedding 方法的生成参数 (尽管嵌入通常参数较少)
            embedding_gen_params: GenerationParams = additional_generation_params.copy()
            if temp is not None:  # 虽然温度对嵌入不典型，但如果提供则透传 #
                embedding_gen_params["temperature"] = temp
            if max_tokens is not None:  # 最大token数对嵌入也不典型 #
                embedding_gen_params["maxOutputTokens"] = max_tokens

            logger.info("路由到内部 UnderlyingLLMClient.get_embedding 以进行非流式嵌入请求。")
            # self.llm_client 是 UnderlyingLLMClient 的实例
            return await self.llm_client.get_embedding(
                text_to_embed=text_to_embed,
                generation_params_override=embedding_gen_params if embedding_gen_params else None,
                max_retries=max_retries,
            )

        # 对于非嵌入请求，'prompt' 应该是有效的字符串
        if prompt is None or not isinstance(prompt, str):
            raise ValueError("非嵌入类型的 LLM 请求必须提供一个有效的 'prompt' 字符串。")

        # 根据 is_stream 参数决定是走流式处理还是非流式处理
        if is_stream:
            # 流式请求必须提供 task_id
            if not task_id:
                raise ValueError("流式请求 (is_stream=True) 必须提供一个 'task_id'。")

            logger.info(f"路由到内部 _StreamingWorkflowManager 以处理流式任务: {task_id}")
            # 将所有相关参数传递给流式工作流管理器的处理方法
            return await self._streaming_manager.process_streaming_task(
                task_id=task_id,
                prompt=prompt,
                # ███ 小懒猫改动开始 ███
                system_prompt=system_prompt,  # 唉，又是我，继续传参数
                # ███ 小懒猫改动结束 ███
                is_multimodal=is_multimodal,
                image_inputs=image_inputs,
                temp=temp,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                max_retries=max_retries,
                image_mime_type_override=image_mime_type_override,
                **additional_generation_params,  # 透传其他生成参数 #
            )
        else:  # 非流式、非嵌入请求 #
            logger.info("路由到内部 UnderlyingLLMClient.make_request 以进行非流式请求。")
            # 直接调用底层 LLMClient 的 make_request 方法
            # is_stream 参数固定为 False
            # UnderlyingLLMClient.make_request 内部会根据参数（如 tools, is_multimodal）确定具体的请求类型
            return await self.llm_client.make_request(
                prompt=prompt,
                # ███ 小懒猫改动开始 ███
                system_prompt=system_prompt,  # 最后一次，在这个文件里，我发誓！
                # ███ 小懒猫改动结束 ███
                is_stream=False,  # 明确指示非流式处理 #
                is_multimodal=is_multimodal,
                image_inputs=image_inputs,
                temp=temp,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                image_mime_type_override=image_mime_type_override,
                max_retries=max_retries,
                # interruption_event 对于非流式调用通常不传递或为 None
                **additional_generation_params,  # 透传其他生成参数 #
            )

    async def interrupt_stream_task(self, task_id: str) -> None:
        """
        便捷方法，用于向当前正在处理的指定流式任务发送中断信号。
        """
        logger.info(f"LLM Processor Client 尝试通过 _StreamingWorkflowManager 中断流式任务: {task_id}")
        await self._streaming_manager.interrupt_task(task_id)
