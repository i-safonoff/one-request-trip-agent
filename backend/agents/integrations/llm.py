"""LLM access behind one interface.

Agents call `llm.structured(system, user, Schema)` and get a validated Pydantic object. The GigaChat
implementation asks for a JSON-schema response format, validates the answer with Pydantic and retries
once with the validation error. Tests use `FakeLLM`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings

log = logging.getLogger("llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLM(Protocol):
    async def structured(self, *, system: str, user: str, schema: type[T], max_tokens: int = 1500) -> T: ...


def extract_json(text: str) -> Any:
    """First JSON object in the text: tolerates ```json fences and chatter around the object."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the model answer")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in the model answer")


def unwrap_schema_echo(obj: Any, schema: type[BaseModel]) -> Any:
    """GigaChat with a json_schema response_format often echoes the schema shape: the fields come wrapped in
    {"properties": {...}} (sometimes next to real top-level fields). Lift them up; real fields win."""
    if isinstance(obj, dict) and isinstance(obj.get("properties"), dict) and "properties" not in schema.model_fields:
        inner = obj["properties"]
        return {**inner, **{k: v for k, v in obj.items() if k != "properties"}}
    return obj


def inline_refs(schema: dict) -> dict:
    """JSON Schema without $defs/$ref (every reference replaced by its definition): safer for model APIs
    whose schema support is partial. Our schemas are not recursive, so this terminates."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)


def flat_schema(schema: type[BaseModel]) -> dict:
    return inline_refs(schema.model_json_schema())


def schema_hint(schema: type[BaseModel]) -> str:
    return json.dumps(flat_schema(schema), ensure_ascii=False)


class GigaChatLLM:
    def __init__(self, settings: Settings):
        if not settings.gigachat_auth_key:
            raise LLMError("GIGACHAT_AUTH_KEY is not set")
        from gigachat import GigaChat  # imported lazily: tests run without credentials
        kwargs: dict[str, Any] = dict(credentials=settings.gigachat_auth_key, scope=settings.gigachat_scope,
                                      model=settings.gigachat_model, timeout=settings.gigachat_timeout,
                                      verify_ssl_certs=settings.gigachat_verify_ssl, max_retries=2)
        if settings.gigachat_ca_bundle:
            kwargs["ca_bundle_file"] = settings.gigachat_ca_bundle
        if settings.gigachat_base_url:
            kwargs["base_url"] = settings.gigachat_base_url
        self._giga = GigaChat(**kwargs)
        self._sem = asyncio.Semaphore(max(1, settings.gigachat_max_concurrency))
        self._use_schema = settings.gigachat_json_schema

    async def _achat_with_backoff(self, chat, attempts: int = 6):
        """The free tier answers 429 under parallel load: wait and retry instead of failing the agent."""
        for i in range(attempts):
            try:
                return await self._giga.achat(chat)
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ != "RateLimitError" and "429" not in str(e) or i == attempts - 1:
                    raise
                delay = 1.5 * (i + 1)
                log.warning("GigaChat 429, retry %d/%d in %.1fs", i + 1, attempts - 1, delay)
                await asyncio.sleep(delay)

    async def _complete(self, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> str:
        from gigachat.models import Chat, Messages, MessagesRole
        from gigachat.models.response_format import JsonSchemaResponseFormat
        messages = [Messages(role=MessagesRole.SYSTEM, content=system), Messages(role=MessagesRole.USER, content=user)]
        async with self._sem:
            for use_schema in ([True, False] if self._use_schema else [False]):
                chat = Chat(messages=messages, temperature=0.1, max_tokens=max_tokens,
                            response_format=JsonSchemaResponseFormat(schema=flat_schema(schema)) if use_schema else None)
                try:
                    resp = await self._achat_with_backoff(chat)
                    return resp.choices[0].message.content or ""
                except Exception as e:  # noqa: BLE001 - SDK raises several exception types
                    if use_schema and "response_format" in str(e).lower() or use_schema and "schema" in str(e).lower():
                        log.warning("GigaChat rejected response_format, falling back to prompt-only JSON: %s", e)
                        self._use_schema = False
                        continue
                    raise LLMError(f"{type(e).__name__}: {e}") from e
        raise LLMError("no completion")

    async def structured(self, *, system: str, user: str, schema: type[T], max_tokens: int = 1500) -> T:
        sys_prompt = f"{system}\n\nОтвечай ТОЛЬКО одним JSON-объектом по схеме, без пояснений:\n{schema_hint(schema)}"
        text = await self._complete(sys_prompt, user, schema, max_tokens)
        try:
            return schema.model_validate(unwrap_schema_echo(extract_json(text), schema))
        except (ValueError, ValidationError) as first:
            log.warning("invalid model JSON (%s), retrying once", str(first)[:200])
            fix = f"{user}\n\nТвой прошлый ответ не прошёл проверку: {str(first)[:600]}\nИсправь и верни только JSON."
            text = await self._complete(sys_prompt, fix, schema, max_tokens)
            try:
                return schema.model_validate(unwrap_schema_echo(extract_json(text), schema))
            except (ValueError, ValidationError) as e:
                raise LLMError(f"model returned invalid JSON twice: {str(e)[:300]}") from e


class FakeLLM:
    """Scripted LLM for tests: `script[SchemaName]` is a dict, a model, or a callable (system, user) -> either."""

    def __init__(self, script: dict[str, Any] | None = None):
        self.script = script or {}
        self.calls: list[tuple[str, str, str]] = []

    async def structured(self, *, system: str, user: str, schema: type[T], max_tokens: int = 1500) -> T:
        self.calls.append((schema.__name__, system, user))
        item = self.script.get(schema.__name__)
        if item is None:
            raise LLMError(f"FakeLLM has no script for {schema.__name__}")
        if callable(item):
            item = item(system, user)
        return item if isinstance(item, schema) else schema.model_validate(item)


def make_llm(settings: Settings) -> LLM:
    return GigaChatLLM(settings)
