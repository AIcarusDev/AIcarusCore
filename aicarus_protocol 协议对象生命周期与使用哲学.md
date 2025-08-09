## **协议对象生命周期与使用哲学**

### 1. 神圣边界原则

`Event` 对象及其包含的 `UserInfo`, `ConversationInfo`, `Seg` 等，其本质是 **数据传输对象 (Data Transfer Object, DTO)**。它们的核心使命是在两个独立的系统（如 Core 和 Adapter）之间进行**序列化**和**网络传输**。

- **MUST (必须):** `Event` 对象**必须**被视为“一次性”的。它在系统的入口处（如 Adapter 接收到平台消息）被创建，或在系统的出口处（如 Core 指示 Adapter 执行动作）被创建。
- **MUST NOT (绝不):** **绝不**应该将一个 `Event` 对象实例在 Core 的内部业务逻辑中（例如，从消息处理模块传递到动作决策模块）持续引用和传递。这是一种**严重的概念混淆和架构耦合**。
- **SHOULD (应当):** Core 内部**应当**拥有自己独立的**领域模型 (Domain Models)**。当一个 `Event` 进入 Core 后，**应当**被立即解析，其数据被提取并填充到 Core 的内部领域模型中。Core 的所有业务逻辑都基于这些内部模型进行。
- **SHOULD (应当):** 当 Core 需要通过 Adapter 执行动作时，**应当**使用其内部领域模型的数据来**组装**一个全新的 `Event` 对象，然后将其发送出去。

### 2. 为什么要有这个原则？

- **解耦 (Decoupling):** 保证 Core 的内部实现可以自由演进，而不受通信协议版本的束缚。只要出入口的“翻译官”能正确组装和解析 `Event`，内部逻辑怎么改都行。
- **纯净性 (Purity):** 保持通信协议的纯净，只包含通信双方都必须理解的信息。像“动机(motivation)”这类纯属 Core 内部的业务逻辑，**绝不**应该污染协议。
- **健壮性 (Robustness):** 避免将核心业务逻辑依赖于协议中**可选 (OPTIONAL)** 或可能变更的字段（如 `raw_data`）。把“动机”塞进 `action_params` 也是一种危险行为，因为 `action_params` 的目的是定义**动作本身**的参数（比如踢谁、禁言多久），而不是定义**为什么**要执行这个动作。

**一句话总结：`Event` 是用来“通信”的信件，不是用来“思考”的大脑。**

```python
# 伪代码示例

# 1. 某个插件或业务逻辑模块决定要踢人
def process_user_message(event: Event):
    if "捣乱" in event.get_text_content():
        # 2. 构建要发送给 Adapter 的纯净 Action Event
        kick_action_event = Event(
            event_id=EventBuilder.generate_event_id(),
            event_type="action.qq.kick_member",
            time=EventBuilder.get_current_timestamp(),
            bot_id=event.bot_id,
            content=[
                Seg(
                    type="action_params",
                    data={
                        "group_id": event.conversation_info.conversation_id,
                        "user_id": event.user_info.user_id,
                        "reject_add_request": True
                    }
                )
            ]
        )

        # 3. 构建内部元数据
        kick_metadata = ActionMetadata(
            motivation=f"用户 '{event.user_info.user_nickname}' 在群聊中发送违禁词 '捣乱'。",
            source_event_id=event.event_id
        )

        # 4. 封装成内部动作请求
        internal_request = InternalActionRequest(
            action_event=kick_action_event,
            metadata=kick_metadata
        )

        # 5. 交给分发器处理，分发器会自己处理日志和发送
        action_dispatcher.dispatch(internal_request)

```
