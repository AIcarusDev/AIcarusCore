# 文件路径: src/mind/thought_generator.py
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response

if TYPE_CHECKING:
    from src.services.action.action_handler import ActionHandler
    from src.services.database.services.media_cache_service import MediaCacheService
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

    def __init__(
        self,
        llm_client: "ProcessorClient",
        action_handler: "ActionHandler",
        media_cache_service: "MediaCacheService",
    ) -> None:
        self.llm_client = llm_client
        self.action_handler = action_handler
        self.media_cache_service = media_cache_service
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
        all_image_data = {}

        if image_references:
            # 1. 批量从本地缓存获取图片
            hashes_to_find = [img["hash"] for img in image_references if "hash" in img]
            logger.debug(f"准备从本地缓存批量获取 {len(hashes_to_find)} 张图片...")
            cached_images = await self.media_cache_service.get_images_b64_by_hashes(hashes_to_find)
            all_image_data.update(cached_images)
            logger.debug(f"本地缓存命中 {len(cached_images)} 张图片。")

            # 2. 找出缓存中没有的图片
            missing_hashes_map = {
                img["hash"]: img["platform_id"]
                for img in image_references
                if "hash" in img and img["hash"] not in cached_images
            }

            # 3. 为缺失的图片发起反向请求
            if missing_hashes_map:
                logger.info(f"发现 {len(missing_hashes_map)} 张图片不在本地缓存，将向Adapter请求。")
                fetched_images_list = await self.action_handler.request_media_from_adapters(
                    missing_hashes_map
                )

                # 将获取到的图片存入缓存并合并到结果中
                if fetched_images_list:
                    await self.media_cache_service.save_images_b64(fetched_images_list)
                    fetched_images_map = {img["hash"]: img for img in fetched_images_list}
                    all_image_data.update(fetched_images_map)

        # 4. 构建最终的 user_prompt_parts
        user_prompt_parts = []
        if not image_references:
            user_prompt_parts.append({"text": user_prompt})
        else:
            # 使用 `all_image_data` 这个最终的数据源来构建
            placeholder_pattern_str = "|".join(
                re.escape(img["placeholder"]) for img in image_references
            )
            placeholder_pattern = re.compile(f"({placeholder_pattern_str})")

            # 创建占位符到哈希的映射
            placeholder_to_hash_map = {
                img["placeholder"]: img.get("hash") for img in image_references
            }

            text_fragments = placeholder_pattern.split(user_prompt)

            for fragment in text_fragments:
                if not fragment:
                    continue

                image_hash = placeholder_to_hash_map.get(fragment)
                if image_hash and image_hash in all_image_data:
                    # 如果这个片段是占位符，并且我们成功获取了它的数据
                    image_data = all_image_data[image_hash]
                    user_prompt_parts.append({
                        "inline_data": {
                            "mime_type": image_data["mime_type"],
                            "data": image_data["base64"],
                        }
                    })
                elif fragment in placeholder_to_hash_map:
                    # 占位符存在，但无法获取图片数据
                    logger.warning(
                        f"无法为占位符 {fragment} (哈希: {image_hash}) 获取图片数据，"
                        f"将在prompt中忽略。"
                    )
                    user_prompt_parts.append({"text": "[图片加载失败]"})
                else:
                    # 否则，它是普通的文本片段
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
