# src/llmrequest/core/provider_handler.py (最终最终最终修正版)

import json
from abc import ABC, abstractmethod
from typing import Any
import re
import asyncio

from .models import GenerationParams
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)

# --- Constants ---
DEFAULT_STREAMING_API_ENDPOINT_GOOGLE: str = ":streamGenerateContent?alt=sse"
DEFAULT_NON_STREAMING_API_ENDPOINT_GOOGLE: str = ":generateContent"
DEFAULT_EMBEDDING_ENDPOINT_GOOGLE: str = ":embedContent"
DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI: str = "/chat/completions"
DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI: str = "/embeddings"

# --- Abstract Base Class for Handlers ---
class ApiProviderHandler(ABC):
    """Abstract base class for provider-specific API handling."""

    def __init__(self, image_placeholder_pattern: re.Pattern):
        self.image_placeholder_pattern = image_placeholder_pattern

    @abstractmethod
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
        """Prepare path, params, headers, and payload for the API request."""
        pass

    @abstractmethod
    def parse_non_streaming_response(
        self, response_json: dict[str, Any], request_type: str
    ) -> dict[str, Any]:
        """Parse a non-streaming response from the API."""
        pass

    @abstractmethod
    async def handle_streaming_response(
        self, response: Any, stream_chunk_delay: float
    ) -> dict[str, Any]:
        """Handle and parse a streaming response from the API."""
        pass
    
    def _interleave_text_and_images(
        self, prompt_text: str, processed_images: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        if not processed_images:
            return [{"type": "text", "text": prompt_text or ""}]
        if not prompt_text:
            return [{"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['b64_data']}"}} for img in processed_images]

        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in self.image_placeholder_pattern.finditer(prompt_text):
            if match.start() > last_end:
                elements.append({"type": "text", "text": prompt_text[last_end:match.start()]})
            try:
                image_index = int(match.group(2)) - 1
                if 0 <= image_index < len(processed_images):
                    img = processed_images[image_index]
                    elements.append({"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['b64_data']}"}})
            except (ValueError, IndexError):
                pass
            last_end = match.end()
        if last_end < len(prompt_text):
            elements.append({"type": "text", "text": prompt_text[last_end:]})
        return elements

# --- Google API Handler ---
class GoogleApiHandler(ApiProviderHandler):
    def prepare_request_data(self, model_name: str, request_type: str, is_streaming: bool, prompt: str | None, system_prompt: str | None, processed_images: list[dict[str, str]] | None, final_generation_config: GenerationParams, tools: list[dict[str, Any]] | None, tool_choice: str | dict | None, text_to_embed: str | None, enable_google_search: bool) -> tuple[str, dict, dict, dict]:
        headers = {"Content-Type": "application/json"}
        params = {}
        payload: dict[str, Any] = {}
        
        if request_type == "embedding":
            path = f"/models/{model_name.strip('/')}{DEFAULT_EMBEDDING_ENDPOINT_GOOGLE}"
            parts = [{"text": text_to_embed}] if text_to_embed else []
            payload = {"content": {"parts": parts}}
        else:
            path = f"/models/{model_name.strip('/')}{DEFAULT_STREAMING_API_ENDPOINT_GOOGLE if is_streaming else DEFAULT_NON_STREAMING_API_ENDPOINT_GOOGLE}"
            payload = {
                "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]],
                "generationConfig": final_generation_config.copy(),
            }
            if "responseSchema" in payload["generationConfig"]:
                if is_streaming:
                    del payload["generationConfig"]["responseSchema"]
                else:
                    payload["generationConfig"]["response_mime_type"] = "application/json"
            
            if system_prompt:
                payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
            
            interleaved = self._interleave_text_and_images(prompt or "", processed_images or [])
            user_content_parts = self._format_content(interleaved)

            payload["contents"] = [{"role": "user", "parts": user_content_parts}]

            active_tools = []
            if enable_google_search: active_tools.append({"google_search": {}})
            if request_type == "tool_call" and tools: active_tools.extend(tools)
            if active_tools: payload["tools"] = active_tools

        return path, params, headers, payload

    def parse_non_streaming_response(self, response_json: dict[str, Any], request_type: str) -> dict[str, Any]:
        parsed = {"text": None, "tool_calls": None, "embedding": None, "raw_response": response_json}
        if request_type == "embedding":
            if "embedding" in response_json and "value" in response_json["embedding"]:
                parsed["embedding"] = response_json["embedding"]["value"]
        else:
            candidate = response_json.get("candidates", [{}])[0]
            if candidate:
                content_parts = candidate.get("content", {}).get("parts", [])
                text_parts = [part["text"] for part in content_parts if "text" in part]
                if text_parts: parsed["text"] = "".join(text_parts)
                
                tool_calls = [part["functionCall"] for part in content_parts if "functionCall" in part]
                if tool_calls: parsed["tool_calls"] = tool_calls

                parsed["finish_reason"] = candidate.get("finishReason")
        return parsed

    async def handle_streaming_response(self, response: Any, stream_chunk_delay: float) -> dict[str, Any]:
        full_text = ""
        async for line_bytes in response.content:
            line = line_bytes.decode("utf-8").strip()
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:])
                    part = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0]
                    if chunk := part.get("text"):
                        print(chunk, end="", flush=True)
                        full_text += chunk
                        if stream_chunk_delay > 0: await asyncio.sleep(stream_chunk_delay)
                except (json.JSONDecodeError, IndexError): continue
        print()
        return {"full_text": full_text, "interrupted": False}

    def _format_content(self, intermediate_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._format_element(elem) for elem in intermediate_list]

    def _format_element(self, element: dict[str, Any]) -> dict[str, Any]:
        if element["type"] == "text": return {"text": element["text"]}
        elif element["type"] == "image_url":
            header, encoded_data = element["image_url"]["url"].split(",", 1)
            mime_type = header.split(";")[0].split(":")[1]
            return {"inline_data": {"mime_type": mime_type, "data": encoded_data}}
        return {}

# --- OpenAI API Handler ---
class OpenAIApiHandler(ApiProviderHandler):
    def prepare_request_data(self, model_name: str, request_type: str, is_streaming: bool, prompt: str | None, system_prompt: str | None, processed_images: list[dict[str, str]] | None, final_generation_config: GenerationParams, tools: list[dict[str, Any]] | None, tool_choice: str | dict | None, text_to_embed: str | None, enable_google_search: bool) -> tuple[str, dict, dict, dict]:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer {api_key}"}
        params = {}
        payload: dict[str, Any] = {}
        
        if request_type == "embedding":
            path = DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI
            payload = {"input": text_to_embed, "model": model_name}
            if "encoding_format" in final_generation_config: payload["encoding_format"] = final_generation_config["encoding_format"]
            if "dimensions" in final_generation_config: payload["dimensions"] = final_generation_config["dimensions"]
        else:
            path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            messages = []
            if system_prompt: messages.append({"role": "system", "content": system_prompt})
            
            content = self._interleave_text_and_images(prompt or "", processed_images or [])
            messages.append({"role": "user", "content": content})

            payload = {"model": model_name, "messages": messages}
            if is_streaming: payload["stream"] = True
            
            for key, value in final_generation_config.items():
                if key == "maxOutputTokens": payload["max_tokens"] = value
                elif key == "stopSequences": payload["stop"] = value
                elif key == "candidateCount": payload["n"] = value
                elif key == "topP": payload["top_p"] = value
                elif key in ["temperature", "presence_penalty", "frequency_penalty", "seed", "user"]: payload[key] = value
            
            if request_type == "tool_call" and tools:
                payload["tools"] = tools
                if tool_choice: payload["tool_choice"] = tool_choice
        
        return path, params, headers, payload

    def parse_non_streaming_response(self, response_json: dict[str, Any], request_type: str) -> dict[str, Any]:
        parsed = {"text": None, "tool_calls": None, "embedding": None, "raw_response": response_json}
        if request_type == "embedding":
            if "data" in response_json and response_json["data"] and "embedding" in response_json["data"][0]:
                parsed["embedding"] = response_json["data"][0]["embedding"]
        else:
            choice = response_json.get("choices", [{}])[0]
            if choice:
                message = choice.get("message", {})
                parsed["text"] = message.get("content")
                if message.get("tool_calls"): parsed["tool_calls"] = message["tool_calls"]
                parsed["finish_reason"] = choice.get("finish_reason")
        return parsed

    async def handle_streaming_response(self, response: Any, stream_chunk_delay: float) -> dict[str, Any]:
        full_text = ""
        async for line_bytes in response.content:
            line = line_bytes.decode("utf-8").strip()
            if line.startswith("data:"):
                data_json_str = line[len("data:"):].strip()
                if data_json_str == "[DONE]": break
                try:
                    data = json.loads(data_json_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    if chunk := delta.get("content"):
                        print(chunk, end="", flush=True)
                        full_text += chunk
                        if stream_chunk_delay > 0: await asyncio.sleep(stream_chunk_delay)
                except (json.JSONDecodeError, IndexError): continue
        print()
        return {"full_text": full_text, "interrupted": False}