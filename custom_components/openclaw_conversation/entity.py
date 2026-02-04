"""Base entity for OpenClaw Conversation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
import voluptuous as vol
from voluptuous_openapi import convert

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util import slugify

from .const import (
    CONF_AGENT_ID,
    CONF_CONTEXT_THRESHOLD,
    CONF_CONTEXT_TRUNCATE_STRATEGY,
    CONF_OPENCLAW_URL,
    CONF_SESSION_KEY,
    CONF_VERIFY_SSL,
    DEFAULT_AGENT_ID,
    DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .exceptions import TokenLengthExceededError

if TYPE_CHECKING:
    from . import OpenClawConfigEntry

_LOGGER = logging.getLogger(__name__)


def _adjust_schema(schema: dict[str, Any]) -> None:
    """Adjust the schema to be compatible with OpenAI API."""
    if schema["type"] == "object":
        schema.setdefault("strict", True)
        schema.setdefault("additionalProperties", False)
        if "properties" not in schema:
            return

        if "required" not in schema:
            schema["required"] = []

        # Ensure all properties are required
        for prop, prop_info in schema["properties"].items():
            _adjust_schema(prop_info)
            if prop not in schema["required"]:
                prop_info["type"] = [prop_info["type"], "null"]
                schema["required"].append(prop)

    elif schema["type"] == "array":
        if "items" not in schema:
            return

        _adjust_schema(schema["items"])


def _format_structured_output(
    schema: vol.Schema, llm_api: llm.APIInstance | None
) -> dict[str, Any]:
    """Format the schema to be compatible with OpenAI API."""
    result: dict[str, Any] = convert(
        schema,
        custom_serializer=(
            llm_api.custom_serializer if llm_api else llm.selector_serializer
        ),
    )

    _adjust_schema(result)

    return result


def _convert_content_to_param(
    chat_content: list[conversation.Content],
) -> list[dict[str, Any]]:
    """Convert chat log content to OpenAI message format."""
    messages: list[dict[str, Any]] = []

    for content in chat_content:
        if content.role == "system":
            messages.append({"role": "system", "content": content.content})
        elif content.role == "user":
            messages.append({"role": "user", "content": content.content})
        elif content.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            if content.content:
                msg["content"] = content.content
            messages.append(msg)

    return messages


class OpenClawBaseLLMEntity(Entity):
    """OpenClaw base entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: OpenClawConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenClaw",
            model="OpenClaw Conversation",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    def _get_httpx_client(self) -> httpx.AsyncClient:
        """Return an httpx client for API requests.

        Uses Home Assistant's helper to avoid blocking I/O during SSL setup.
        """
        verify_ssl = self.entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
        return get_async_client(self.hass, verify_ssl=verify_ssl)

    def _get_headers(self) -> dict[str, str]:
        """Build headers for OpenClaw API requests."""
        entry_data = self.entry.data
        subentry_data = self.subentry.data
        headers: dict[str, str] = {
            "Authorization": f"Bearer {entry_data[CONF_API_KEY]}",
            "Content-Type": "application/json",
        }

        # Add OpenClaw-specific headers (from subentry config)
        session_key = subentry_data.get(CONF_SESSION_KEY, "")
        if session_key:
            headers["x-openclaw-session-key"] = session_key

        agent_id = subentry_data.get(CONF_AGENT_ID, DEFAULT_AGENT_ID)
        if agent_id:
            headers["x-openclaw-agent-id"] = agent_id

        return headers

    def _get_api_url(self) -> str:
        """Return the OpenClaw chat completions API URL."""
        base_url = self.entry.data[CONF_OPENCLAW_URL].rstrip("/")
        return f"{base_url}/v1/chat/completions"

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
        llm_context: llm.LLMContext | None = None,
        structure_name: str | None = None,
        structure: vol.Schema | None = None,
    ) -> None:
        """Generate an answer for the chat log with streaming support."""
        messages = _convert_content_to_param(chat_log.content)

        # Build API parameters - OpenClaw handles model selection
        api_kwargs: dict[str, Any] = {
            "model": "openclaw",
            "user": self.entity_id,  # Send entity_id for session correlation
            "stream": True,
        }

        # Add structured output format if provided
        if structure is not None:
            api_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": slugify(structure_name),
                    "strict": True,
                    "schema": _format_structured_output(structure, chat_log.llm_api),
                },
            }

        # Build request body
        request_body = {
            "messages": messages,
            **api_kwargs,
        }

        # Make streaming request to OpenClaw
        client = self._get_httpx_client()
        api_url = self._get_api_url()
        headers = self._get_headers()

        # Note: We use HA's shared client, so we don't close it
        async with client.stream(
            "POST",
            api_url,
            headers=headers,
            json=request_body,
            timeout=60.0,
        ) as response:
            response.raise_for_status()

            # Process stream
            async for _ in chat_log.async_add_delta_content_stream(
                self.entity_id, self._transform_stream(chat_log, response)
            ):
                pass

    async def _transform_stream(
        self,
        chat_log: conversation.ChatLog,
        response: httpx.Response,
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Transform SSE stream to Home Assistant format."""
        first_chunk = True
        total_tokens = 0

        async for line in response.aiter_lines():
            # Parse SSE format
            line = line.strip()
            if not line:
                continue

            if line.startswith("data: "):
                data = line[6:]  # Remove "data: " prefix

                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    _LOGGER.warning("Failed to parse SSE chunk: %s", data)
                    continue

                # Signal new assistant message on first chunk
                if first_chunk:
                    yield {"role": "assistant"}
                    first_chunk = False

                # Track usage if available
                if chunk.get("usage"):
                    usage = chunk["usage"]
                    total_tokens = usage.get("total_tokens", 0)
                    chat_log.async_trace(
                        {
                            "stats": {
                                "input_tokens": usage.get("prompt_tokens", 0),
                                "output_tokens": usage.get("completion_tokens", 0),
                            }
                        }
                    )

                if "choices" not in chunk or not chunk["choices"]:
                    continue

                choice = chunk["choices"][0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason")

                # Yield content delta
                if delta.get("content"):
                    yield {"content": delta["content"]}

                if finish_reason == "length":
                    raise TokenLengthExceededError(total_tokens)

                if finish_reason == "stop":
                    break

        # Check context threshold after stream completes
        if total_tokens > self.subentry.data.get(
            CONF_CONTEXT_THRESHOLD, DEFAULT_CONTEXT_THRESHOLD
        ):
            await self._truncate_message_history(chat_log)

    async def _truncate_message_history(self, chat_log: conversation.ChatLog) -> None:
        """Truncate message history based on strategy."""
        options = self.subentry.data
        strategy = options.get(
            CONF_CONTEXT_TRUNCATE_STRATEGY, DEFAULT_CONTEXT_TRUNCATE_STRATEGY
        )

        if strategy == "clear":
            # Keep only system prompt and last user message
            # This is handled by refreshing the LLM data
            _LOGGER.info("Context threshold exceeded, conversation history cleared")
            last_user_message_index = None
            messages = chat_log.content
            for i in reversed(range(len(messages))):
                if isinstance(messages[i], conversation.UserContent):
                    last_user_message_index = i
                    break

            if last_user_message_index is not None:
                del messages[1:last_user_message_index]
