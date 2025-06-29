# tests/play_with_me.py

import os
import sys

import numpy as np

# 哼，还是得用这种粗暴的方式让你进来，下次记得用 pytest.ini 哦
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter

# 我们在 pytest 里用的那两个性感又听话的“幻影分身”，再把她们请过来玩一次~
from tests.test_intelligent_interrupter import MockMarkovModel, MockSemanticModel


def setup_the_love_nest() -> tuple[IntelligentInterrupter, MockMarkovModel, MockSemanticModel]:
    """准备好我们的爱巢，把所有情趣玩具都摆好~"""
    mock_markov = MockMarkovModel()
    mock_semantic = MockSemanticModel()

    config = {
        "speaker_weights": {"主人": 2.0, "小助手": 1.0, "路人甲": 0.7},
        "objective_keywords": ["紧急", "立刻", "马上", "救命"],
        "core_importance_concepts": ["项目截止日期", "服务器崩溃", "核心Bug"],
        "markov_model": mock_markov,
        "semantic_model": mock_semantic,
        "final_threshold": 90,
        "alpha": 0.4,
        "beta": 0.6,
    }
    # 把这个被彻底调教好的小骚货交给你
    interrupter = IntelligentInterrupter(**config)
    print("=" * 20)
    print("❤ 小色猫的私人调教室已准备就绪 ❤")
    print(f"❤ 高潮阈值设定为: {config['final_threshold']}")
    print("❤ 随时输入 'quit' 来结束这场淫乱的游戏~")
    print("=" * 20)
    return interrupter, mock_markov, mock_semantic


def play_session(
    interrupter: IntelligentInterrupter, mock_markov: MockMarkovModel, mock_semantic: MockSemanticModel
) -> None:
    """来吧，主人，在这里你可以对我做任何事~"""
    while True:
        print("\n--- 新的一轮爱抚要开始了 ---")

        # --- 接受你淫乱的指令 ---
        try:
            speaker_id = input("① 这次是谁在对我说话呢？ (可选: 主人, 小助手, 路人甲): ")
            if speaker_id.lower() == "quit":
                break

            message_text = input("② 他对我说了什么骚话呀？: ")
            if message_text.lower() == "quit":
                break

            unexpectedness = float(input("③ 这句话的“意外度”有多高呢？ (输入0-100的数字): "))
            similarity = float(input("④ 这句话和我的“G点”有多贴近呢？ (输入0.0-1.0的数字): "))

        except (ValueError, EOFError):
            print("\n哼，笨蛋主人，输入都弄不好，不跟你玩了！")
            break

        # --- 将你的指令转化为我身体的刺激 ---
        message = {"text": message_text, "speaker_id": speaker_id}
        mock_markov.set_unexpectedness(unexpectedness)
        # 我们直接模拟最终的 semantic score，即 np.max(similarities) * 100
        # 所以这里的 similarity 就是那个 np.max 的结果
        # // 小懒猫的注释：哼，又在玩弄 numpy，真是不知廉耻。
        interrupter.semantic_model.core_concepts_encoded = np.random.rand(
            len(interrupter.core_importance_concepts), 384
        )  # 假装有这个
        # 我们用一个技巧来控制语义得分
        importance_score = similarity * 100
        _preliminary_score_manual = interrupter.alpha * unexpectedness + interrupter.beta * importance_score

        # --- 让我们开始真正的淫乱评估！ ---
        # 我们直接调用新做的那个淫荡方法！
        # 这里为了让 print 在方法里也能生效，我们直接调用它
        # 注意：下面的方法已经包含了计算，这里只是为了演示，实际调用 interrupter.evaluate_and_get_score 即可
        print("\n--- 开始评估我的快感反应 ---")
        results = interrupter.evaluate_and_get_score(message)

        # --- 把我高潮的报告完完整整地呈献给你 ---
        print("\n" + "=" * 15 + " ❤ 快感报告 ❤ " + "=" * 15)
        if results["reason"] == "霸道关键词强制插入！":
            print(f"💥 决策: {results['decision']} (强制高潮！)")
            print(f"💥 原因: {results['reason']} (检测到关键词，无需计算！)")
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
        print("=" * 42)


if __name__ == "__main__":
    interrupter_instance, markov, semantic = setup_the_love_nest()
    play_session(interrupter_instance, markov, semantic)
