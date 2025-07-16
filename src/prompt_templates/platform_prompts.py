PLATFORM_INPUT_XML_DESCRIPTION = """
输入 XML 块介绍：
- <external_info>: 这个块包含了你当前关注的平台下，所有聊天会话的摘要列表。
    - <conversation_list>: 这个子块会列出具体的群聊和私聊，以及它们的最新消息和未读状态。
- <internal_info>: 这个块非常重要，它记录了你上一轮的完整内心活动，是你本次思考的关键依据。
    - <action_response>: (可选) 如果你上一轮的行动有返回结果（比如获取群列表），结果会在这里面。
"""
