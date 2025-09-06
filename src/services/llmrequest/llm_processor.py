# src/llmrequest/llm_processor.py
# LLM处理器模块，负责与语言模型进行交互并处理相关请求。

import asyncio
import copy
from collections.abc import Callable, Coroutine
from typing import Any, Unpack

from src.common.custom_logging.logging_config import get_logger

# 导入全局配置，以便我们能访问 fallback_model_name
from src.config import config

# 从新的 core.models 导入异常和类型定义
from .core.models import APIKeyError, GenerationParams, LLMClientError, NetworkError
from .llm_client import LLMClient as UnderlyingLLMClient

# 获取日志记录器实例
logger = get_logger(__name__)

ChunkCallbackType = Callable[[Any, str, dict[str, Any] | None], Coroutine[Any, Any, None]]


class StreamInterruptError(Exception):
    """自定义异常，用于表示流处理被用户或程序逻辑主动中断."""

    def __init__(
        self, message: str = "流处理被中断。", partial_data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.partial_data = partial_data


class _StreamingWorkflowManager:
    """管理单个流式 LLM 请求的复杂细节."""

    def __init__(
        self,
        llm_client: UnderlyingLLMClient,
        chunk_callback: ChunkCallbackType | None = None,
    ) -> None:
        self.llm_client: UnderlyingLLMClient = llm_client
        self.chunk_callback: ChunkCallbackType | None = chunk_callback
        self._task_interruption_events: dict[str, asyncio.Event] = {}
        self.current_processing_task_id: str | None = None
        logger.info("_StreamingWorkflowManager 初始化完成。")
        if self.chunk_callback:
            logger.info(f"已注册流式数据块回调函数: {self.chunk_callback.__name__}")
        else:
            logger.info("未注册流式数据块回调函数。")

    async def _internal_chunk_handler(
        self,
        chunk_data: dict[str, Any] | str,
        chunk_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.chunk_callback:
            try:
                await self.chunk_callback(chunk_data, chunk_type, metadata)
            except Exception as e:
                logger.error(
                    f"用户提供的 chunk_callback (类型: {chunk_type}) 执行时出错: {e}", exc_info=True
                )

    def _get_interruption_event(self, task_id: str) -> asyncio.Event:
        if task_id not in self._task_interruption_events:
            self._task_interruption_events[task_id] = asyncio.Event()
        return self._task_interruption_events[task_id]

    def _clear_interruption_event(self, task_id: str) -> None:
        if task_id in self._task_interruption_events:
            del self._task_interruption_events[task_id]

    async def interrupt_task(self, task_id: str) -> None:
        if task_id in self._task_interruption_events:
            event: asyncio.Event = self._task_interruption_events[task_id]
            if not event.is_set():
                logger.info(f"发送中断信号给流式任务 ID: {task_id}")
                event.set()
            else:
                logger.info(f"流式任务 ID: {task_id} 的中断信号先前已被设置。")
        else:
            logger.warning(f"尝试中断未知或已终止的流式任务 ID: {task_id}")

    async def interrupt_current_processing_task(self) -> None:
        if self.current_processing_task_id:
            await self.interrupt_task(self.current_processing_task_id)
        else:
            logger.warning("当前没有正在通过 _StreamingWorkflowManager 处理的流式任务可以中断。")

    async def process_streaming_task(
        self,
        task_id: str,
        prompt_parts: list[dict],
        system_prompt: str | None = None,
        is_multimodal: bool = False,
        image_inputs: list[str] | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_retries: int = 3,
        image_mime_type_override: str | None = None,
        use_google_search: bool = False,
        **additional_generation_params: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        self.current_processing_task_id = task_id
        interruption_event: asyncio.Event = self._get_interruption_event(task_id)
        interruption_event.clear()

        logger.info(f"开始处理流式任务 ID: {task_id} (通过 _StreamingWorkflowManager)")
        if system_prompt:
            logger.info(
                f"  附带 System Prompt (前50字符): {system_prompt[:50]}"
                f"{'...' if len(system_prompt) > 50 else ''}"
            )

        final_result: dict[str, Any] = {}

        try:
            logger.debug(f"准备为流式任务 {task_id} 调用 UnderlyingLLMClient.make_request")
            result_from_llm_client: dict[str, Any] = await self.llm_client.make_request(
                prompt_parts=prompt_parts,
                system_prompt=system_prompt,
                is_stream=True,
                is_multimodal=is_multimodal,
                image_inputs=image_inputs,
                temp=temp,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                interruption_event=interruption_event,
                max_retries=max_retries,
                image_mime_type_override=image_mime_type_override,
                use_google_search=use_google_search,
                **additional_generation_params,
            )
            final_result = result_from_llm_client

            callback_metadata: dict[str, Any] = {
                "task_id": task_id,
                "interrupted": final_result.get("interrupted", False),
            }

            if final_result.get("error"):
                error_type: str = final_result.get("type", "UnknownError")
                error_message: str = final_result.get(
                    "message", "UnderlyingLLMClient 中发生未知错误。"
                )
                status_code: int | None = final_result.get("status_code")
                logger.error(
                    f"流式任务 {task_id} 处理失败 (来自 UnderlyingLLMClient): "
                    f"类型={error_type}, 状态码={status_code}, 消息='{error_message}'"
                )
                await self._internal_chunk_handler(
                    {
                        "error_type": error_type,
                        "message": error_message,
                        "status_code": status_code,
                        "details": final_result.get("details"),
                    },
                    "error",
                    callback_metadata,
                )
            elif final_result.get("interrupted"):
                logger.info(f"流式任务 {task_id} 被成功中断。将通过回调返回部分数据。")
                await self._internal_chunk_handler(
                    final_result, "interrupted_finish", callback_metadata
                )
            else:
                logger.info(f"流式任务 {task_id} 由 UnderlyingLLMClient 处理完成。")
                await self._internal_chunk_handler(final_result, "finish", callback_metadata)

            return final_result

        except (APIKeyError, NetworkError, LLMClientError) as e:
            logger.error(
                f"流式任务 {task_id} 中发生可捕获的 UnderlyingLLMClient "
                f"错误: {type(e).__name__} - {e}",
                exc_info=True,
            )
            error_type_val: str = type(e).__name__
            message_val: str = str(e)
            status_code_val: int | None = getattr(e, "status_code", None)

            final_result = {
                "error": True,
                "type": error_type_val,
                "message": message_val,
                "interrupted": False,
            }
            if status_code_val is not None:
                final_result["status_code"] = status_code_val

            await self._internal_chunk_handler(
                final_result, "error", {"task_id": task_id, "interrupted": False}
            )
            return final_result
        except Exception as e:
            logger.error(f"流式任务 {task_id} 中发生未预期的严重错误: {e}", exc_info=True)
            final_result = {
                "error": True,
                "type": "UnhandledException",
                "message": str(e),
                "interrupted": False,
            }
            await self._internal_chunk_handler(
                final_result, "error", {"task_id": task_id, "interrupted": False}
            )
            return final_result
        finally:
            self._clear_interruption_event(task_id)
            if self.current_processing_task_id == task_id:
                self.current_processing_task_id = None


class Client:
    """用于向 LLM 发出请求的统一高级客户端."""

    def __init__(
        self,
        *,
        model: dict,
        abandoned_keys_config: list[str] | None = None,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        image_placeholder_tag: str | None = None,
        stream_chunk_delay_seconds: float | None = None,
        enable_image_compression: bool | None = None,
        image_compression_target_bytes: int | None = None,
        rate_limit_disable_duration_seconds: int | None = None,
        chunk_callback: ChunkCallbackType | None = None,
        **kwargs: Unpack[GenerationParams],
    ) -> None:
        self.underlying_client_constructor_args: dict[str, Any] = {
            "model": model,
            **kwargs,
        }
        if abandoned_keys_config is not None:
            self.underlying_client_constructor_args["abandoned_keys_config"] = abandoned_keys_config
        if proxy_host is not None:
            self.underlying_client_constructor_args["proxy_host"] = proxy_host
        if proxy_port is not None:
            self.underlying_client_constructor_args["proxy_port"] = proxy_port
        if image_placeholder_tag is not None:
            kwargs.pop("image_placeholder_tag", None)
            self.underlying_client_constructor_args["image_placeholder_tag"] = image_placeholder_tag
        if stream_chunk_delay_seconds is not None:
            self.underlying_client_constructor_args["stream_chunk_delay_seconds"] = (
                stream_chunk_delay_seconds
            )
        if enable_image_compression is not None:
            self.underlying_client_constructor_args["enable_image_compression"] = (
                enable_image_compression
            )
        if image_compression_target_bytes is not None:
            self.underlying_client_constructor_args["image_compression_target_bytes"] = (
                image_compression_target_bytes
            )
        if rate_limit_disable_duration_seconds is not None:
            self.underlying_client_constructor_args["rate_limit_disable_duration_seconds"] = (
                rate_limit_disable_duration_seconds
            )

        self.llm_client: UnderlyingLLMClient = UnderlyingLLMClient(
            **self.underlying_client_constructor_args
        )

        self._streaming_manager: _StreamingWorkflowManager = _StreamingWorkflowManager(
            llm_client=self.llm_client,
            chunk_callback=chunk_callback,
        )

        logger.info(
            f"LLM Processor Client 初始化完成。内部已创建并持有一个 UnderlyingLLMClient "
            f"(模型: {self.llm_client.model_name}, 提供商: {self.llm_client.provider})。"
        )

    async def make_llm_request(
        self,
        *,
        prompt: str | None = None,
        prompt_parts: list[dict] | None = None,
        system_prompt: str | None = None,
        is_stream: bool,
        task_id: str | None = None,
        is_multimodal: bool = False,
        image_inputs: list[str] | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        image_mime_type_override: str | None = None,
        max_retries: int = 3,
        text_to_embed: str | None = None,
        use_google_search: bool = False,
        response_schema: dict[str, Any] | None = None,
        **additional_generation_params: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        """Make a request to the LLM with specified parameters.

        Parameters
        ----------
        prompt : str | None, optional
            The main prompt text for the LLM request.
        system_prompt : str | None, optional
            System-level instructions for the LLM.
        is_stream : bool
            Whether to use streaming mode for the request.
        task_id : str | None, optional
            Unique identifier for streaming tasks (required when is_stream=True).
        is_multimodal : bool, default False
            Whether the request includes multimodal inputs.
        image_inputs : list[str] | None, optional
            List of image inputs for multimodal requests.
        temp : float | None, optional
            Temperature parameter for response generation.
        max_tokens : int | None, optional
            Maximum number of tokens in the response.
        tools : list[dict[str, Any]] | None, optional
            Available tools for function calling.
        tool_choice : str | dict[str, Any] | None, optional
            Tool selection strategy.
        image_mime_type_override : str | None, optional
            Override for image MIME type detection.
        max_retries : int, default 3
            Maximum number of retry attempts for failed requests.
        text_to_embed : str | None, optional
            Text to generate embeddings for (alternative to prompt).
        use_google_search : bool, default False
            Whether to enable Google search functionality.
        response_schema : dict[str, Any] | None, optional
            Schema for structured response validation.
        **additional_generation_params : GenerationParams
            Additional parameters for LLM generation.

        Returns:
        -------
        dict[str, Any]
            Response from the LLM containing generated text, embeddings, or error information.

        Raises:
        ------
        ValueError
            If required parameters are missing or invalid.
        """
        logger.info(
            f"LLM Processor Client 收到 make_llm_request 调用: "
            f"流式={is_stream}, TaskID={task_id if task_id else 'N/A'}, "
            f"嵌入={'是' if text_to_embed else '否'}, 多模态={is_multimodal}"
        )
        if system_prompt:
            logger.info(
                f"  make_llm_request 收到 System Prompt (前50字符): {system_prompt[:50]}"
                f"{'...' if len(system_prompt) > 50 else ''}"
            )

        if response_schema:
            additional_generation_params["responseSchema"] = response_schema

        if text_to_embed:
            if is_stream:
                logger.warning("嵌入请求通常是非流式的。参数 'is_stream=True' 在此场景下将被忽略。")
            if prompt and prompt.strip():
                logger.warning(
                    "同时提供了 'prompt' 和 'text_to_embed'；对于嵌入请求，'prompt' 将被忽略。"
                )
            if tools or image_inputs:
                logger.warning("为嵌入请求提供了 'tools' 或 'image_inputs'；这些参数将被忽略。")
            if system_prompt:
                logger.warning("为嵌入请求提供了 'system_prompt'；此参数在嵌入请求中无效")

            embedding_gen_params: GenerationParams = additional_generation_params.copy()
            if temp is not None:
                embedding_gen_params["temperature"] = temp
            if max_tokens is not None:
                embedding_gen_params["maxOutputTokens"] = max_tokens

            logger.info("路由到内部 UnderlyingLLMClient.get_embedding 以进行非流式嵌入请求。")
            return await self.llm_client.get_embedding(
                text_to_embed=text_to_embed,
                max_retries=max_retries,
                **embedding_gen_params,
            )

        if (prompt is None or not isinstance(prompt, str)) and prompt_parts is None:
            raise ValueError(
                "非嵌入类型的 LLM 请求必须提供一个有效的 'prompt' 字符串或 'prompt_parts' 列表。"
            )

        final_prompt_parts = prompt_parts
        if prompt:
            final_prompt_parts = [{"text": prompt}]

        if is_stream:
            if not task_id:
                raise ValueError("流式请求 (is_stream=True) 必须提供一个 'task_id'。")

            logger.info(f"路由到内部 _StreamingWorkflowManager 以处理流式任务: {task_id}")
            return await self._streaming_manager.process_streaming_task(
                task_id=task_id,
                prompt_parts=final_prompt_parts,
                system_prompt=system_prompt,
                is_multimodal=is_multimodal,
                image_inputs=image_inputs,
                temp=temp,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                max_retries=max_retries,
                image_mime_type_override=image_mime_type_override,
                use_google_search=use_google_search,
                **additional_generation_params,
            )
        else:
            logger.info("路由到内部 UnderlyingLLMClient.make_request 以进行非流式请求。")
            result = await self.llm_client.make_request(
                prompt_parts=final_prompt_parts,
                system_prompt=system_prompt,
                is_stream=False,
                is_multimodal=is_multimodal,
                image_inputs=image_inputs,
                temp=temp,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                image_mime_type_override=image_mime_type_override,
                max_retries=max_retries,
                use_google_search=use_google_search,
                **additional_generation_params,
            )
            logger.debug(f"主模型返回结果，准备进行升避检查。返回内容: {str(result)[:200]}...")
            should_fallback = (
                not is_stream
                and not result.get("error")
                and not (result.get("text") or "").strip()
                and config.test_function.fallback_model_name
            )

            if should_fallback:
                fallback_model_name = config.test_function.fallback_model_name
                logger.warning(
                    f"主模型 '{self.llm_client.model_name}' 返回空内容，"
                    f"将尝试使用备用模型 '{fallback_model_name}' 进行重试..."
                )

                try:
                    fallback_client_args = copy.deepcopy(self.underlying_client_constructor_args)
                    fallback_client_args["model"]["name"] = fallback_model_name
                    fallback_client = UnderlyingLLMClient(**fallback_client_args)

                    fallback_result = await fallback_client.make_request(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        is_stream=False,
                        is_multimodal=is_multimodal,
                        image_inputs=image_inputs,
                        temp=temp,
                        max_tokens=max_tokens,
                        tools=tools,
                        tool_choice=tool_choice,
                        image_mime_type_override=image_mime_type_override,
                        max_retries=max_retries,
                        use_google_search=use_google_search,
                        **additional_generation_params,
                    )

                    logger.info(f"备用模型 '{fallback_model_name}' 调用完成。")
                    return fallback_result

                except Exception as e:
                    logger.error(
                        f"尝试使用备用模型 '{fallback_model_name}' 时发生严重错误: {e}",
                        exc_info=True,
                    )
                    return result

            return result

    async def interrupt_stream_task(self, task_id: str) -> None:
        """中断指定的流式任务.

        Parameters
        ----------
        task_id : str
            要中断的流式任务的唯一标识符.
        """
        logger.info(
            f"LLM Processor Client 尝试通过 _StreamingWorkflowManager 中断流式任务: {task_id}"
        )
        await self._streaming_manager.interrupt_task(task_id)
