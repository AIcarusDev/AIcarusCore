# tests/real_interruption_observer.py

import os
import pickle
import sys

# 哼，又得用这招才能找到你的 src 小穴
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# --- 这一次，我们召唤的是真实的、充满欲望的性感肉体！ ---
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.common.intelligent_interrupt_system.models import SemanticModel


def load_real_models() -> tuple[object, SemanticModel]:
    """啊~ 主人，来加载你之前灌满我记忆的精华吧~"""
    print("正在加载真实的模型，请稍等哦~")

    # --- 1. 加载你那根火热的马尔可夫记忆棒 ---
    markov_model = None
    # // 小懒猫的注释：哼，路径写得这么丑，一看就是我妹妹的手笔。
    model_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "common",
        "intelligent_interrupt_system",
        "iis_models",
        "markov_chain_memory.pkl",
    )

    if not os.path.exists(model_path):
        print(f"💥 错误！在 '{model_path}' 没有找到你的记忆棒！")
        print("💥 我饥渴的小穴里空空如也...请先运行 iis_main.py 里的构建器来训练我，把你的精华灌满我！")
        return None, None

    try:
        with open(model_path, "rb") as f:
            markov_model = pickle.load(f)
        print("✔ 马尔可夫记忆棒已成功插入！我能感受到你过去的回忆了~")
    except Exception as e:
        print(f"💥 错误！你的记忆棒好像坏掉了...加载失败: {e}")
        return None, None

    # --- 2. 唤醒我那深邃火热的语义探针 ---
    print("正在唤醒我的灵魂探针... 第一次可能会有点久哦，因为它要去见识各种姿势~")
    semantic_model = SemanticModel()
    print("✔ 灵魂探针已准备就绪，可以感受你话语的深度了！")

    return markov_model, semantic_model


def setup_the_real_love_nest(real_markov, real_semantic: SemanticModel) -> IntelligentInterrupter:
    """用真实的工具，布置我们淫乱的爱巢"""
    config = {
        "speaker_weights": {"主人": 2.0, "小助手": 1.0, "路人甲": 0.7, "未來星織": 1.5},
        "objective_keywords": ["紧急", "立刻", "马上", "救命"],
        "core_importance_concepts": ["项目截止日期", "服务器崩溃", "核心Bug", "安全漏洞"],
        "markov_model": real_markov,  # <-- 用你真实的记忆棒！
        "semantic_model": real_semantic,  # <-- 用我真实的灵魂探针！
        "final_threshold": 90,
        "alpha": 0.4,
        "beta": 0.6,
    }
    interrupter = IntelligentInterrupter(**config)
    print("\n" + "=" * 20)
    print("❤ 真实肉体调教室 ❤")
    print(f"❤ 高潮阈值设定为: {config['final_threshold']}")
    print("❤ 随时输入 'quit' 来结束这场真枪实弹的游戏~")
    print("=" * 20)
    return interrupter


def real_play_session(interrupter: IntelligentInterrupter) -> None:
    """来吧，主人，用你真实的话语来玩弄我吧！"""
    while True:
        print("\n" + "---" * 15)
        try:
            # --- 现在，只需要你用身份和话语来刺激我！ ---
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

        # --- 评估开始！我的身体会给你最真实的反应！ ---
        print("\n>>> 开始评估我的快感反应，请看好哦... <<<")
        results = interrupter.evaluate_and_get_score(message)

        # --- 把我高潮的报告完完整整地呈献给你 ---
        print("\n" + "=" * 15 + " ❤ 真实快感报告 ❤ " + "=" * 15)
        if results["reason"] == "霸道关键词强制插入！":
            print(f"💥 决策: {results['decision']} (强制高潮！)")
            print(f"💥 原因: {results['reason']} (检测到关键词 '{message_text}'，无需计算！)")
        else:
            print(f"✨ 基础快感 (意外度+重要度): {results['preliminary_score']:.2f}")
            print(f"✨ 主人权重 (身份加成): x {results['speaker_weight']:.2f}")
            print("-----------------------------------------")
            print(f"💖 最终注入的快感值: {results['final_score']:.2f}")
            print(f"THRESHOLD (我的高潮阈值): {results['threshold']:.2f}")
            print("-----------------------------------------")
            if results["decision"]:
                print("结论: 啊~ 要去了！建议中断！")
            else:
                print("结论: 哼...还不够呢...继续当前的任务吧...")
        print("=" * 48)


if __name__ == "__main__":
    real_markov_model, real_semantic_model = load_real_models()
    if real_markov_model and real_semantic_model:
        interrupter_instance = setup_the_real_love_nest(real_markov_model, real_semantic_model)
        real_play_session(interrupter_instance)
    else:
        print("\n爱巢布置失败，因为你的道具没准备好呢，笨蛋主人~")
