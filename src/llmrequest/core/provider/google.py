# src/llmrequest/core/provider/google.py
import asyncio
import json
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.llmrequest.core.models import GenerationParams
from src.llmrequest.core.provider.base import ApiProviderHandler

logger = get_logger(__name__)

# --- Constants ---
DEFAULT_STREAMING_API_ENDPOINT_GOOGLE: str = ":streamGenerateContent?alt=sse"
DEFAULT_NON_STREAMING_API_ENDPOINT_GOOGLE: str = ":generateContent"
DEFAULT_EMBEDDING_ENDPOINT_GOOGLE: str = ":embedContent"


class GoogleApiHandler(ApiProviderHandler):
    """Handler for Google API requests."""

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
        """Prepare the request data for Google API calls.

        Parameters
        ----------
        model_name : str
            The name of the model to use.
        request_type : str
            The type of request ("embedding", "tool_call", etc.).
        is_streaming : bool
            Whether the response should be streamed.
        prompt : str | None
            The user prompt for the model.
        system_prompt : str | None
            The system prompt for the model.
        processed_images : list[dict[str, str]] | None
            List of processed images to include in the request.
        final_generation_config : GenerationParams
            Generation configuration parameters.
        tools : list[dict[str, Any]] | None
            List of tools for function calling.
        tool_choice : str | dict | None
            Tool choice specification.
        text_to_embed : str | None
            Text to embed for embedding requests.
        enable_google_search : bool
            Whether to enable Google search.

        Returns:
        -------
        tuple[str, dict, dict, dict]
            The API endpoint path, query parameters, headers, and payload dictionary.
        """
        headers = {"Content-Type": "application/json"}
        params = {}
        payload: dict[str, Any] = {}

        if request_type == "embedding":
            path = f"/models/{model_name.strip('/')}{DEFAULT_EMBEDDING_ENDPOINT_GOOGLE}"
            parts = [{"text": text_to_embed}] if text_to_embed else []
            payload = {"content": {"parts": parts}}
        else:
            endpoint = (
                DEFAULT_STREAMING_API_ENDPOINT_GOOGLE
                if is_streaming
                else DEFAULT_NON_STREAMING_API_ENDPOINT_GOOGLE
            )
            path = f"/models/{model_name.strip('/')}{endpoint}"
            payload = {
                "safetySettings": [
                    {"category": c, "threshold": "BLOCK_NONE"}
                    for c in [
                        "HARM_CATEGORY_HARASSMENT",
                        "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "HARM_CATEGORY_DANGEROUS_CONTENT",
                    ]
                ],
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
            if enable_google_search:
                active_tools.append({"google_search": {}})
            if request_type == "tool_call" and tools:
                active_tools.extend(tools)
            if active_tools:
                payload["tools"] = active_tools

        return path, params, headers, payload

    def parse_non_streaming_response(
        self, response_json: dict[str, Any], request_type: str
    ) -> dict[str, Any]:
        """Parse the non-streaming response from the Google API.

        Parameters
        ----------
        response_json : dict[str, Any]
            The JSON response from the API.
        request_type : str
            The type of request ("embedding", "tool_call", etc.).

        Returns:
        -------
        dict[str, Any]
            A dictionary containing parsed text, tool calls, embedding, and the raw response.
        """
        parsed = {
            "text": None,
            "tool_calls": None,
            "embedding": None,
            "raw_response": response_json,
        }
        if request_type == "embedding":
            if "embedding" in response_json and "value" in response_json["embedding"]:
                parsed["embedding"] = response_json["embedding"]["value"]
        else:
            candidate = response_json.get("candidates", [{}])[0]
            if candidate:
                content_parts = candidate.get("content", {}).get("parts", [])
                text_parts = [part["text"] for part in content_parts if "text" in part]
                if text_parts:
                    parsed["text"] = "".join(text_parts)

                tool_calls = [
                    part["functionCall"] for part in content_parts if "functionCall" in part
                ]
                if tool_calls:
                    parsed["tool_calls"] = tool_calls

                parsed["finish_reason"] = candidate.get("finishReason")
        return parsed

    async def handle_streaming_response(
        self, response: Any, stream_chunk_delay: float
    ) -> dict[str, Any]:
        """Handle and parse a streaming response from the Google API.

        Parameters
        ----------
        response : Any
            The streaming response object from the API.
        stream_chunk_delay : float
            Delay in seconds between processing each chunk of streamed data.

        Returns:
        -------
        dict[str, Any]
            A dictionary containing the full concatenated text and an interruption flag.
        """
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
                        if stream_chunk_delay > 0:
                            await asyncio.sleep(stream_chunk_delay)
                except (json.JSONDecodeError, IndexError):
                    continue
        print()
        return {"full_text": full_text, "interrupted": False}

    def _format_content(self, intermediate_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._format_element(elem) for elem in intermediate_list]

    def _format_element(self, element: dict[str, Any]) -> dict[str, Any]:
        if element["type"] == "text":
            return {"text": element["text"]}
        elif element["type"] == "image_url":
            header, encoded_data = element["image_url"]["url"].split(",", 1)
            mime_type = header.split(";")[0].split(":")[1]
            return {"inline_data": {"mime_type": mime_type, "data": encoded_data}}
        return {}
