"""Minimal MCP client over streamable HTTP (JSON-RPC), read-only by construction.

Only tools in READ_ONLY_TOOLS can be called. Tools that create carts, orders or checkout links
(`create_checkout_link`, `register_checkout_passengers`) are deliberately absent: by Tutu's own
instructions the avia flow places a real unpaid order, and the product story is that purchase
happens inside our service (it is simulated there).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

log = logging.getLogger("mcp")

READ_ONLY_TOOLS = {
    "search_hotels", "get_offer_details", "get_hotels_instructions", "fetch_resource",
    "search_avia", "search_rail", "search_bus", "search_etrain", "search_multitransport",
}
PROTOCOL = "2025-06-18"


class McpError(Exception):
    pass


class SourceUnavailable(McpError):
    """The server could not be reached or timed out (as opposed to answering with an error)."""


class McpClient:
    def __init__(self, url: str, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None,
                 retry_delays: tuple[float, ...] = (0.5, 1.5, 3.0)):
        self.url = url
        self.retry_delays = retry_delays  # seconds before each retry of a transient network failure
        # a short connect timeout: mcp.tutu.ru sometimes accepts a connection slowly, and a retry is cheaper than 30 s of waiting
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)), transport=transport)
        self._headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self._ready = False
        self._lock = asyncio.Lock()
        self._id = 0
        self.tools: dict[str, dict] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict:
        """JSON-RPC call; transient network failures (timeouts, 5xx) are retried, tool errors are not."""
        for delay in (*self.retry_delays, None):
            try:
                return await self._rpc_once(method, params, notify=notify)
            except SourceUnavailable as e:
                if delay is None:
                    raise
                log.info("MCP %s failed (%s), retrying in %.1fs", method, e, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _rpc_once(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._id += 1
            payload["id"] = self._id
        try:
            r = await self._http.post(self.url, json=payload, headers=self._headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 500:
            raise SourceUnavailable(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}: {r.text[:300]}")
        if sid := r.headers.get("Mcp-Session-Id"):
            self._headers["Mcp-Session-Id"] = sid
        if notify or not r.text.strip():
            return {}
        if "text/event-stream" in r.headers.get("content-type", ""):
            msgs = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:") and line[5:].strip()]
            msg = next((m for m in msgs if m.get("id") == payload.get("id")), msgs[-1] if msgs else {})
        else:
            msg = r.json()
        if "error" in msg:
            raise McpError(str(msg["error"]))
        return msg.get("result", {})

    async def _ensure(self) -> None:
        async with self._lock:
            if self._ready:
                return
            init = await self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                  "clientInfo": {"name": "ekvivalentno", "version": "1.0"}})
            self._headers["MCP-Protocol-Version"] = init.get("protocolVersion", PROTOCOL)
            await self._rpc("notifications/initialized", notify=True)
            listed = await self._rpc("tools/list", {})
            self.tools = {t["name"]: t for t in listed.get("tools", [])}
            self._ready = True

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in READ_ONLY_TOOLS:
            raise McpError(f"tool '{name}' is not allowed (read-only whitelist)")
        await self._ensure()
        if self.tools and name not in self.tools:
            raise McpError(f"server has no tool '{name}'")
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise McpError(text[:500] or "tool error")
        if result.get("structuredContent"):
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            return text
