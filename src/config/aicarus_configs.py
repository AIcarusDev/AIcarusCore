from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List # 确保导入所有需要的类型

# 导入 ConfigBase
from .config_base import ConfigBase


@dataclass
class PersonaSettings(ConfigBase):
    """定义 AI 机器人的人格设置。
    包括名称、描述和个人资料信息。
    """
    
    bot_name: str = "霜"
    """AI 机器人的名称。"""

    description: str = ""
    """AI 机器人的描述信息。"""

    profile: str = ""
    """AI 机器人的个人资料信息。"""


@dataclass
class LLMClientSettings(ConfigBase):
    """定义 LLM 客户端的设置。
    包括 API 密钥、基础 URL、图像处理设置等。
    """
    
    image_placeholder_tag: str = "[IMAGE_HERE]"
    """图像占位符标签，用于指示图像位置的占位符文本。"""

    stream_chunk_delay_seconds: float = 0.05
    """流块延迟时间（秒），用于控制分块传输的速度。"""

    enable_image_compression: bool = True
    """是否启用图像压缩。"""

    image_compression_target_bytes: int = 1*1024*1024
    """图像压缩的目标大小（字节）。"""

    rate_limit_disable_duration_seconds: int = 1800
    """速率限制禁用持续时间（秒）。"""


@dataclass
class ModelParams(ConfigBase):
    """
    定义单个模型的参数。
    'provider' 字段至关重要，它决定了如何获取API密钥和基础URL。
    """
    
    provider: str  # 例如 "gemini", "openai", "ollama"
    """模型提供者的名称，用于确定如何获取 API 密钥和基础 URL。"""
    
    model_name: str
    """模型的名称，例如 "gemini-1.5-flash" 或 "gpt-4o"。"""

    temperature: Optional[float] = None
    """控制生成文本的随机性，值越高，输出越多样化。"""

    max_output_tokens: Optional[int] = None
    """生成文本的最大长度（以标记为单位）。"""
    
    top_p: Optional[float] = None
    """控制生成文本的多样性，使用 nucleus sampling 方法。"""
    
    top_k: Optional[int] = None
    """控制生成文本的多样性，使用 top-k sampling 方法。"""


@dataclass
class AllModelPurposesConfig(ConfigBase):
    """
    统一存放所有不同用途的模型配置。
    每个字段代表一个AI任务，其值是该任务所使用的模型参数 (ModelParams)。
    这个类现在将直接作为 AlcarusRootConfig 的一个字段。
    """
    
    main_consciousness: Optional[ModelParams] = None
    """主要意识模型，用于处理核心任务和对话。"""
    
    intrusive_thoughts: Optional[ModelParams] = None
    """侵入性思维模型，用于生成和处理侵入性思维。"""

    action_decision: Optional[ModelParams] = None
    """行动决策模型，用于生成和处理行动决策相关的思维。"""

    information_summary: Optional[ModelParams] = None
    """信息摘要模型，用于生成和处理信息摘要相关的思维。"""

    embedding_default: Optional[ModelParams] = None
    """嵌入模型，用于生成和处理文本嵌入相关的思维。"""
    # 如果未来有其他任务，例如图像生成、语音识别等，可以在这里添加新的字段。


@dataclass
class ServerSettings(ConfigBase):
    """服务器相关设置，包括主机和端口配置。"""

    host: str = "localhost"
    """服务器主机名或IP地址。"""

    port: int = 8077
    """服务器端口号。默认值为 8077。"""


@dataclass
class CoreLogicSettings(ConfigBase):
    """核心逻辑设置，包括 AI 的思考间隔时间等。"""

    thinking_interval_seconds: int = 30
    """思考间隔时间（秒），用于控制 AI 的思考频率。"""

@dataclass
class IntrusiveThoughtsSettings(ConfigBase):
    """侵入性思维模块的设置，包括启用状态、生成间隔和插入概率。"""

    enabled: bool = True
    """是否启用侵入性思维模块。"""

    generation_interval_seconds: int = 600
    """生成间隔时间（秒），用于控制侵入性思维的生成频率。"""

    insertion_probability: float = 0.15
    """插入概率，用于控制侵入性思维的插入频率。"""

@dataclass
class LoggingSettings(ConfigBase):
    """日志记录相关设置，包括日志级别和日志文件路径。"""

    pass


@dataclass
class InnerConfig(ConfigBase):
    """Aicarus 内部配置，包含版本号和协议版本。
    这些信息用于确保 Aicarus 的各个组件之间的兼容性。
    """

    version: str = "0.1.0"
    """AicarusCore 的版本号，便于跟踪和更新。"""

    protocol_version: str = "1.4.0"
    """Aicarus-Message-Protocol 标准通信协议版本号，确保与客户端和其他服务兼容。"""


@dataclass
class AlcarusRootConfig(ConfigBase):
    """Aicarus 的根配置类，包含所有核心设置和模型配置。
    这个类将作为 Aicarus 的主要配置入口点，包含所有必要的设置。
    """

    inner: InnerConfig
    llm_client_settings: LLMClientSettings
    persona: PersonaSettings
    core_logic_settings: CoreLogicSettings
    intrusive_thoughts_module_settings: IntrusiveThoughtsSettings
    llm_models: Optional[AllModelPurposesConfig] = field(default_factory=AllModelPurposesConfig)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
