# src/config/alcarus_configs.py
from dataclasses import dataclass

# 导入我们刚刚创建的 ConfigBase
from .config_base import ConfigBase  # 使用相对导入，因为它们在同一个包 (src.config)


@dataclass
class PersonaSettings(ConfigBase):
    bot_name: str = "霜"  # 可以给一个默认值
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
    abandoned_keys_env_var: str | None = None  # 示例，可以设为 None


# --- LLM Purpose Definitions ---
@dataclass
class LLMPurpose(ConfigBase):
    provider: str
    model_key_in_toml: str


# --- Specific Model Parameters (within a provider) ---
@dataclass
class ModelParams(ConfigBase):
    model_name: str
    temperature: float | None = None  # 让温度等参数可选
    max_output_tokens: int | None = None
    top_p: float | None = None
    top_k: int | None = None


# --- Provider Configuration ---
@dataclass
class ProviderModels(ConfigBase):  # 用于 [providers.gemini.models]
    main_consciousness: ModelParams | None = None  # 键名应与 toml 中的完全一致
    intrusive_thoughts: ModelParams | None = None
    action_decision: ModelParams | None = None  # 之前是 action_decision_model
    information_summary: ModelParams | None = None  # 之前是 information_summary_model
    embedding_default: ModelParams | None = None
    # ... Alcarus 可能需要的其他模型key，例如您在 official_configs.py 中看到的各种模型用途


@dataclass
class ProviderSettings(ConfigBase):
    api_keys_env_var: str
    base_url_env_var: str
    models: ProviderModels  # 嵌套的 ProviderModels


@dataclass
class ProvidersConfig(ConfigBase):  # 用于 [providers]
    gemini: ProviderSettings | None = None
    openai: ProviderSettings | None = None
    # ... 其他提供商


# --- Database Settings ---
@dataclass
class DatabaseSettings(ConfigBase):
    # mongodb_connection_string_env_var: str # 移除 MongoDB 特定的配置
    arangodb_host_env_var: str = "ARANGODB_HOST"
    arangodb_user_env_var: str = "ARANGODB_USER"
    arangodb_password_env_var: str = "ARANGODB_PASSWORD"
    arangodb_database_env_var: str = "ARANGODB_DATABASE"


# --- Proxy Settings ---
@dataclass
class ProxySettings(ConfigBase):
    use_proxy: bool = False
    http_proxy_url: str | None = ""  # "" 或 None 都可以作为默认


# --- Core Logic Settings ---
@dataclass
class CoreLogicSettings(ConfigBase):
    thinking_interval_seconds: int = 30
    time_format_string: str = "%Y年%m月%d日 %H点%M分%S秒"


# --- Intrusive Thoughts Settings ---
@dataclass
class IntrusiveThoughtsSettings(ConfigBase):
    enabled: bool = True
    generation_interval_seconds: int = 600
    insertion_probability: float = 0.15


# --- Logging Settings ---
@dataclass
class LoggingSettings(ConfigBase):
    app_log_level_env_var: str = "APP_LOG_LEVEL"
    # default_app_log_level: str = "INFO" # 如果想在toml中硬编码默认值
    pymongo_log_level_env_var: str = (
        "PYMONGO_LOG_LEVEL"  # 这个可以保留，或者如果ArangoDB驱动有自己的日志控制，可以相应修改
    )
    asyncio_log_level_env_var: str = "ASYNCIO_LOG_LEVEL"
    llm_client_log_level_env_var: str = "LLM_CLIENT_LOG_LEVEL"


# --- Inner Version Control ---
@dataclass
class InnerConfig(ConfigBase):
    version: str


# --- Root Configuration Class for Alcarus ---
@dataclass
class AlcarusRootConfig(ConfigBase):
    inner: InnerConfig  # 必须有，且与 toml 中的 [inner] 对应
    llm_client_settings: LLMClientSettings
    main_llm_settings: LLMPurpose
    intrusive_llm_settings: LLMPurpose
    action_llm_settings: LLMPurpose
    summary_llm_settings: LLMPurpose
    providers: ProvidersConfig
    database: DatabaseSettings
    proxy: ProxySettings
    core_logic_settings: CoreLogicSettings
    intrusive_thoughts_module_settings: IntrusiveThoughtsSettings  # 注意键名与 toml 一致
    logging: LoggingSettings
    persona: PersonaSettings
