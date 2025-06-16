# **AIcarus认知核心重构：专注模式与指挥中心一体化方案**

文档版本: 1.0  
目标: 本文档旨在指导对AIcarusCore项目进行一次深度的架构重构。核心目标是将目前独立的“子意识”(sub\_consciousness)模块，改造并深度整合进“主意识”(core\_logic)流程中，形成一个全新的、统一的认知模型。

## **1\. 核心思想与最终效果**

### **1.1. 当前架构的局限性**

* **意识割裂**: 主意识与子意识是两个并行的、几乎没有信息交集的模块。主意识无法感知子意识的活动，子意识也无法获知主意识的思考，导致AI人格和记忆的断裂。  
* **被动响应**: AI只能被动地响应@等事件，无法主动地将注意力投向它认为重要的地方。

### **1.2. 重构后的新认知模型**

新模型将包含两个核心状态：**“指挥中心模式”** (由主意识CoreLogic扮演) 和 **“专注模式”** (由改造后的ChatSession扮演)。

* **指挥中心 (主意识)**: 负责宏观观察。它不再直接处理琐碎的聊天，而是通过一个“未读消息通知面板”来监控所有会话的动态。它的主要职责是分析全局态势，并决策**是否以及何时**进入“专注模式”来处理某个具体会话。  
* **专注模式 (前子意识)**: 负责深度交互。一旦被“指挥中心”激活，它将接管系统的思考权，专注于与单个会话进行连续、深入的对话。它拥有独立的行动能力，并能在认为适当时，主动退出并将控制权和一份“交接总结”交还给“指挥中心”。

### **1.3. 期望的最终工作流**

1. **观察**: 主意识作为指挥中心，看到“通知面板”中列出了几个有未读消息的会话。  
2. **决策**: 主意识分析通知，认为其中一个会话（如被直接@）需要立即处理，于是在其思考输出中指定要激活该会话。  
3. **交接**: 主意识将自己最新的想法作为“启动上下文”传递给专注模式，然后自身进入“空转等待”状态。  
4. **专注**: 专注模式被激活，接收上下文，并开始与用户进行多轮对话。期间，它可以自主调用工具（如网页搜索）来完成任务。  
5. **退出**: 对话结束后，专注模式认为可以结束，于是在其输出中标记退出信号。  
6. **总结**: 在退出前，专注模式调用总结模块，对本次长对话生成一份第一人称的“回忆录”。  
7. **唤醒**: 专注模式销毁自身，并立即驱动一次主意识的思考循环，将“回忆录”和自己最后的想法作为交接信息传递过去。  
8. **回归**: 主意识被唤醒，吸收交接信息，回归到步骤1的观察状态，形成一个完整的智能闭环。

## **2\. 实施方案：分阶段修改指南**

### **阶段一：数据层改造 \- 建立“已读/未读”机制**

**目标**: 为系统提供追踪消息处理状态的能力。

1. **文件**: src/database/services/event\_storage\_service.py  
   * **修改**: 在EventStorageService.save\_event方法中，当保存一个新的消息类型事件时，**强制**为该事件文档增加一个新字段 is\_processed: bool，并将其默认值设为 False。  
   * **代码示例**:  
     \# 在 event\_storage\_service.py 的 save\_event 方法内部  
     event\_data \= event.model\_dump(by\_alias=True)  
     if event\_data.get("event\_type", "").startswith("message."):  
         event\_data\["is\_processed"\] \= False \# 强制增加此字段  
     \# ...后续的数据库插入逻辑

2. **文件**: src/sub\_consciousness/chat\_session.py  
   * **修改**: 在ChatSession.\_process\_llm\_response\_and\_generate\_events（或处理LLM响应的相关方法）中，当一个或多个用户消息事件被成功用于生成回复后，需要调用EventStorageService，将这些被“消耗”掉的消息事件的is\_processed字段更新为 True。  
   * **逻辑**: 需要记录本轮ChatSession处理了哪些event\_id，然后在处理成功后批量更新它们的状态。

### **阶段二：指挥中心改造 (主意识)**

**目标**: 将主意识改造为能观察全局、做出战略决策的指挥中心。

1. **新建模块**: src/core\_logic/unread\_info\_service.py  
   * **目的**: 创建一个新服务，专门负责生成“未读消息通知面板”。  
   * **类**: UnreadInfoService  
   * **方法**: async def generate\_unread\_summary\_text() \-\> str:  
     * **逻辑**:  
       1. 调用 EventStorageService，查询所有 is\_processed \= False 的消息事件。  
       2. 按 conversation\_id 对结果进行分组。  
       3. 对每个分组，统计未读消息总数，并获取最新一条消息的内容作为预览。  
       4. 调用 ConversationStorageService 获取会话的名称等元信息。  
       5. 将所有信息格式化为一段清晰的文本，如下所示。如果没有任何未读消息，则返回空字符串或提示“所有消息均已处理”。  
   * **输出格式**:  
     你有以下未处理的会话新消息:

     \[群聊消息\]  
     \- \[群名称\]: AIcarus核心开发群 \[群ID\]: 12345678 \[最新消息\]: "张三：@AIcarus 那个新功能的PR我合并了，你看一下哈" (此会话共有 5 条未读消息)  
     \- \[群名称\]: 日常摸鱼区 \[群ID\]: 87654321 \[最新消息\]: "李四：\[图片\]" (此会话共有 18 条未读消息)

     \[私聊消息\]  
     \- \[用户\]: 王五 \[用户ID\]: 114514191 \[最新消息\]: "在吗？有个事情想请教一下。" (此会话共有 2 条未读消息)

2. **文件**: src/core\_logic/prompt\_builder.py  
   * **类**: ThoughtPromptBuilder  
   * **修改**:  
     1. 移除\_build\_recent\_contextual\_information方法中原有的逻辑。  
     2. 让这个方法转而调用新建的UnreadInfoService.generate\_unread\_summary\_text()，并将其返回值作为{recent\_contextual\_information}占位符的内容。  
     3. **修改System Prompt**: 在主意识的System Prompt中，明确告知LLM其新的角色定位和能力。**增加**以下指令：  
        * “你的角色是一个指挥中心。{recent\_contextual\_information}部分会向你展示所有未读消息的摘要。”  
        * “在你输出的JSON中，有一个active\_focus\_on\_conversation\_id字段。如果你判断某个会话需要你立即介入处理，请将该会话的ID填入此字段。其它情况下，保持其为null。”  
        * “你无法直接回复消息，只能通过激活专注模式来处理。”  
3. **文件**: src/core\_logic/consciousness\_flow.py  
   * **类**: CoreLogic  
   * **新增方法**: def trigger\_immediate\_thought\_cycle(self, handover\_summary: str \= None, last\_focus\_think: str \= None):  
     * **目的**: 提供一个事件驱动的接口，供专注模式退出时调用，以立即触发一次思考。  
     * **参数**: 接收专注模式传递过来的“交接总结”和“最后想法”。这些信息需要被整合进下一轮的ThoughtPromptBuilder中。  
   * **修改\_core\_thinking\_loop方法**:  
     * 在循环的**最开始**，增加状态检查：if self.chat\_session\_manager.is\_any\_session\_active(): time.sleep(1); continue。这会让主意识在专注模式激活时进入“空转等待”。  
     * 在调用LLM并获得思考结果后，增加对新字段的检查：  
       \# 在 \_core\_thinking\_loop 内部，获取到 llm\_response\_json 之后  
       focus\_id \= llm\_response\_json.get("active\_focus\_on\_conversation\_id")  
       if focus\_id:  
           last\_think \= llm\_response\_json.get("think")  
           self.chat\_session\_manager.activate\_session\_by\_id(focus\_id, last\_think)

### **阶段三：专注模式改造 (前子意识)**

**目标**: 将子意识改造为能被主意识激活、能自主行动和退出、并能与主意识无缝交接的专注模块。

1. **文件**: src/sub\_consciousness/chat\_prompt\_builder.py  
   * **类**: ChatPromptBuilder  
   * **修改**:  
     1. 修改 build 方法，增加一个 is\_first\_turn: bool 参数。  
     2. 当 is\_first\_turn 为 True 时：  
        * **不**包含“--- 以上消息是你已经思考过的内容 \---”这类已读/未读分割线。  
        * 在Prompt的最顶端，增加一个新的{previous\_thoughts\_block}占位符，其内容应被格式化为：“**\[指挥中心交接\]** 你刚才的想法是：‘{主意识传递过来的last\_think}’。你现在开始处理此会话。”  
   * **修改System Prompt**: 在专注模式的System Prompt中，增加以下指令：  
     * “你的任务是专注于当前对话。你可以调用action\_to\_take来执行工具。”  
     * “在你输出的JSON中，有一个end\_focused\_chat: bool字段。当你认为本次对话可以告一段落时，请将此字段设为true以退出专注模式。其它情况下，保持其为false。”  
2. **文件**: src/sub\_consciousness/chat\_session.py  
   * **类**: ChatSession  
   * **修改**:  
     1. 在\_\_init\_\_中接收主意识传递的last\_think，并在首次构建Prompt时使用。  
     2. 修改处理LLM响应的逻辑：  
        * 检查 end\_focused\_chat 字段。如果为 true，则触发退出流程：  
          1. 调用（待创建的）SummarizationService，对本次会话的完整历史记录进行总结。  
          2. 调用 self.core\_logic.trigger\_immediate\_thought\_cycle()，将总结和自己最后的think作为参数传递过去。  
          3. 调用 self.chat\_session\_manager.deactivate\_session(self.session\_id) 来销毁自己。  
        * 检查 action\_to\_take 字段。如果存在，则调用ActionHandler处理。

### **阶段四：核心服务与交接机制建设**

**目标**: 建设支撑新架构所需的关键服务，确保信息在主、子意识间顺畅流动。

1. **新建模块**: src/core\_logic/summarization\_service.py  
   * **目的**: 提供对话总结能力，解决长对话Token限制问题。  
   * **类**: SummarizationService  
   * **方法**: async def summarize\_conversation(self, history: list) \-\> str:  
     * **实现“渐进式总结”**: (作为高级目标)  
       * 如果 history 过长，则分块进行。先总结前N条，然后将“上一轮的摘要”和“下一批新消息”一起作为上下文，生成新的摘要，滚动进行。  
     * **(作为初始目标)**: 先实现简化版，直接总结整个 history，并加入Token数量检查和警告。  
     * **Prompt要求**: 调用一个专用的“总结LLM”，并**必须**为其提供完整的AI人设System Prompt，要求它从**第一人称视角**进行主观回忆式的总结。  
2. **文件**: src/action/action\_handler.py  
   * **类**: ActionHandler  
   * **修改**:  
     * 修改核心处理方法（如handle\_action），使其能接受一个可选的回调函数或返回一个Future对象。  
     * 当被ChatSession调用时，ActionHandler在完成所有异步操作（工具执行、结果总结）后，应通过这个回调/Future机制，将最终的action\_result精确地返回给发起调用的那个ChatSession实例。

### **阶段五：系统依赖注入与整合**

**目标**: 确保所有模块都能获取到它们所需依赖的实例。

1. **文件**: src/main.py  
   * **类**: CoreSystemInitializer  
   * **修改**:  
     * 在initialize\_core\_components方法中，进行依赖注入。  
     * 确保 CoreLogic 的实例被注入到 ChatSessionManager 和 ChatSession 中。  
     * 确保 ChatSessionManager 的实例被注入到 CoreLogic 中。  
     * 确保新建的 UnreadInfoService 和 SummarizationService 被正确初始化并注入到需要它们的模块（如ThoughtPromptBuilder和ChatSession）中。

此文档详尽地规划了所有必要的修改。请严格按照上述步骤和要求进行实施，以确保AIcarus认知核心的成功重构。