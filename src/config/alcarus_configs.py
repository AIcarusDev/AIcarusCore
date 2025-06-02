# AIcarusCore/src/config/alcarus_configs.py
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List # 🐾 小猫爪：确保导入 List

# 导入 ConfigBase
from .config_base import ConfigBase


@dataclass
class PersonaSettings(ConfigBase):
    bot_name: str = "霜"
    description: str = ""
    profile: str = ""


@dataclass
class LLMClientSettings(ConfigBase):
    image_placeholder_tag: str = "[IMAGE_HERE]"
    stream_chunk_delay_seconds: float = 0.05
    enable_image_compression: bool = True
    image_compression_target_bytes: int = 1048576
    rate_limit_disable_duration_seconds: int = 1800


@dataclass
class ModelParams(ConfigBase):
    provider: str
    model_name: str
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None


@dataclass
class ProviderModels(ConfigBase):
    main_consciousness: Optional[ModelParams] = None
    intrusive_thoughts: Optional[ModelParams] = None
    action_decision: Optional[ModelParams] = None
    information_summary: Optional[ModelParams] = None
    embedding_default: Optional[ModelParams] = None
    # 🐾 小猫爪：为子思维聊天回复新增的LLM配置字段！
    sub_mind_chat_reply: Optional[ModelParams] = None


@dataclass
class ProviderSettings(ConfigBase):
    models: Optional[ProviderModels] = None


@dataclass
class ProvidersConfig(ConfigBase):
    gemini: Optional[ProviderSettings] = None
    openai: Optional[ProviderSettings] = None
    # 🐾 小猫爪：如果未来有更多提供商，可以在这里添加


@dataclass
class DatabaseSettings(ConfigBase):
    pass


@dataclass
class ProxySettings(ConfigBase):
    use_proxy: bool = False
    http_proxy_url: Optional[str] = "" # 🐾 小猫爪：保持 Optional[str]


@dataclass
class CoreLogicSettings(ConfigBase):
    thinking_interval_seconds: int = 30
    chat_history_context_duration_minutes: int = 10 # 🐾 小猫爪：之前讨论中提到，这里明确一下


@dataclass
class IntrusiveThoughtsSettings(ConfigBase):
    enabled: bool = True
    generation_interval_seconds: int = 600
    insertion_probability: float = 0.15


@dataclass
class LoggingSettings(ConfigBase):
    pass


@dataclass
class InnerConfig(ConfigBase):
    version: str


@dataclass
class AlcarusRootConfig(ConfigBase):
    inner: InnerConfig
    llm_client_settings: LLMClientSettings
    persona: PersonaSettings
    proxy: ProxySettings
    core_logic_settings: CoreLogicSettings
    intrusive_thoughts_module_settings: IntrusiveThoughtsSettings
    providers: Optional[ProvidersConfig] = None # 🐾 小猫爪：保持 Optional
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
