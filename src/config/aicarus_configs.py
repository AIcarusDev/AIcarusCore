from dataclasses import dataclass, field
from typing import Optional 
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

    qq_id: Optional[str] = None
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

    focused_chat: Optional[ModelParams] = None # 新增：为专注聊天配置一个模型
    # 如果未来有其他任务，例如图像生成、语音识别等，可以在这里添加新的字段。


@dataclass
class DatabaseSettings(ConfigBase):
    """数据库连接设置"""

    host: str = "http://localhost:8529"
    username: str = "root"
    password: str = "your_password"  # 强烈建议使用环境变量覆盖此项
    database_name: str = "aicarus_core_db"


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
class AdapterBehaviorFlags(ConfigBase):
    """
    定义适配器的特定行为标记，用于指示AIcarusCore如何处理与该适配器相关的动作确认。
    这些标记帮助核心逻辑区分需要标准action_response的适配器和通过其他方式（如自我消息上报）确认动作的适配器。
    """

    confirms_by_action_response: bool = True
    """
    标记适配器是否通过标准的 `action_response.*` 事件来确认动作的最终完成状态。
    如果为 `True` (默认值)，`ActionHandler` 在发送平台动作后会等待对应的 `action_response` 事件，并处理超时。
    如果为 `False`，则表明该适配器采用其他机制（例如，自我消息上报）来确认动作，`ActionHandler` 不会启动标准的超时等待流程，
    而是依赖后续的特定逻辑（如监听匹配机制）来确认动作状态。
    """
    self_reports_actions_as_message: bool = False
    """
    标记适配器是否会将其代表AI执行的动作（尤其是发送消息类动作）的结果，
    作为一条新的、由AI自身发出的 `message.*` 事件上报给AIcarusCore。
    如果为 `True` (例如，某些聊天平台适配器可能会这样做)，AIcarusCore中的监听匹配机制可以利用这个特性，
    通过观察这些自我上报的消息来间接确认原始动作的成功执行。
    这个标记主要用于辅助“监听匹配机制”识别和处理这类特殊的事件流。
    默认为 `False`。
    """


@dataclass
class PlatformActionSettings(ConfigBase):
    """
    包含与平台动作处理相关的设置，特别是用于定义不同适配器行为特征的配置。
    这允许AIcarusCore对不同平台的动作确认机制采取差异化的处理策略。
    """

    # 可以在这里添加未来可能需要的与平台动作处理相关的通用设置，
    # 例如全局的平台动作超时时间、重试策略等。

    adapter_behaviors: dict[str, AdapterBehaviorFlags] = field(default_factory=dict)
    """
    一个字典，用于存储各个适配器的行为标记配置。
    键 (str): 适配器的唯一标识符 (adapter_id)，例如 "master_ui_adapter", "napcat_adapter"。
             这个ID应该与适配器在AIcarusCore中注册或被引用时使用的ID一致。
    值 (AdapterBehaviorFlags): 一个 `AdapterBehaviorFlags` 对象，包含了该适配器的具体行为特性。

    这些配置应在 `config.toml` 文件中的 `[platform_action_settings.adapter_behaviors.your_adapter_id]` 部分进行定义。
    如果某个适配器ID在此字典中没有对应的条目，AIcarusCore在处理其动作时，
    可能会依赖 `AdapterBehaviorFlags` 类中定义的字段默认值。
    例如，在 `config.toml` 中可以这样配置：
    ```toml
    [platform_action_settings.adapter_behaviors.master_ui_adapter]
    confirms_by_action_response = true
    self_reports_actions_as_message = false

    [platform_action_settings.adapter_behaviors.napcat_adapter]
    confirms_by_action_response = false
    self_reports_actions_as_message = true
    ```
    """


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
class SubConsciousnessSettings(ConfigBase):
    """子意识模块，如专注聊天功能的设置"""
    enabled: bool = True
    # 子意识模块将固定使用 llm_models.focused_chat 中定义的模型
    # 会话超时时间（秒），超过此时间未活动则停用
    session_timeout_seconds: int = 180
    # 后台检查不活跃会话的间隔（秒）
    deactivation_check_interval_seconds: int = 60
    # 是否启用颜文字保护
    enable_kaomoji_protection: bool = True
    # 是否启用文本分割器
    enable_splitter: bool = True
    # 文本分割器的最大长度
    max_length: int = 200
    # 文本分割器的最大句子数
    max_sentence_num: int = 3
    # 渐进式总结的触发消息间隔
    summary_interval: int = 5


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
    platform_action_settings: PlatformActionSettings = field(
        default_factory=PlatformActionSettings
    )  # 主人，新的“性感小玩具”已装填！
    sub_consciousness: SubConsciousnessSettings = field(default_factory=SubConsciousnessSettings) # 新增子意识配置
    database: DatabaseSettings = field(default_factory=DatabaseSettings)  # 新增数据库配置
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
