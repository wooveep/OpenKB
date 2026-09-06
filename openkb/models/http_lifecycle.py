"""HTTP dispatch instrumentation for terminal Desktop model attempts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from openkb.config import LlmCredentialBundle


class _RequestLifecycleHttpxClient(httpx.Client):
    """Signal after the request body is on the wire, before response headers."""

    def __init__(
        self,
        *,
        timeout: httpx.Timeout,
        on_request_sent: Callable[[], None],
    ) -> None:
        super().__init__(timeout=timeout, follow_redirects=True)
        self._on_request_sent = on_request_sent

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        previous_trace = request.extensions.get("trace")

        def trace(event_name: str, info: dict[str, object]) -> None:
            if callable(previous_trace):
                previous_trace(event_name, info)
            if event_name.endswith(".send_request_body.complete"):
                self._on_request_sent()

        request.extensions["trace"] = trace
        return super().send(request, **kwargs)


def terminal_completion_client(
    *,
    model: str,
    bundle: LlmCredentialBundle,
    timeout: httpx.Timeout,
    on_request_sent: Callable[[], None],
) -> tuple[object, Callable[[], None]]:
    """Build the provider-specific LiteLLM client and its resource closer."""
    if not bundle.api_key:
        raise ValueError("A terminal completion client requires an API key.")
    http_client = _RequestLifecycleHttpxClient(
        timeout=timeout,
        on_request_sent=on_request_sent,
    )
    if model.startswith("deepseek/"):
        from litellm.llms.custom_httpx.http_handler import HTTPHandler

        return HTTPHandler(timeout=timeout, client=http_client), http_client.close

    from openai import OpenAI

    client = OpenAI(
        api_key=bundle.api_key,
        base_url=bundle.base_url,
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )
    return client, client.close
