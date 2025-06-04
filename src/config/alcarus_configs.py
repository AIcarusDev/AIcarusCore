from dataclasses import dataclass, field
from typing import Any  # 添加 Any 的导入

# 导入 ConfigBase
from .config_base import ConfigBase


@dataclass
class PersonaSettings(ConfigBase):
    bot_name: str = "霜"
    description: str = ""
    profile: str = ""


# --- LLMClient Settings ---
@dataclass
class LLMClientSettings(ConfigBase):
    image_placeholder_tag: str = "[IMAGE_HERE]"
    stream_chunk_delay_seconds: float = 0.05
    enable_image_compression: bool = True
    image_compression_target_bytes: int = 1048576
    rate_limit_disable_duration_seconds: int = 1800
    # abandoned_keys_env_var 已被移除


# --- Specific Model Parameters (within a provider) ---
@dataclass
class ModelParams(ConfigBase):
    provider: str  # 新增：明确指定此模型配置属于哪个提供商
    model_name: str
    temperature: float | None = None
    max_output_tokens: int | None = None
    top_p: float | None = None
    top_k: int | None = None
    # 可以根据需要添加其他特定于模型的参数，例如 vision_config 等


# --- Provider Configuration ---
@dataclass
class ProviderModels(ConfigBase):  # 用于 [providers.<provider_name>.models]
    # 这些字段名需要与 TOML 中定义的模型用途键名完全一致
    # 例如：main_consciousness, intrusive_thoughts, action_decision, information_summary
    # 我们使用 dict[str, ModelParams] 来更灵活地处理不同用途的模型
    # 或者，如果用途是固定的，可以继续像之前那样显式列出，但类型是 ModelParams
    main_consciousness: ModelParams | None = None
    intrusive_thoughts: ModelParams | None = None
    action_decision: ModelParams | None = None
    information_summary: ModelParams | None = None
    embedding_default: ModelParams | None = None
    # 如果您希望更动态地处理模型用途，可以使用以下方式：
    # _dynamic_models: dict[str, ModelParams] = field(default_factory=dict)
    # 但这需要 ConfigBase.from_dict 支持解析到这种动态字典中，或者在加载后进行额外处理。
    # 为简单起见，暂时显式列出已知用途。


@dataclass
class ProviderSettings(ConfigBase):  # 用于 [providers.<provider_name>]
    # api_keys_env_var 和 base_url_env_var 已被移除
    # models 字段现在直接持有 ProviderModels
    models: ProviderModels | None = None  # 改为可选，因为一个provider可能只定义了API而没有具体模型


@dataclass
class ProvidersConfig(ConfigBase):  # 用于 [providers] 表
    # 键名应与 toml 中的提供商名称一致 (例如 "gemini", "openai")
    gemini: ProviderSettings | None = None
    openai: ProviderSettings | None = None
    # 可以添加其他提供商...
    # 为了更动态地处理，也可以考虑使用 dict[str, ProviderSettings]
    # _dynamic_providers: dict[str, ProviderSettings] = field(default_factory=dict)
    # 同样，为简单起见，暂时显式列出。


# --- Server Settings ---
@dataclass
class ServerSettings(ConfigBase):
    host: str = "localhost"
    port: int = 8077


# --- Proxy Settings ---
@dataclass
class ProxySettings(ConfigBase):
    use_proxy: bool = False
    http_proxy_url: str | None = ""


# --- Core Logic Settings ---
@dataclass
class CoreLogicSettings(ConfigBase):
    thinking_interval_seconds: int = 30
    # time_format_string 已被移除


# --- Intrusive Thoughts Settings ---
@dataclass
class IntrusiveThoughtsSettings(ConfigBase):
    enabled: bool = True
    generation_interval_seconds: int = 600
    insertion_probability: float = 0.15


# --- Logging Settings ---
@dataclass
class LoggingSettings(ConfigBase):
    # 所有 *_env_var 字段已被移除。
    # 此数据类现在为空，但保留它作为配置结构的一部分。
    # 代码将直接从固定名称的环境变量读取日志级别。
    pass  # 表示这是一个空类


# --- Inner Version Control ---
@dataclass
class InnerConfig(ConfigBase):
    version: str


# --- Root Configuration Class for Alcarus ---
@dataclass
class AlcarusRootConfig(ConfigBase):
    inner: InnerConfig  # 没有默认值，必须提供
    llm_client_settings: LLMClientSettings  # 没有默认值，必须提供
    persona: PersonaSettings  # 没有默认值，必须提供
    proxy: ProxySettings  # 没有默认值，必须提供
    core_logic_settings: CoreLogicSettings  # 没有默认值，必须提供
    intrusive_thoughts_module_settings: IntrusiveThoughtsSettings  # 没有默认值，必须提供
    providers: ProvidersConfig | None = None
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)  # 添加server配置
