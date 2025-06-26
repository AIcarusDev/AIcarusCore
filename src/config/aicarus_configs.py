from dataclasses import dataclass, field

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

    qq_id: str | None = None
    """AI 机器人的QQ号。"""


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

    image_compression_target_bytes: int = 1 * 1024 * 1024
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

    temperature: float | None = None
    """控制生成文本的随机性，值越高，输出越多样化。"""

    max_output_tokens: int | None = None
    """生成文本的最大长度（以标记为单位）。"""

    top_p: float | None = None
    """控制生成文本的多样性，使用 nucleus sampling 方法。"""

    top_k: int | None = None
    """控制生成文本的多样性，使用 top-k sampling 方法。"""


@dataclass
class AllModelPurposesConfig(ConfigBase):
    """
    统一存放所有不同用途的模型配置。
    每个字段代表一个AI任务，其值是该任务所使用的模型参数 (ModelParams)。
    这个类现在将直接作为 AlcarusRootConfig 的一个字段。
    """

    main_consciousness: ModelParams | None = None
    """主要意识模型，用于处理核心任务和对话。"""

    intrusive_thoughts: ModelParams | None = None
    """侵入性思维模型，用于生成和处理侵入性思维。"""

    action_decision: ModelParams | None = None
    """行动决策模型，用于生成和处理行动决策相关的思维。"""

    information_summary: ModelParams | None = None
    """信息摘要模型，用于生成和处理信息摘要相关的思维。"""

    embedding_default: ModelParams | None = None
    """嵌入模型，用于生成和处理文本嵌入相关的思维。"""

    focused_chat: ModelParams | None = None
    """专注聊天模型，用于处理专注聊天相关的思维。"""


@dataclass
class DatabaseSettings(ConfigBase):
    """数据库连接设置
    此处无需修改，无需配置文件中创建对应配置项。该配置将直接被环境变量覆盖。
    """

    host: str = "http://localhost:8529"
    """数据库主机地址。默认值为 http://localhost:8529。"""

    username: str = "root"
    """数据库用户名。默认值为 root。"""

    password: str = "your_password"
    """数据库密码。默认值为 your_password。"""

    database_name: str = "aicarus_core_db"
    """数据库名称。默认值为 aicarus_core_db。"""


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
class FocusChatModeSettings(ConfigBase):
    """专注聊天模式的设置"""

    enabled: bool = True
    """是否启用子意识模块"""

    enable_dynamic_bot_profile: bool = True
    """是否启用动态获取机器人自身信息的功能。如果禁用，将回退到使用 persona 中的静态配置。"""

    session_timeout_seconds: int = 180
    """子意识模块将固定使用 llm_models.focused_chat 中定义的模型
    会话超时时间（秒），超过此时间未活动则停用"""

    deactivation_check_interval_seconds: int = 60
    """后台检查不活跃会话的间隔（秒）"""

    enable_kaomoji_protection: bool = True
    """是否启用颜文字保护"""

    enable_splitter: bool = True
    """是否启用文本分割器"""

    max_length: int = 200
    """文本分割器的最大长度"""

    max_sentence_num: int = 3
    """文本分割器的最大句子数"""

    summary_interval: int = 5
    """渐进式总结的触发消息间隔"""


@dataclass
class TestFunctionConfig(ConfigBase):
    """测试功能配置类，用于测试和调试目的。
    这个类将包含一些测试相关的设置，未来可能会被移除。
    """

    enable_test_group: bool = False
    """是否启用测试模式。"""

    test_group: list[str] = field(default_factory=list)
    """测试群组列表，用于指定哪些群组启用测试功能。"""


@dataclass
class SpeakerWeightEntry(ConfigBase):
    """定义单个发言者的权重条目。"""

    id: str
    """发言者的唯一ID。"""
    weight: float
    """该发言者的权重因子。"""
    name: str | None = None  # name是可选的，给个备注方便看~
    """发言者的名字（可选，仅用于备注）。"""


@dataclass
class InterruptModelConfig(ConfigBase):
    """中断模型配置类，用于定义中断模型的相关设置。
    这个类将包含中断模型的名称和其他相关参数。
    """

    objective_keywords: list[str] = field(default_factory=list)
    """中断模型的目标关键词列表，用于识别需要中断的消息。"""

    core_importance_concepts: list[str] = field(default_factory=list)
    """中断模型的核心重要概念列表，用于识别需要中断的消息。"""

    speaker_weights: list[SpeakerWeightEntry] = field(default_factory=list)
    """发言者权重列表，用于调整不同发言者的中断权重。"""


@dataclass
class RuntimeEnvironmentSettings(ConfigBase):
    """运行时环境设置，包括临时文件目录等。
    这些设置用于配置 Aicarus 在运行时的环境参数。
    """

    temp_file_directory: str = "/tmp/aicarus_temp_images"
    """临时文件目录，用于存储运行时生成的临时文件。默认值为 /tmp/aicarus_temp_images。"""


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
    llm_models: AllModelPurposesConfig | None = field(default_factory=AllModelPurposesConfig)
    test_function: TestFunctionConfig = field(default_factory=TestFunctionConfig)
    focus_chat_mode: FocusChatModeSettings = field(default_factory=FocusChatModeSettings)  # 新增专注聊天配置
    database: DatabaseSettings = field(default_factory=DatabaseSettings)  # 新增数据库配置
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    interrupt_model: InterruptModelConfig = field(default_factory=InterruptModelConfig)
    runtime_environment: RuntimeEnvironmentSettings = field(default_factory=RuntimeEnvironmentSettings)
