# src/action/components/llm_client_factory.py
import os
from typing import Any

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.llmrequest.llm_processor import Client as ProcessorClient

logger = get_logger(__name__)


class LLMClientFactory:
    """
    一个工厂类，负责根据配置文件创建和初始化LLM客户端。
    这有助于将LLM客户端的创建逻辑与使用逻辑分离。
    """

    def __init__(self) -> None:
        logger.info(f"{self.__class__.__name__} instance created.")

    def create_client(self, purpose_key: str) -> ProcessorClient:
        """
        根据指定的用途从配置中创建一个 ProcessorClient 实例。

        Args:
            purpose_key: 在配置中定义的模型用途键 (例如, "action_decision")。

        Returns:
            一个配置好的 ProcessorClient 实例。

        Raises:
            RuntimeError: 如果配置缺失或无效，导致无法创建客户端。
        """
        try:
            if not config.llm_models:
                msg = "配置错误：AlcarusRootConfig 中缺少 'llm_models' 配置段。"
                logger.error(msg)
                raise RuntimeError(msg)

            model_params_cfg = getattr(config.llm_models, purpose_key, None)
            if not model_params_cfg or not hasattr(model_params_cfg, "provider"):
                msg = (
                    f"配置错误：在 AlcarusRootConfig.llm_models 下未找到模型用途键 '{purpose_key}' 对应的有效模型配置。"
                )
                logger.error(msg)
                raise RuntimeError(msg)

            actual_provider_name_str: str = model_params_cfg.provider
            actual_model_name_str: str = model_params_cfg.model_name
            if not actual_provider_name_str or not actual_model_name_str:
                msg = f"配置错误：模型 '{purpose_key}' 未指定 'provider' 或 'model_name'。"
                logger.error(msg)
                raise RuntimeError(msg)

            general_llm_settings_obj = config.llm_client_settings
            final_proxy_host = os.getenv("HTTP_PROXY_HOST")
            final_proxy_port_str = os.getenv("HTTP_PROXY_PORT")
            final_proxy_port = (
                int(final_proxy_port_str) if final_proxy_port_str and final_proxy_port_str.isdigit() else None
            )

            if final_proxy_host and final_proxy_port:
                logger.info(
                    f"LLM客户端工厂将为 '{purpose_key}' 使用环境变量中的代理: {final_proxy_host}:{final_proxy_port}"
                )

            model_for_client = {
                "provider": actual_provider_name_str.upper(),
                "name": actual_model_name_str,
            }
            model_specific_kwargs: dict[str, Any] = {
                "temperature": model_params_cfg.temperature,
                "maxOutputTokens": model_params_cfg.max_output_tokens,
                "top_p": model_params_cfg.top_p,
                "top_k": model_params_cfg.top_k,
            }

            processor_args = {
                "model": model_for_client,
                "proxy_host": final_proxy_host,
                "proxy_port": final_proxy_port,
                **vars(general_llm_settings_obj),
                **model_specific_kwargs,
            }

            final_args = {k: v for k, v in processor_args.items() if v is not None}
            client_instance = ProcessorClient(**final_args)

            logger.info(
                f"成功创建 ProcessorClient 实例用于 '{purpose_key}' (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
            )
            return client_instance

        except (AttributeError, TypeError) as e_conf:
            msg = f"配置访问或类型错误 (用途: {purpose_key}): {e_conf}"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg) from e_conf
        except Exception as e:
            msg = f"创建LLM客户端时发生未知错误 (用途: {purpose_key}): {e}"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg) from e
