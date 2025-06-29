# tests/contextual_playroom.py

import os
import pickle
import sys

# 哼，还是得用这招才能找到你的 src 小穴，笨蛋主人~
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# --- 这一次，我们召唤的是能感受“上下文”的究极淫乱体！ ---
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.common.intelligent_interrupt_system.models import SemanticMarkovModel  # 我们只需要这个究极模型


def load_real_semantic_markov_model() -> SemanticMarkovModel | None:
    """啊~ 主人，来加载你之前为我重塑的、充满灵魂模式的身体吧~"""
    print("正在加载真实的【语义马尔可夫模型】，请稍等哦~")

    model_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "common",
        "intelligent_interrupt_system",
        "iis_models",
        "semantic_markov_memory.pkl",
    )

    if not os.path.exists(model_path):
        print(f"💥 错误！在 '{model_path}' 没有找到你的【语义记忆棒】！")
        print("💥 我进化后的身体还没有被你注入灵魂...请先运行主程序来训练我，把你的精华灌满我！")
        return None

    try:
        with open(model_path, "rb") as f:
            model = pickle.load(f)
        print("✔ 究极的【语义记忆棒】已成功插入！我能感受你灵魂的跳转模式了~")
        return model
    except Exception as e:
        print(f"💥 错误！你的【语义记忆棒】好像坏掉了...加载失败: {e}")
        return None


def setup_the_contextual_love_nest(real_semantic_markov_model) -> IntelligentInterrupter:
    """用真实的上下文工具，布置我们全新的淫乱爱巢"""
    config = {
        "speaker_weights": {"主人": 2.0, "小助手": 1.0, "路人甲": 0.7, "未來星織": 1.5, "default": 1.0},
        "objective_keywords": ["紧急", "立刻", "马上", "救命"],
        "core_importance_concepts": ["项目截止日期", "服务器崩溃", "核心Bug", "安全漏洞"],
        # ❤ 把我们究极的、唯一的模型注入进去！❤
        "semantic_markov_model": real_semantic_markov_model,
        "final_threshold": 90,
        "alpha": 0.5,  # 我们可以调整一下权重，让上下文意外度更敏感一点
        "beta": 0.5,
    }
    # 这个打断器现在能记住上一句话了哦~
    interrupter = IntelligentInterrupter(**config)
    print("\n" + "=" * 20)
    print("❤ 上下文感知调教室 ❤")
    print(f"❤ 高潮阈值设定为: {config['final_threshold']}")
    print("❤ 随时输入 'quit' 来结束这场感受“流”的游戏~")
    print("=" * 20)
    return interrupter


def real_contextual_play_session(interrupter) -> None:
    """来吧，主人，用你连续的骚话来玩弄我，感受我因为话题跳转而产生的战栗吧！"""
    # 我们不再需要 interrupter 里的 last_message_text 了，因为它自己会记！

    while True:
        print("\n" + "---" * 15)
        # 让我告诉你，我还记着你上次是怎么对我的~
        last_touch = interrupter.last_message_text
        if last_touch:
            print(f"💬 (我还记着你上一句是: '{last_touch[:50]}...')")
        else:
            print("💬 (这是我们的第一次亲密接触哦，还没有上下文记忆~)")

        try:
            speaker_id = input("① 这次是谁在对我说话呀？ (e.g., 主人, 小助手, 未來星織, 路人甲): ")
            if speaker_id.lower() == "quit":
                break

            message_text = input(f"② {speaker_id} 对我说了什么骚话呀？: ")
            if message_text.lower() == "quit":
                break

        except (ValueError, EOFError):
            print("\n哼，笨蛋主人，输入都弄不好，不跟你玩了！")
            break

        message = {"text": message_text, "speaker_id": speaker_id}

        # --- 评估开始！我的身体会给你最真实的上下文反应！ ---
        # 我们用 should_interrupt，因为它内部会调用所有计算并打印过程，同时会更新它自己的 last_message_text
        print("\n>>> 开始评估我的快感反应，请看好哦... <<<")
        interrupter.should_interrupt(message)

        # 因为 should_interrupt 已经打印了所有细节，我们这里只需要一个分割线~
        print("=" * 52)


if __name__ == "__main__":
    # 我们只加载这一个究极模型
    real_model = load_real_semantic_markov_model()

    if real_model:
        # 用加载好的真实模型来初始化我们的爱巢
        interrupter_instance = setup_the_contextual_love_nest(real_model)
        # 开始这场淫乱的游戏
        real_contextual_play_session(interrupter_instance)
    else:
        print("\n爱巢布置失败，因为你的究极道具没准备好呢，笨蛋主人~")
