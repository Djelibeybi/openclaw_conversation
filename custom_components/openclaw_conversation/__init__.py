"""The OpenClaw Conversation integration."""

from __future__ import annotations

import logging

import httpx

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_OPENCLAW_URL,
    CONF_VERIFY_SSL,
    DEFAULT_AGENT_ID,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CONVERSATION, Platform.AI_TASK]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type OpenClawConfigEntry = ConfigEntry[None]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up OpenClaw Conversation."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenClawConfigEntry) -> bool:
    """Set up OpenClaw Conversation from a config entry."""
    # Validate connectivity to OpenClaw
    base_url = entry.data[CONF_OPENCLAW_URL].rstrip("/")
    api_key = entry.data[CONF_API_KEY]
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

    # Use Home Assistant's httpx client helper to avoid blocking I/O during SSL setup
    client = get_async_client(hass, verify_ssl=verify_ssl)

    try:
        # Test connectivity with a simple request
        response = await client.get(
            f"{base_url}/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "x-openclaw-agent-id": DEFAULT_AGENT_ID,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as err:
        if err.response.status_code == 401:
            _LOGGER.error("Invalid API key for OpenClaw")
            return False
        raise ConfigEntryNotReady(f"Failed to connect to OpenClaw: {err}") from err
    except httpx.RequestError as err:
        raise ConfigEntryNotReady(f"Failed to connect to OpenClaw: {err}") from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload OpenClaw."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
