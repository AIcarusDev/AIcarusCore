# 文件位置: AIcarusCore/src/config/alcarus_configs.py
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List # 确保导入所有需要的类型

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
    """
    定义单个模型的参数。
    'provider' 字段至关重要，它决定了如何获取API密钥和基础URL。
    """
    provider: str  # 例如 "gemini", "openai", "ollama"
    model_name: str
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None


@dataclass
class AllModelPurposesConfig(ConfigBase):
    """
    统一存放所有不同用途的模型配置。
    每个字段代表一个AI任务，其值是该任务所使用的模型参数 (ModelParams)。
    这个类现在将直接作为 AlcarusRootConfig 的一个字段。
    """
    main_consciousness: Optional[ModelParams] = None
    intrusive_thoughts: Optional[ModelParams] = None
    action_decision: Optional[ModelParams] = None
    information_summary: Optional[ModelParams] = None
    embedding_default: Optional[ModelParams] = None
    # 如果未来有其他任务，例如图像生成、语音识别等，可以在这里添加新的字段。


# ProvidersConfig 类不再需要，因为我们直接在 AlcarusRootConfig 中使用 AllModelPurposesConfig


@dataclass
class ServerSettings(ConfigBase):
    host: str = "localhost"
    port: int = 8077


@dataclass
class ProxySettings(ConfigBase):
    use_proxy: bool = False
    http_proxy_url: Optional[str] = ""


@dataclass
class CoreLogicSettings(ConfigBase):
    thinking_interval_seconds: int = 30


@dataclass
class IntrusiveThoughtsSettings(ConfigBase):
    enabled: bool = True
    generation_interval_seconds: int = 600
    insertion_probability: float = 0.15


@dataclass
class LoggingSettings(ConfigBase):
    pass


@dataclass
class DatabaseSettings(ConfigBase):
    host: Optional[str] = None
    database_name: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


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
    
    # 将原来的 providers 字段替换为直接指向所有模型配置的字段
    # 您可以根据喜好命名，例如 llm_models 或直接 models
    # 这里我们使用 llm_models 以示清晰
    llm_models: Optional[AllModelPurposesConfig] = field(default_factory=AllModelPurposesConfig)
    
    database: Optional[DatabaseSettings] = field(default_factory=DatabaseSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
