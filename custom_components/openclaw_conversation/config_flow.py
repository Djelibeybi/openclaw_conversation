"""Config flow for OpenClaw Conversation integration."""

from __future__ import annotations

import logging
import types
from typing import Any

import httpx
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AGENT_ID,
    CONF_CONTEXT_THRESHOLD,
    CONF_CONTEXT_TRUNCATE_STRATEGY,
    CONF_HA_MCP_URL,
    CONF_OPENCLAW_URL,
    CONF_PROMPT,
    CONF_SESSION_KEY,
    CONF_VERIFY_SSL,
    CONTEXT_TRUNCATE_STRATEGIES,
    DEFAULT_AGENT_ID,
    DEFAULT_AI_TASK_NAME,
    DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
    DEFAULT_CONVERSATION_NAME,
    DEFAULT_NAME,
    DEFAULT_PROMPT,
    DEFAULT_SESSION_KEY,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_OPENCLAW_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): BooleanSelector(),
    }
)

DEFAULT_OPTIONS = types.MappingProxyType(
    {
        CONF_PROMPT: DEFAULT_PROMPT,
        CONF_AGENT_ID: DEFAULT_AGENT_ID,
        CONF_SESSION_KEY: DEFAULT_SESSION_KEY,
        CONF_CONTEXT_THRESHOLD: DEFAULT_CONTEXT_THRESHOLD,
        CONF_CONTEXT_TRUNCATE_STRATEGY: DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
    }
)

DEFAULT_AI_TASK_OPTIONS = types.MappingProxyType(
    {
        CONF_AGENT_ID: DEFAULT_AGENT_ID,
        CONF_SESSION_KEY: DEFAULT_SESSION_KEY,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    base_url = data[CONF_OPENCLAW_URL].rstrip("/")
    api_key = data[CONF_API_KEY]
    verify_ssl = data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

    # Use Home Assistant's httpx client helper to avoid blocking I/O during SSL setup
    client = get_async_client(hass, verify_ssl=verify_ssl)

    # Validate connectivity and auth by POSTing to the chat completions endpoint.
    # OpenClaw's gateway doesn't implement /v1/models, so we send a minimal
    # request to the endpoint the integration actually uses at runtime.
    # A 401 means bad auth; a connection/timeout error means unreachable;
    # any other response (including 400 for bad payload) means success.
    response = await client.post(
        f"{base_url}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-openclaw-agent-id": DEFAULT_AGENT_ID,
        },
        json={"model": "openclaw", "messages": []},
        timeout=10.0,
    )
    if response.status_code == 401:
        response.raise_for_status()


class OpenClawConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenClaw Conversation."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return OpenClawOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors = {}

        try:
            await validate_input(self.hass, user_input)
        except httpx.ConnectError:
            errors["base"] = "cannot_connect"
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 401:
                errors["base"] = "invalid_auth"
            else:
                _LOGGER.exception("HTTP error during validation")
                errors["base"] = "cannot_connect"
        except httpx.TimeoutException:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            # Normalize the URL by stripping trailing slash
            user_input[CONF_OPENCLAW_URL] = user_input[CONF_OPENCLAW_URL].rstrip("/")
            # Don't create default subentries - user must add conversation agents
            # manually so they can provide the required HA MCP URL
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {
            "conversation": OpenClawSubentryFlowHandler,
            "ai_task_data": OpenClawAITaskSubentryFlowHandler,
        }


class OpenClawSubentryFlowHandler(ConfigSubentryFlow):
    """Flow for managing OpenClaw conversation subentries."""

    options: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        """Return if this is a new subentry."""
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry."""
        self.options = dict(DEFAULT_OPTIONS)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a subentry."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manage the options."""
        # Abort if entry is not loaded
        if self._get_entry().state != ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is not None:
            if self._is_new:
                title = user_input.get(CONF_NAME, DEFAULT_CONVERSATION_NAME)
                if CONF_NAME in user_input:
                    del user_input[CONF_NAME]
                return self.async_create_entry(
                    title=title,
                    data=user_input,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=user_input,
            )

        schema = self.openclaw_config_option_schema(self.options)

        if self._is_new:
            schema = {
                vol.Optional(CONF_NAME, default=DEFAULT_CONVERSATION_NAME): str,
                **schema,
            }

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.options
            ),
        )

    def openclaw_config_option_schema(self, options: dict[str, Any]) -> dict:
        """Return a schema for OpenClaw conversation options."""
        return {
            vol.Required(CONF_HA_MCP_URL): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Optional(
                CONF_PROMPT,
                default=DEFAULT_PROMPT,
            ): TemplateSelector(),
            vol.Optional(
                CONF_AGENT_ID,
                default=DEFAULT_AGENT_ID,
            ): str,
            vol.Optional(
                CONF_SESSION_KEY,
                default=DEFAULT_SESSION_KEY,
            ): str,
            vol.Optional(
                CONF_CONTEXT_THRESHOLD,
                default=DEFAULT_CONTEXT_THRESHOLD,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1000, max=100000, step=1000, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_CONTEXT_TRUNCATE_STRATEGY,
                default=DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=strategy["key"], label=strategy["label"])
                        for strategy in CONTEXT_TRUNCATE_STRATEGIES
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }


class OpenClawAITaskSubentryFlowHandler(ConfigSubentryFlow):
    """Flow for managing OpenClaw AI Task subentries."""

    options: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        """Return if this is a new subentry."""
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry."""
        self.options = dict(DEFAULT_AI_TASK_OPTIONS)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a subentry."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manage the options."""
        # Abort if entry is not loaded
        if self._get_entry().state != ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is not None:
            if self._is_new:
                title = user_input.get(CONF_NAME, DEFAULT_AI_TASK_NAME)
                if CONF_NAME in user_input:
                    del user_input[CONF_NAME]
                return self.async_create_entry(
                    title=title,
                    data=user_input,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=user_input,
            )

        schema: dict = {}

        if self._is_new:
            schema[vol.Optional(CONF_NAME, default=DEFAULT_AI_TASK_NAME)] = str

        schema.update(
            {
                vol.Optional(
                    CONF_AGENT_ID,
                    default=DEFAULT_AGENT_ID,
                ): str,
                vol.Optional(
                    CONF_SESSION_KEY,
                    default=DEFAULT_SESSION_KEY,
                ): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.options
            ),
        )


class OpenClawOptionsFlowHandler(OptionsFlow):
    """Handle OpenClaw Conversation options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate connectivity with new settings
            test_data = {
                CONF_OPENCLAW_URL: user_input.get(
                    CONF_OPENCLAW_URL, self.config_entry.data[CONF_OPENCLAW_URL]
                ),
                CONF_API_KEY: user_input.get(
                    CONF_API_KEY, self.config_entry.data[CONF_API_KEY]
                ),
                CONF_VERIFY_SSL: user_input.get(
                    CONF_VERIFY_SSL,
                    self.config_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                ),
            }

            try:
                await validate_input(self.hass, test_data)
            except httpx.ConnectError:
                errors["base"] = "cannot_connect"
            except httpx.HTTPStatusError as err:
                if err.response.status_code == 401:
                    errors["base"] = "invalid_auth"
                else:
                    _LOGGER.exception("HTTP error during validation")
                    errors["base"] = "cannot_connect"
            except httpx.TimeoutException:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Normalize URL and update config entry data
                user_input[CONF_OPENCLAW_URL] = user_input[CONF_OPENCLAW_URL].rstrip(
                    "/"
                )
                new_data = {**self.config_entry.data, **user_input}
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                return self.async_create_entry(data={})

        # Build schema with current values as defaults
        current_data = self.config_entry.data
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_OPENCLAW_URL,
                    default=current_data.get(CONF_OPENCLAW_URL, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(
                    CONF_API_KEY,
                    default=current_data.get(CONF_API_KEY, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    CONF_VERIFY_SSL,
                    default=current_data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
