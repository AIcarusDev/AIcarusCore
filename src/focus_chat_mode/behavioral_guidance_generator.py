# src/focus_chat_mode/behavioral_guidance_generator.py

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .chat_session import ChatSession

class BehavioralGuidanceGenerator:
    """
    哼，一个专门生产烦人提示的家伙。
    它会偷看会话里的计数器，然后决定怎么念叨你。
    """

    def __init__(self, session: "ChatSession"):
        self.session = session
        # 你可以把这些阈值写到配置文件里，但我懒，就先放这了
        self.NO_ACTION_THRESHOLD = 3
        self.BOT_SPAM_THRESHOLD = 3

    def generate_guidance(self) -> str:
        """
        根据会话的当前状态，生成对应的行为指导提示。
        """
        # 从会话里把那两个烦人的计数器拿过来
        no_act_count = self.session.no_action_count
        bot_spam_count = self.session.consecutive_bot_messages_count

        # 检查是不是私聊，这决定了我的语气
        is_private = self.session.conversation_type == "private"

        # 终极自闭警告：又话痨又自闭
        if bot_spam_count >= self.BOT_SPAM_THRESHOLD and no_act_count >= self.NO_ACTION_THRESHOLD:
            if is_private:
                return (
                    f"你之前已经连续发送了 {bot_spam_count} 条消息，且对方没有回应你，"
                    f"并且在这之后，你又决定连续不发言/没有互动 {no_act_count} 次了，"
                    "观察一下目前与对方的话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于与对方的聊天了。"
                )
            else:
                return (
                    f"你之前已经连续发送了 {bot_spam_count} 条消息，且无人回应你，"
                    f"并且在这之后，你又决定连续不发言/没有互动 {no_act_count} 次了，"
                    "观察一下目前群内话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于该群聊的消息了。"
                )

        # 话痨警告：说太多没人理
        elif bot_spam_count >= self.BOT_SPAM_THRESHOLD:
            if is_private:
                return (
                    f"你已经连续发送了 {bot_spam_count} 条消息，且对方没有回应你，"
                    "有可能现在再发消息已经不合适了，注意观察聊天记录，如果你还打算继续发送消息，请确保这是合适且不会打扰的。"
                )
            else:
                return (
                    f"你已经连续发送了 {bot_spam_count} 条消息，且没有任何人回应你，"
                    "有可能现在再发消息已经不合适了，注意观察聊天记录，如果你还打算继续发送消息，请确保这是合适且不会打扰的。"
                )

        # 沉默警告：半天不说话
        elif no_act_count >= self.NO_ACTION_THRESHOLD:
            if is_private:
                return (
                    f"你已经决定连续不发言/没有互动 {no_act_count} 次了，"
                    "观察一下目前与对方的话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于与对方的聊天了。"
                )
            else:
                return (
                    f"你已经决定连续不发言/没有互动 {no_act_count} 次了，"
                    "观察一下目前群内话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于群聊的消息了。"
                )

        # 没事了，滚吧
        return ""