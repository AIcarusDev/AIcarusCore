# 重构计划：动态获取并使用机器人自身详细信息

**日期:** 2025-06-17

**作者:** 小懒猫

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

## 2. 现状分析

经过代码分析，目前 `AIcarusCore` 与 `AIcarus-Napcat-adapter` 之间存在以下问题：

1.  **信息缺失**: Adapter 在通过 `meta.lifecycle.connect` 事件向 Core 注册时，只提供了 `adapter_id` 和 `display_name`，并未包含机器人自身的详细 `UserInfo`。
2.  **缺少主动查询机制**: Core 没有现成的机制可以主动向 Adapter 请求获取机器人自身的详细信息。现有的 `action` 都是执行操作（如撤回、戳一戳），而非数据查询。

因此，必须通过新增通信协议和逻辑来实现此功能。

## 3. 设计方案

本方案采用“Core主动请求，Adapter响应”的模式，分两阶段进行。

### 第一阶段：修改 `AIcarus-Napcat-adapter`

在 Adapter 端增加一个新的 `action`，用于响应 Core 的信息查询请求。

1.  **定义新动作 (`action_definitions.py`)**:
    -   创建一个新的 Handler 类 `GetBotInfoHandler`。
    -   该 Handler 负责处理名为 `action.bot.get_self_info` 的新动作。
    -   `execute` 方法逻辑：
        -   调用 Napcat API 获取机器人自身信息（如 `get_self_info`）以及在特定群聊中的信息（如 `get_group_member_info`）。
        -   为了获取群内信息（群名片、头衔等），此动作需要接收一个可选的 `group_id` 参数。如果 `group_id` 未提供，则只返回全局信息（如昵称）。
        -   将获取到的信息组装成一个 `aicarus_protocols.UserInfo` 对象。
        -   将此 `UserInfo` 对象作为 `action_response` 事件中 `details` 字段的内容返回给 Core。
    -   在 `ACTION_HANDLERS` 字典中注册 `action.bot.get_self_info`。

### 第二阶段：修改 `AIcarusCore`

在 Core 端实现请求发送、信息缓存和最终使用的逻辑。

1.  **新增平台工具 (`tools/platform_actions.py`)**:
    -   创建一个新的异步方法，例如 `async def get_bot_profile(adapter_id: str, group_id: Optional[str] = None) -> Optional[UserInfo]:`。
    -   此方法会构建一个 `action.bot.get_self_info` 动作事件，包含可选的 `group_id`。
    -   通过 `ActionSender` 将该动作发送给指定的 `adapter_id`。
    -   **关键**: `ActionSender` 和 `ActionHandler` 需要支持请求-响应模式，能够等待并返回 `action_response` 的结果。这可能需要引入一个基于 `event_id` 的 `Future` 对象来同步等待。

2.  **引入信息缓存**:
    -   在 Core 的一个合适的全局状态管理模块中（例如 `config/global_config.py` 或新建一个 `core_logic/bot_profile_cache.py`），创建一个缓存（如一个字典）来存储获取到的机器人 `UserInfo`。
    -   缓存的 key 可以是 `adapter_id` 或 `adapter_id:group_id` 的组合，以区分不同群聊中的身份信息。
    -   系统在需要时调用 `get_bot_profile`，并将获取结果存入缓存，避免重复请求。

3.  **修改目标逻辑 (`sub_consciousness/chat_prompt_builder.py`)**:
    -   在构建 `user_map` 的地方，修改原有逻辑。
    -   首先尝试从缓存中读取对应当前会话（`conversation_info`）的机器人 `UserInfo`。
    -   **降级逻辑 (重要!)**:
        -   如果缓存中没有，或者获取失败（`get_bot_profile` 返回 `None`），则必须退回到当前的硬编码逻辑，以保证核心功能不中断。
        -   获取到 `UserInfo` 对象后，安全地访问其字段（如 `user_info.user_nickname`），并进行逻辑判断：
            -   `nick` 使用 `user_info.user_nickname`，如果为空则使用默认值 "机器人"。
            -   `card` 优先使用 `user_info.user_cardname`，如果为空，则使用 `nick` 的值。
            -   `title` 使用 `user_info.user_titlename`，如果为空则为空字符串。
            -   `perm` 使用 `user_info.permission_level`，如果为空则为 "成员"。

## 4. 风险与对策

1.  **风险**: `ActionSender`/`Handler` 的请求-响应改造可能复杂。
    -   **对策**: 在改造时充分测试，确保不影响现有的单向 `action` 流程。可以为带响应的 `action` 设置一个超时时间。

2.  **风险**: 网络问题或 Adapter 未启动，导致 `get_bot_profile` 请求失败。
    -   **对策**: `get_bot_profile` 方法必须有完善的异常处理（如 `try...except`），在任何失败情况下都应返回 `None`，触发调用方的降级逻辑。

3.  **风险**: `chat_prompt_builder.py` 中因获取信息失败而崩溃。
    -   **对策**: 严格遵守第3节中描述的**降级逻辑**，确保在任何情况下都有一个保底的、可用的 `user_map`。

4.  **风险**: 获取到的 `UserInfo` 字段不全（例如私聊时 `user_cardname` 为 `None`）。
    -   **对策**: 在使用 `UserInfo` 对象的字段时，必须进行 `None` 值检查，并提供默认值或备用逻辑（如 `card` 使用 `nick`）。

---
**结论:** 该方案在逻辑上是完整的，但执行时需特别注意错误处理和降级机制的实现，以确保系统的稳定性和鲁棒性。
