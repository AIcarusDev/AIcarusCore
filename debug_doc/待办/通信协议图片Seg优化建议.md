# 通信协议中图片 Seg 的优化建议

**负责人：** 通信协议开发人员

**问题背景：**
当前机器人项目的原生多模态图片识别功能失效。经过排查，发现机器人无法真正“看到”聊天中的图片，而是将其转换为一个纯文本标签（如 `[IMG:...]`），导致无法利用大语言模型的多模态能力。

---

## 问题根源分析

问题的核心在于从**通信协议**到**核心逻辑**的数据流转过程中，图片的关键信息（Base64编码）被“隐式”传递并最终被忽略。

### 1. 消息源头 `AIcarus-Napcat-adapter` (行为正确)

- **文件:** `AIcarus-Napcat-adapter/src/recv_handler_aicarus.py`
- **分析:** Adapter 在接收到图片消息后，正确地下载了图片，将其转换为 Base64 编码，并与 `url`, `file_id` 一同放入了 `Seg` 对象的 `data` 字典中。
- **代码片段:**
  ```python
  # file: AIcarus-Napcat-adapter/src/recv_handler_aicarus.py

  elif seg_type == NapcatSegType.image:
      image_url = seg_data.get("url")
      image_base64 = await get_image_base64_from_url(image_url) # 正确获取了 base64
      aicarus_s = Seg(
          type="image",
          data={
              "url": image_url,
              "file_id": seg_data.get("file"),
              "base64": image_base64, # 正确打包了 base64
          },
      )
  ```
- **结论:** 源头数据处理正确。

### 2. 通信协议 `AIcarus-Message-Protocol` (设计缺陷)

- **文件:** `AIcarus-Message-Protocol/src/aicarus_protocols/seg.py`
- **分析:** `Seg` 对象的定义过于灵活，`image` 类型的 `Seg` 没有为 `base64` 提供一个明确的、一等公民的字段。它依赖于调用者和使用者之间对 `data` 字典内部结构的“默契”。`SegBuilder.image` 辅助函数也没有 `base64` 的显式参数，加剧了这个问题。
- **代码片段:**
  ```python
  # file: AIcarus-Message-Protocol/src/aicarus_protocols/seg.py

  @dataclass
  class Seg:
      type: str
      data: Dict[str, Any]  # 过于通用，没有强制图片类型的结构

  class SegBuilder:
      @staticmethod
      def image(url: str = "", file_id: str = "", **kwargs) -> Seg:
          # base64 只能通过 kwargs 传入，没有在函数签名中明确定义
          data = {"url": url, "file_id": file_id}
          data.update(kwargs)
          return Seg(type="image", data=data)
  ```
- **结论:** 协议的设计使得 `base64` 数据成为了一个“隐式”字段，容易被下游忽略。

### 3. 核心逻辑 `AIcarusCore` (实现缺失)

- **文件:** `AIcarusCore/src/sub_consciousness/chat_prompt_builder.py`
- **分析:** 核心逻辑在构建 Prompt 时，完全印证了通信协议设计缺陷带来的问题。开发者显然只处理了协议中明确定义的字段 (`url`, `file_id`)，完全忽略了可能存在于 `data` 字典中的 `base64` 数据。最终，图片被转换成了一个无意义的文本标签。
- **代码片段:**
  ```python
  # file: AIcarusCore/src/sub_consciousness/chat_prompt_builder.py

  elif seg.type == "image":
      main_content_type = "IMG"
      # 只认 file_id 和 url，完全没看 base64
      img_src = seg.data.get("file_id") or seg.data.get("url", "unknown_image")
      main_content_parts.append(f"[IMG:{img_src.split('/')[-1][:15]}]") # 转换为了纯文本
  ```
- **结论:** 核心逻辑没有实现真正的多模态内容构建，是功能失效的直接原因。

---

## 修改建议

为了从根本上解决此问题，建议对**通信协议**和**核心逻辑**进行如下修改：

### 1. 优化通信协议 (`AIcarus-Message-Protocol`)

**目标:** 将 `base64` 提升为 `image` Seg 的标准、可选字段。

**修改文件:** `AIcarus-Message-Protocol/src/aicarus_protocols/seg.py`

**建议方案:**
修改 `SegBuilder.image` 函数，为其添加 `base64` 参数。虽然 `Seg` 的 `data` 结构不变，但通过 `Builder` 模式可以向开发者明确协议的最佳实践。

```python
# file: AIcarus-Message-Protocol/src/aicarus_protocols/seg.py

class SegBuilder:
    @staticmethod
    def image(url: str = "", file_id: str = "", base64: Optional[str] = None, **kwargs) -> Seg:
        """
        创建图片 Seg。
        
        Args:
            url (str): 图片的URL。
            file_id (str): 图片的文件ID或路径。
            base64 (Optional[str]): 图片的 Base64 编码字符串。
            **kwargs: 其他附加数据。
        """
        data = {"url": url, "file_id": file_id}
        if base64:
            data["base64"] = base64
        data.update(kwargs)
        return Seg(type="image", data=data)

```
**理由:**
- 通过在 `Builder` 中明确 `base64` 参数，为下游开发者提供了清晰的指引。
- 保持了向后兼容性，旧代码仍然可以工作。
- 这是最直接、改动最小且能明确意图的方式。

### 2. 升级核心逻辑 (`AIcarusCore`)

**目标:** 使 `ChatPromptBuilder` 能够处理图片 `Seg` 中的 `base64` 数据，并构建符合大模型要求的多模态 Prompt。

**修改文件:** `AIcarusCore/src/sub_consciousness/chat_prompt_builder.py`

**建议方案:**
修改对 `image` Seg 的处理逻辑，使其检查 `base64` 字段，并按照目标 LLM 的格式（例如 OpenAI Vision 格式）构建内容。

**注意:** 以下代码为**示例**，具体实现需要根据 `ChatPromptBuilder` 的最终输出格式进行调整。当前 `ChatPromptBuilder` 的输出是纯字符串，需要将其改造为支持多模态消息体（如 `List[Dict[str, Any]]`）。

```python
# file: AIcarusCore/src/sub_consciousness/chat_prompt_builder.py

# ... 在处理 event_data_log.content 的循环中 ...

# 这是一个概念性修改，实际代码需要调整 ChatPromptBuilder 的返回类型和整体结构
# 以支持 OpenAI Vision 等多模态模型的输入格式 (List[Dict[str, Any]])

elif seg.type == "image":
    image_base64 = seg.data.get("base64")
    image_url = seg.data.get("url")

    if image_base64:
        # 如果有 base64，构建符合多模态模型要求的数据结构
        # 例如 OpenAI Vision 格式
        image_content_block = {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{image_base64}"
            }
        }
        # !! 注意：main_content_parts 需要能接收字典，而不仅仅是字符串
        main_content_parts.append(image_content_block)
    elif image_url:
        # 如果没有 base64 但有 url，可以降级为文本标签或尝试让模型通过url访问
        main_content_parts.append(f"[图片, URL: {image_url}]")
    else:
        main_content_parts.append("[一张无法显示的图片]")

```

**理由:**
- 这是实现真·多模态能力的关键一步。
- 优先使用 `base64` 可以保证模型一定能“看到”图片，避免因网络问题或访问权限导致 `url` 不可用的情况。
- 提供了降级处理方案，保证在没有 `base64` 时程序不会崩溃。

---

**总结:**
完成以上两步修改后，从 Adapter 到核心的数据链路才能完整地传递图片信息，从而使机器人具备真正的多模态识图能力。
