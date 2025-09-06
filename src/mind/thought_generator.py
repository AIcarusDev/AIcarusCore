# 文件路径: src/mind/thought_generator.py
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response

if TYPE_CHECKING:
    from src.services.llmrequest.llm_processor import Client as ProcessorClient

logger = get_logger(__name__)


class ThoughtGenerator:
    """负责生成思考结果的核心类.

    这个类会调用 LLM API，传入系统提示和用户提示，
    并解析返回的 JSON 响应。它还可以处理多模态输入（如图像），
    并支持自定义的响应模式定义.

    Attributes:
        llm_client (ProcessorClient): 用于与 LLM API 交互的客户端实例.
    """

    def __init__(self, llm_client: "ProcessorClient") -> None:
        self.llm_client = llm_client
        logger.info("ThoughtGenerator 已初始化。")

    async def generate_thought(
        self,
        system_prompt: str,
        user_prompt: str,
        image_references: list[dict],  # 接收收集到的图片数据
        response_schema: dict[str, Any] | None = None,
        focus_path: str | None = None,
    ) -> dict[str, Any] | None:
        """生成思考结果的核心方法.

        这个方法会调用 LLM API，传入系统提示和用户提示，
        并解析返回的 JSON 响应.

        Args:
            system_prompt (str): 系统提示，用于指导 LLM 的行为和思考方式.
            user_prompt (str): 用户提示，包含用户的输入或问题.
            image_references (list[dict]): 可选的图像引用列表，用于多模态处理.
            response_schema (dict[str, Any] | None): 可选的响应模式定义，用于指导 LLM 的输出格式.
            focus_path (str | None): 可选的注意力焦点路径，用于指定当前思考的上下文.

        Returns:
            dict[str, Any] | None: 解析后的思考结果 JSON 对象，如果调用失败或解析错误则返回 None.
        """
        # 在这里打印所有即将发送给LLM的信息
        logger.debug("=" * 40 + " LLM DEBUG PROMPT " + "=" * 40)
        logger.debug(f"当前注意力焦点 (Focus Path): {focus_path or 'core'}")

        # 2. 创建一个专门用于日志打印的 prompt 版本
        prompt_for_logging = re.sub(
            r"<system_rule>.*?</system_rule>",
            "<system_rule>... [内容已省略] ...</system_rule>",
            system_prompt,
            flags=re.DOTALL,  # re.DOTALL 标志让 '.' 可以匹配包括换行符在内的任意字符
        )
        # 3. 在日志中使用这个净化后的版本
        logger.debug(f"--- [SYSTEM PROMPT] ---\n{prompt_for_logging}")
        # 注意：user_prompt 包含占位符，将在下面处理
        logger.debug(f"--- [USER PROMPT (with placeholders)] ---\n{user_prompt}")

        if response_schema:
            try:
                # 使用 json.dumps
                schema_str = json.dumps(response_schema, ensure_ascii=False)
                logger.debug(f"--- [JSON SCHEMA] ---\n{schema_str}")
            except Exception as e:
                logger.error(f"无法序列化 JSON Schema: {e}")
                logger.debug(f"--- [JSON SCHEMA (Raw)] ---\n{response_schema}")
        else:
            logger.debug("--- [JSON SCHEMA] --- \nNone")

        if image_references:
            logger.debug(f"--- [IMAGE REFERENCES ({len(image_references)})] ---")
            for img_ref in image_references:
                logger.debug(
                    f"  - ID: {img_ref['id']}, "
                    f"Placeholder: {img_ref['placeholder']}, MIME: {img_ref['mime_type']}"
                )

        logger.debug("=" * 41 + " END OF DEBUG " + "=" * 41)

        # --- 2. 构建图文混合的 parts 列表 ---
        user_prompt_parts = []
        if not image_references:
            # 如果没有图片，行为和以前一样，是纯文本
            user_prompt_parts.append({"text": user_prompt})
        else:
            # 如果有图片，执行精巧的“分裂-插入”逻辑
            # 1. 构建一个正则表达式，用于查找所有占位符
            # e.g., r"(\[图片_1\]|\[动画表情_2\]|...)"
            placeholder_pattern_str = "|".join(
                re.escape(img["placeholder"]) for img in image_references
            )
            placeholder_pattern = re.compile(f"({placeholder_pattern_str})")

            # 2. 创建一个从占位符文本到图像数据的映射，方便快速查找
            image_map = {img["placeholder"]: img for img in image_references}

            # 3. 分割文本
            text_fragments = placeholder_pattern.split(user_prompt)

            # 4. 重新组装成 parts 列表
            for fragment in text_fragments:
                if not fragment:
                    continue

                if fragment in image_map:
                    # 如果这个片段是我们的占位符，就插入图片数据
                    image_data = image_map[fragment]
                    user_prompt_parts.append(
                        {
                            "inline_data": {
                                "mime_type": image_data["mime_type"],
                                "data": image_data["data"],
                            }
                        }
                    )
                else:
                    # 否则，它就是普通的文本片段
                    user_prompt_parts.append({"text": fragment})

        # --- 3. 调用LLM客户端 ---
        try:
            # 将 user_prompt_parts 传递给 LLM 客户端
            response_data = await self.llm_client.make_llm_request(
                prompt_parts=user_prompt_parts,  # 使用 parts 列表
                system_prompt=system_prompt,
                is_stream=False,
                is_multimodal=bool(image_references),
                use_google_search=False,
                response_schema=response_schema,
            )

            # --- 4. 后续的响应处理逻辑  ---
            if response_data.get("error"):
                logger.error(f"LLM调用失败: {response_data.get('message', '未知错误')}")
                return None

            raw_text = response_data.get("text", "")
            if not raw_text:
                logger.error("LLM响应中缺少文本内容。")
                return None

            parsed_json = parse_llm_json_response(raw_text)

            if parsed_json is None:
                logger.error("解析LLM的JSON响应失败，它返回了None。")
                return None

            # 验证JSON结构是否符合预期
            if (
                not isinstance(parsed_json, dict)
                or "internal_state" not in parsed_json
                or not isinstance(parsed_json.get("internal_state"), dict)
            ):
                logger.error(
                    f"LLM响应的JSON结构不符合预期。'internal_state' "
                    f"键必须存在且其值必须是一个对象/字典。"
                    f"收到的内容: {str(parsed_json)[:200]}..."
                )
                return None

            if response_data.get("usage"):
                parsed_json["_llm_usage_info"] = response_data.get("usage")

            # 在这里，我们给生成的思考结果也加上唯一的ID，方便追踪
            if "action" in parsed_json and isinstance(parsed_json["action"], dict):
                # 如果有动作，就用 action_id 作为整个思考的ID
                parsed_json["thought_id"] = parsed_json.get("action", {}).get(
                    "action_id", str(uuid.uuid4())
                )
            else:
                parsed_json["thought_id"] = str(uuid.uuid4())

            logger.info(f"LLM API 的回应已成功解析为JSON。Thought ID: {parsed_json['thought_id']}")
            return parsed_json

        except Exception as e:
            logger.error(f"调用LLM或解析响应时发生意外错误: {e}", exc_info=True)
            return None
