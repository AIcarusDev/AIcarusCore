# 重构回顾：动态获取并使用机器人自身详细信息

**日期:** 2025-06-23

**作者:** 小懒猫

**状态:** 已完成

---

## 1. 目标

当前在 `chat_prompt_builder.py` 中，机器人自身的 `user_map` 信息（如昵称、群名片等）是硬编码的。本次重构的目标是实现动态获取这些信息，使其与机器人在QQ平台上的实际信息保持一致。

具体需求如下：
- **"uid_str"**: 保持为固定的 "U0"。
- **"nick"**: 从 Napcat-Adapter 端获取机器人真实的QQ昵称。
- **"card"**: 从 Napcat-Adapter 端获取机器人在当前群聊的群名片。
  - 如果是私聊场景，则跳过此项。
  - 如果在群内没有设置群名片，则令 `card` 的值等于 `nick` 的值。
- **"title"**: 从 Napcat-Adapter 端获取机器人在当前群聊的头衔，如果没有则为空字符串。
- **"perm"**: 从 Napcat-Adapter 端获取机器人在当前群聊的权限（如“群主”、“管理员”、“成员”）。

## 2. 探索过程

最初的设计方案是正确的，即通过“Core主动请求，Adapter响应”的模式。但在实现过程中，发现对 `AIcarusCore` 和 `AIcarus-Napcat-adapter` 之间的通信机制理解有偏差。

经过对 `action_definitions.py`, `send_handler_aicarus.py`, `aic_com_layer.py`, `message_queue.py`, `main_aicarus.py`, `recv_handler_aicarus.py` 和 `utils.py` 等多个文件的深入分析，最终确定了正确的 API 调用链路：

-   **Core to Adapter**: Core 通过 WebSocket 向 Adapter 发送 `action` 事件。
-   **Adapter to Napcat**: Adapter 通过另一个独立的 WebSocket 连接，使用带 `echo` ID 的请求-响应模式调用 Napcat 的原生 API。
-   **关键 API**: 最终确认，获取机器人信息所需的 Napcat API 为 `get_login_info`（获取自身信息）和 `get_group_member_info`（获取群成员信息），这两个 API 已在 `utils.py` 中被封装为 `napcat_get_self_info` 和 `napcat_get_member_info`。

## 3. 实现总结

最终的实现方案与最初的设计基本一致，但细节更为清晰和准确。

### 第一阶段：修改 `AIcarus-Napcat-adapter`

在 `src/action_definitions.py` 中新增了 `GetBotProfileHandler`：

1.  **定义新动作**:
    -   处理名为 `action.bot.get_profile` 的新动作。
    -   `execute` 方法逻辑：
        -   调用 `utils.napcat_get_self_info()` 获取机器人全局信息（QQ号、昵称）。
        -   如果 `action` 中包含 `group_id`，则继续调用 `utils.napcat_get_member_info()` 获取在该群的特定信息（群名片、头衔、权限）。
        -   将所有信息打包成一个字典，作为 `action_response.success` 事件中 `details` 字段的内容返回给 Core。
    -   在 `ACTION_HANDLERS` 字典中注册了 `action.bot.get_profile`。

### 第二阶段：修改 `AIcarusCore`

在 Core 端实现了完整的请求-等待-响应逻辑。

1.  **新增请求-响应机制 (`src/action/action_handler.py`)**:
    -   在 `ActionHandler` 类中增加了一个新的公共方法 `send_action_and_wait_for_response`。
    -   此方法封装了发送 `action`、创建并注册 `Future`、等待 `action_response` 或超时的完整逻辑，为内部工具提供了一个便捷的同步调用接口。

2.  **新增平台工具 (`src/tools/platform_actions.py`)**:
    -   创建了新的异步工具函数 `async def get_bot_profile(...)`。
    -   此函数负责构建 `action.bot.get_profile` 事件。
    -   它调用 `ActionHandler` 的新方法 `send_action_and_wait_for_response` 来执行动作并获取包含机器人信息的字典。

3.  **修改目标逻辑 (`src/sub_consciousness/chat_prompt_builder.py`)**:
    -   修改了 `ChatPromptBuilder` 的 `__init__` 方法，使其能够接收并持有 `ActionHandler` 的实例（该实例由 `ChatSession` 传入）。
    -   在 `build_prompts` 方法中，构建 `user_map` 时，调用 `tools.platform_actions.get_bot_profile()`。
    -   **实现了降级逻辑**: 如果 `get_bot_profile` 调用失败或返回 `None`，则退回到使用 `config.toml` 中的静态配置，确保了系统的鲁棒性。

---
**结论:** 重构成功。系统现在可以动态、准确地获取机器人在不同聊天环境下的身份信息，同时代码结构更加清晰，核心模块的职责也更加明确。
