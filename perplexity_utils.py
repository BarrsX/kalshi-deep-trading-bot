"""
Utilities for working with Perplexity API for structured output parsing.

This module provides helpers to:
- Create chat completions with Perplexity's API
- Parse structured outputs into Pydantic models
- Extract JSON from model responses with real-time web search grounding

Perplexity models used:
- sonar-pro: Best for structured JSON output (direct responses without extended thinking)
- sonar-reasoning: Has <think> blocks that can consume tokens - avoid for JSON output
"""

from typing import Any, Dict, List, Optional, Sequence, Type, TypeVar, cast
import json
import re

import httpx
from pydantic import BaseModel

from config import PerplexityConfig


T = TypeVar("T", bound=BaseModel)


PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"


def _normalize_messages(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalize messages to Perplexity's expected format.
    """
    normalized: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "user")
        if isinstance(content, list):
            # Convert list of content parts to string
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)
        normalized.append({"role": role, "content": str(content) if content is not None else ""})
    return normalized


def _extract_json_from_text(text: str) -> Optional[str]:
    """
    Extract JSON from text that may contain markdown code blocks, <think> blocks, or other formatting.

    Handles sonar-reasoning model responses that include <think>...</think> chain-of-thought blocks.
    Also handles cases where Perplexity outputs action/fetch text before the actual JSON.
    """
    # First, remove any <think>...</think> blocks (sonar-reasoning chain-of-thought)
    # These blocks contain reasoning that precedes the actual JSON output
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)

    # Also handle unclosed <think> blocks (response was truncated before closing)
    # Remove everything from <think> to end of string if no closing tag
    text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.IGNORECASE)

    # Remove common Perplexity action/fetch prefixes that appear before JSON
    # These are internal action outputs that sometimes leak into responses
    action_patterns = [
        r"^.*?(?:Now |)let me fetch[^{]*",
        r"^.*?(?:Now |)I(?:'ll| will) (?:fetch|search|look up)[^{]*",
        r"^.*?Fetching[^{]*",
        r"^.*?Searching[^{]*",
    ]
    for pattern in action_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)

    text = text.strip()

    # If text is empty after removing think blocks, return None
    if not text:
        return None

    # Try to find JSON in code blocks first
    code_block_patterns = [
        r"```json\s*([\s\S]*?)\s*```",
        r"```\s*([\s\S]*?)\s*```",
    ]

    for pattern in code_block_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            potential_json = match.group(1).strip()
            try:
                json.loads(potential_json)
                return potential_json
            except json.JSONDecodeError:
                continue

    # Try to find a JSON object directly
    # Look for content starting with { and ending with }
    # Use a more robust approach to find the outermost JSON object
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        potential_json = brace_match.group(0)
        try:
            json.loads(potential_json)
            return potential_json
        except json.JSONDecodeError:
            # Try to find balanced braces by finding the first { and matching }
            start_idx = potential_json.find("{")
            if start_idx != -1:
                brace_count = 0
                for i, char in enumerate(potential_json[start_idx:], start=start_idx):
                    if char == "{":
                        brace_count += 1
                    elif char == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            balanced_json = potential_json[start_idx : i + 1]
                            try:
                                json.loads(balanced_json)
                                return balanced_json
                            except json.JSONDecodeError:
                                break

    # Try the entire text as JSON
    try:
        json.loads(text.strip())
        return text.strip()
    except json.JSONDecodeError:
        pass

    return None


async def perplexity_create_text(
    config: PerplexityConfig,
    *,
    messages: Sequence[Dict[str, Any]],
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """
    Create a Perplexity API call for free-form text response.

    Uses real-time web search for grounded responses.
    """
    normalized = _normalize_messages(messages)
    use_model = model or config.model or "sonar-reasoning"

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            PERPLEXITY_API_URL,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": use_model,
                "messages": normalized,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "search_recency_filter": "day",  # Use recent data only
                "return_citations": False,
            },
        )
        response.raise_for_status()
        data = response.json()

        # Extract content from response
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return ""


def _is_incomplete_response(text: str) -> bool:
    """
    Check if the response appears to be an incomplete action/fetch response.

    Perplexity sometimes returns internal action text without the actual result.
    """
    incomplete_indicators = [
        "let me fetch",
        "now let me",
        "i will search",
        "i'll search",
        "i will fetch",
        "i'll fetch",
        "let me search",
        "fetching",
        "searching for",
    ]
    text_lower = text.lower().strip()

    # Check if response starts with or primarily contains action text
    for indicator in incomplete_indicators:
        if indicator in text_lower:
            # If there's no JSON brace after the indicator, it's incomplete
            if "{" not in text:
                return True
    return False


async def perplexity_parse_pydantic(
    config: PerplexityConfig,
    *,
    messages: Sequence[Dict[str, Any]],
    response_format: Type[T],
    model: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 16384,
    _retry_count: int = 0,
) -> T:
    """
    Parse structured output using Perplexity API into the provided Pydantic model type.

    Uses sonar model for better structured output handling.
    Includes JSON schema in the prompt for reliable parsing.
    """
    MAX_RETRIES = 2
    normalized = _normalize_messages(messages)
    # Use sonar for structured output - it's more reliable for JSON output
    # sonar-pro and sonar-reasoning have been exhibiting issues with outputting
    # internal action text instead of JSON responses
    use_model = model or "sonar"

    # Build JSON schema from the Pydantic model
    try:
        schema = response_format.model_json_schema()
    except Exception:
        # Pydantic v1 fallback
        schema = response_format.schema()  # type: ignore[attr-defined]

    schema_str = json.dumps(schema, indent=2)

    # Create a system message with JSON schema instruction
    # Using a very strict prompt to prevent Perplexity from outputting action text
    schema_instruction = {
        "role": "system",
        "content": (
            "IMPORTANT: You must respond with ONLY valid JSON. No other text.\n\n"
            "RULES:\n"
            "- Start your response with { and end with }\n"
            "- No markdown, no code blocks, no explanations\n"
            "- No phrases like 'Let me', 'I will', 'Based on', etc.\n"
            "- No tables, no bullet points, no headers\n"
            "- ONLY the JSON object matching this schema:\n\n"
            f"{schema_str}\n\n"
            "Your entire response must be parseable by JSON.parse()."
        ),
    }

    # Prepend schema instruction
    messages_with_schema = [schema_instruction] + list(normalized)

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            PERPLEXITY_API_URL,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": use_model,
                "messages": messages_with_schema,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "search_recency_filter": "day",  # Use recent data for analysis
                "return_citations": False,
            },
        )
        response.raise_for_status()
        data = response.json()

        # Extract content from response
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Perplexity API returned no choices")

        text_content = choices[0].get("message", {}).get("content", "")

        if not text_content:
            raise RuntimeError("Perplexity API returned empty content")

        # Check if this is an incomplete response (Perplexity action/fetch leak)
        if _is_incomplete_response(text_content) and _retry_count < MAX_RETRIES:
            import asyncio

            await asyncio.sleep(1)  # Brief delay before retry
            return await perplexity_parse_pydantic(
                config,
                messages=messages,
                response_format=response_format,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                _retry_count=_retry_count + 1,
            )

        # Extract JSON from the response (handles markdown code blocks, etc.)
        json_str = _extract_json_from_text(text_content)

        if not json_str:
            raise RuntimeError(
                f"Could not extract valid JSON from Perplexity response. "
                f"Response content: {text_content[:500]}..."
            )

        try:
            parsed_data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse JSON from Perplexity response: {e}")

        # Validate and parse with Pydantic
        try:
            return cast(T, response_format.model_validate(parsed_data))
        except Exception:
            # Pydantic v1 fallback
            return cast(T, response_format.parse_obj(parsed_data))  # type: ignore[attr-defined]
