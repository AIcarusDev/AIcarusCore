# src/llmrequest/core/provider/openai.py
import asyncio
import json
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.llmrequest.core.models import GenerationParams
from src.llmrequest.core.provider.base import ApiProviderHandler

logger = get_logger(__name__)

# --- Constants ---
DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI: str = "/chat/completions"
DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI: str = "/embeddings"


class OpenAIApiHandler(ApiProviderHandler):
    """Handler for OpenAI and compatible API requests."""

    def prepare_request_data(
        self,
        model_name: str,
        request_type: str,
        is_streaming: bool,
        prompt: str | None,
        system_prompt: str | None,
        processed_images: list[dict[str, str]] | None,
        final_generation_config: GenerationParams,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict | None,
        text_to_embed: str | None,
        enable_google_search: bool,
    ) -> tuple[str, dict, dict, dict]:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer {api_key}"}
        params = {}
        payload: dict[str, Any] = {}

        if request_type == "embedding":
            path = DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI
            payload = {"input": text_to_embed, "model": model_name}
            if "encoding_format" in final_generation_config:
                payload["encoding_format"] = final_generation_config["encoding_format"]
            if "dimensions" in final_generation_config:
                payload["dimensions"] = final_generation_config["dimensions"]
        else:
            path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            messages = []
            
            # 1. 支持 System Prompt
            if system_prompt:
                # 如果有 schema，将其附加到 system_prompt，以强制模型输出 JSON
                if schema := final_generation_config.get("responseSchema"):
                    schema_json_string = json.dumps(schema, ensure_ascii=False)
                    system_prompt_with_schema = (
                        f"{system_prompt}\n\n"
                        f"IMPORTANT: You must respond in the following JSON format:\n"
                        f"{schema_json_string}"
                    )
                    messages.append({"role": "system", "content": system_prompt_with_schema})
                else:
                    messages.append({"role": "system", "content": system_prompt})
            
            # 2. 支持图文混排
            content = self._interleave_text_and_images(prompt or "", processed_images or [])
            messages.append({"role": "user", "content": content})

            payload = {"model": model_name, "messages": messages}
            if is_streaming:
                payload["stream"] = True

            # 3. 支持 JSON Schema (JSON Mode)
            if final_generation_config.get("responseSchema"):
                payload["response_format"] = {"type": "json_object"}

            # 转换通用参数为 OpenAI 特定参数
            for key, value in final_generation_config.items():
                if key == "maxOutputTokens":
                    payload["max_tokens"] = value
                elif key == "stopSequences":
                    payload["stop"] = value
                elif key == "candidateCount":
                    payload["n"] = value
                elif key == "topP":
                    payload["top_p"] = value
                elif key in [
                    "temperature", "presence_penalty", "frequency_penalty", "seed", "user"
                ]:
                    payload[key] = value
            
            # 4. 支持 Tools (Function Calling)
            if request_type == "tool_call" and tools:
                payload["tools"] = tools
                if tool_choice:
                    payload["tool_choice"] = tool_choice

        return path, params, headers, payload

    def parse_non_streaming_response(
        self, response_json: dict[str, Any], request_type: str
    ) -> dict[str, Any]:
        parsed = {
            "text": None,
            "tool_calls": None,
            "embedding": None,
            "raw_response": response_json,
        }
        if request_type == "embedding":
            # 5. 支持解析 Embedding 响应
            if (
                "data" in response_json
                and response_json["data"]
                and "embedding" in response_json["data"][0]
            ):
                parsed["embedding"] = response_json["data"][0]["embedding"]
        else:
            choice = response_json.get("choices", [{}])[0]
            if choice:
                message = choice.get("message", {})
                parsed["text"] = message.get("content")
                if message.get("tool_calls"):
                    parsed["tool_calls"] = message["tool_calls"]
                parsed["finish_reason"] = choice.get("finish_reason")
        return parsed

    async def handle_streaming_response(
        self, response: Any, stream_chunk_delay: float
    ) -> dict[str, Any]:
        full_text = ""
        tool_calls_chunks = []
        
        async for line_bytes in response.content:
            line = line_bytes.decode("utf-8").strip()
            if line.startswith("data:"):
                data_json_str = line[len("data:") :].strip()
                if data_json_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_json_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    
                    # 处理文本块
                    if chunk := delta.get("content"):
                        print(chunk, end="", flush=True)
                        full_text += chunk
                        if stream_chunk_delay > 0:
                            await asyncio.sleep(stream_chunk_delay)
                    
                    # 6. 支持处理流式 Tool Calls
                    if tool_chunks := delta.get("tool_calls"):
                        for tool_chunk in tool_chunks:
                            tool_calls_chunks.append(tool_chunk)

                except (json.JSONDecodeError, IndexError):
                    continue
        print()
        
        final_tool_calls = self._reconstruct_tool_calls(tool_calls_chunks)

        return {"full_text": full_text, "tool_calls": final_tool_calls, "interrupted": False}

    def _reconstruct_tool_calls(self, chunks: list[dict]) -> list[dict] | None:
        """从流式块中重构完整的 tool_calls。"""
        if not chunks:
            return None
        
        tools_by_index = {}
        for chunk in chunks:
            index = chunk.get("index")
            if index is None:
                continue

            if index not in tools_by_index:
                tools_by_index[index] = chunk.copy()
            else:
                if 'id' in chunk:
                    tools_by_index[index]['id'] = chunk['id']
                if 'type' in chunk:
                    tools_by_index[index]['type'] = chunk['type']
                
                if 'function' in chunk:
                    if 'function' not in tools_by_index[index]:
                        tools_by_index[index]['function'] = {}
                    
                    if name_part := chunk['function'].get('name'):
                        tools_by_index[index]['function']['name'] = tools_by_index[index]['function'].get('name', '') + name_part
                    if args_part := chunk['function'].get('arguments'):
                        tools_by_index[index]['function']['arguments'] = tools_by_index[index]['function'].get('arguments', '') + args_part

        return list(tools_by_index.values())