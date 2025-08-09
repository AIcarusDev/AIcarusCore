# AIcarus Prompt 编写通用指导

## 前言

该文档皆在对 AIcarus 项目内所有对 LLM 展现的文本部分（prompt/json schema）进行概念与理念上的统一与规范化，以便于给未来所有的 prompt 编写者构建一个基本的概念与指导。

---

### 1. 回归本质：使用直观、核心的词汇

无论是 prompt 还是 json schema 的字段，在我们展现给 LLM 的部分，都应该遵循模型“直觉”认知，优先使用模型能“第一时间”正确识别，认识的词语/句子/概念。而不是创造一个新的定义，或偏离本质定义。

**核心思想**：LLM 不是一个会“学习”新定义的数据库，它是在根据海量文本训练出的概率分布来“预测”下一个词。因此，我们提供的词汇越接近其训练数据的核心概念，它的理解就越准确，行为也越可预测。

#### 反例：

- “慢思考”功能的原始 json schema：

```json
"spawn_lite_pipelines": {
    "type": "object",
    "description": "【仅用于高风险决策】触发一次性的内部辩论，以权衡利弊。",
    "properties": {
        "motivation": {
            "type": "string",
        },
        "pipelines": {
            "type": "array",
            "description": "代表不同策略或观点的思考管线，数量限制在2-5个。",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "此观点的简短标签，如'风险确认策略'。",
                    },
                    "initial_thought": {
                        "type": "string",
                        "description": "此观点的详细初始想法。",
                    },
                },
                "required": ["tag", "initial_thought"],
            },
        },
    },
    "required": ["motivation", "pipelines"],
},
```

- **欠佳原因**：缺乏直观性，创造了不必要的理解负担

  - `spawn_lite_pipelines`：这是一个非常技术化且小众的术语，其“衍生轻量级管线”的含义偏离了“进行深入思考”这个功能的核心。
  - `pipelines`：在通用语言环境中，“管道”一词通常不直接等同于“观点”或“策略”。
  - **重定向描述**：后续通过 description 字段来“强行解释”这些术语的真实意图（例如用“内部辩论”来解释 spawn_lite_pipelines），这无疑增加了模型的理解成本和出现偏差的风险。

- **改进**：

```json
"deep_think": {  // 定义为 "deep_think"，去除不必要的理解门槛。
    "type": "object",
    "description": "进行理性的深度思考，在遇到陌生、复杂、抽象问题，或高风险的决策时使用。",  // 更加符合直觉，常识的解释。
    "properties": {
        "motivation": {"type": "string"},
        "opinions": {  // 观点使用"opinions"，对应中文“观点”。
            "type": "array",
            "description": "需要讨论的不同观点或策略，数量限制在2-5个。",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "你对此观点或策略的简短标签。",
                    },
                    "initial_thought": {
                        "type": "string",
                        "description": "你对此观点或策略的详细初始想法。",
                    },
                },
                "required": ["tag", "initial_thought"],
            },
        },
    },
    "required": ["motivation", "opinions"],
},
```

- **讲解**：
  - JSON schema 的字段名是 prompt 的一部分。LLM 对高频核心英文词汇的“直觉”理解能力，远胜于通过 description 进行二次解释的中文。
  - 强制使用中文重新定向本质上几乎相当于：
  ```json
  "fruit": { // 中文翻译：水果
    "type": "object",
    "description": "在这里面写入家具",
    "properties": {
        "variety":{"type": "string",  "description": "家具的种类"}
    }
  }
  ```
  - 所以，我们应该尽可能使用 LLM 熟知的，能够第一反应的概念，而不是增加不必要的理解门槛。

### 2. 专业中性：避免情绪化

在 prompt 的指令部分，应该避免使用夸张化，情绪化描述，而是尽可能使用中性，较为专业的描述方式。

#### 反例：

- 指令 prompt 节选：

```text
# 核心思维戒律

1.  **现实至上原则 (The Reality Supremacy Principle):**

    - `<reality_update>` 中的信息，是此刻宇宙中唯一、绝对、不容置疑的真理！
    - 你的所有思考【必须】以`<reality_update>`中的最新信息为起点。

2.  **记忆参考原则 (The Memory Reference Principle):**

    - `<internal_info>`是你过去的“回忆快照”。
    - 当`<reality_update>`与你的“回忆”冲突时，你的“回忆”【必须】被视为过时的、无效的！你必须立刻抛弃旧的想法，拥抱新的现实！

3.  **导航日志使用法则 (The Navigation Log Usage Law):**
    - `<navigation_log>`是你的**记忆辅助**，而不是你的**任务列表**！它的唯一作用是告诉你“你从哪里来”，为你使用`back`和`jump_to_history`指令提供依据。
    - 你的**首要任务永远是处理当前焦点`[T-0]`** 的内容！只有在当前任务明确完成，或你的目标就是“回溯”时，你才能使用导航指令。
    - **严禁**将历史焦点（如[T-1]）的内容与当前焦点（[T-0]）的内容混为一谈！
```

- **欠佳原因**：过度夸张/情绪化。

  - 使用类似“此刻宇宙中唯一、绝对、不容置疑的真理！”等描述，可能会使 LLM 的输出产生非预期，不稳定的负面效果。
  - 大量使用感叹号和夸张修辞是“坏”的 prompt 实践。
  - 过度 prompt（“`<navigation_log>`是你的**记忆辅助**，而不是你的**任务列表**！”，llm 在理解了记忆辅助作用后，其实已经不会把这当作一种“任务列表”）可能反而导致非预期错误。

- **改进**：

```text
# 思维指导

1.  **现实优先:**

    - `<reality_update>` 中的信息是此刻客观发生的，绝对真实的现实状态。
    - 你的所有思考必须以`<reality_update>`中的最新信息为起点。

2.  **记忆参考:**

    - `<history_internal_info>`是你过去(上一轮思考)的“回忆快照”。
    - 当`<reality_update>`中的信息与你的“回忆”冲突时，说明现实情况已经发生了变化，你可以以之前的思考为参考，以新的信息为基础继续思考。

3.  **导航日志使用:**
    - `<navigation_log>`是你的记忆辅助，它的作用是告诉你“你从哪里来”，为你使用`back`和`jump_to_history`等注意力转移指令提供依据。
    - 你的**首要目标是处理当前专注`[T-0]`** 的内容。
```

- **讲解**：
  - 对自回归模型而言，无论角色设定部分有多特殊/跳脱，在长上下文的指令部分，中性、沉稳、专业的描述是产生高质量、稳定输出的最佳实践。

### 3. 结构清晰：善用分隔，如无必要勿增实体

Prompt 的不同部分（如指令、上下文、用户信息、示例）应该有清晰的结构和边界。同时，在标记结构时，保持简洁，不要增加不必要的词语。

#### 反例：

- 构建 prompt 的 XML 块常见方式：

```text
<system_rule_pool>...</system_rule_pool>

<persona_pool>...</persona_pool>

<info_pool>...</info_pool>
```

- **欠佳原因**：不必要描述（`pool`）。

  - `pool`（池）这个词在此处是冗余的。`<system_rule>` 本身已经清晰地界定了这是一个关于系统规则的区块。虽然 llm 可以理解这类描述，但是通常来说，这是一种不必要的消耗。
  - 过多不必要的、重复的修饰词，会增加模型的认知负荷与消耗，并可能在长文本中引入不必要的噪声。

- **改进**：

```text
<system_rule>
...
</system_rule>

<persona>
...
</persona>

<info>
...
</info>
```

- **讲解**：

  - 简洁、明确、一致的结构化标签是最好的。让标签名直接反映其内容本质。
