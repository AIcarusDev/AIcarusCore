# tests/core_logic/test_sanitizer.py

import pytest
from src.mind.sanitizer import LLMOutputSanitizer


# 使用 pytest.fixture 来创建可复用的测试设置
@pytest.fixture
def sanitizer_instance() -> LLMOutputSanitizer:
    """创建一个带有预设用户映射的 Sanitizer 实例."""
    user_map = {
        "10001": {"uid_str": "U1", "nick": "用户A", "card": "测试用户A"},
        "10002": {"uid_str": "U2", "nick": "用户B", "card": "测试用户B"},
        "99999": {"uid_str": "U0", "nick": "AIcarus", "card": "霜"},
    }
    uid_str_to_platform_id_map = {
        "U0": "99999",
        "U1": "10001",
        "U2": "10002",
    }
    return LLMOutputSanitizer(user_map, uid_str_to_platform_id_map)


# === 测试用例 ===


def test_sanitize_natural_language_fields(sanitizer_instance: LLMOutputSanitizer) -> None:
    """测试: 自然语言字段 (mood, think, intent, text content) 中的 Uid 应被替换为显示名."""
    input_json = {
        "internal_state": {
            "mood": "我对 U1 感到好奇。",
            "think": "我认为 U2 说的有道理，而 U0 应该回应。",
            "intent": "向 U1 问好",
        }
    }
    expected_output = {
        "internal_state": {
            "mood": "我对 测试用户A 感到好奇。",
            "think": "我认为 测试用户B 说的有道理，而 我 应该回应。",
            "intent": "向 测试用户A 问好",
        }
    }
    result = sanitizer_instance.sanitize(input_json)
    assert result == expected_output


def test_sanitize_at_and_text_in_send_message(sanitizer_instance: LLMOutputSanitizer) -> None:
    """测试: send_message 动作中，'at' 指令的 user_id 应被替换为平台ID，'text' 内容应被替换为显示名."""  # noqa: E501
    input_json = {
        "action": {
            "qq": {
                "send_message": {
                    "steps": [
                        {"command": "at", "params": {"user_id": "U1"}},
                        {"command": "text", "params": {"content": " U2 找你！"}},
                    ]
                }
            }
        }
    }
    expected_output = {
        "action": {
            "qq": {
                "send_message": {
                    "steps": [
                        {"command": "at", "params": {"user_id": "10001"}},
                        {"command": "text", "params": {"content": " 测试用户B 找你！"}},
                    ]
                }
            }
        }
    }
    result = sanitizer_instance.sanitize(input_json)
    assert result == expected_output


def test_sanitize_reply_command_user_id(sanitizer_instance: LLMOutputSanitizer) -> None:
    """测试: send_message 动作中，'reply' 指令的 message_id 字段不应被处理.

    但如果未来 params 中包含 user_id (虽然目前协议没有，但为了健壮性)，它也应被替换为平台ID。
    (这个测试主要是为了验证新逻辑的通用性)
    """
    input_json = {
        "action": {
            "qq": {
                "send_message": {
                    "steps": [
                        # 模拟一个未来可能出现的、包含user_id的reply指令
                        {"command": "reply", "params": {"message_id": "msg123", "user_id": "U2"}},
                        {"command": "text", "params": {"content": "同意 U2 的看法。"}},
                    ]
                }
            }
        }
    }
    expected_output = {
        "action": {
            "qq": {
                "send_message": {
                    "steps": [
                        {
                            "command": "reply",
                            "params": {"message_id": "msg123", "user_id": "10002"},
                        },
                        {"command": "text", "params": {"content": "同意 测试用户B 的看法。"}},
                    ]
                }
            }
        }
    }
    result = sanitizer_instance.sanitize(input_json)
    assert result == expected_output


def test_sanitize_other_actions_with_user_id(sanitizer_instance: LLMOutputSanitizer) -> None:
    """测试: 其他需要 user_id 的动作（如 poke_user, delete_friend）也能被正确替换."""
    input_json = {
        "action": {"qq": {"poke_user": {"target_user_id": "U1", "motivation": "提醒 U1"}}}
    }
    expected_output = {
        "action": {"qq": {"poke_user": {"target_user_id": "10001", "motivation": "提醒 测试用户A"}}}
    }
    result = sanitizer_instance.sanitize(input_json)
    assert result == expected_output
