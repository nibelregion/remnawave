from __future__ import annotations

import json
import os
import re
import typing
import urllib.parse
from dataclasses import dataclass

import wreq.blocking as http

ERRORS_SOURCE_URL: typing.Final = (
    "https://raw.githubusercontent.com/remnawave/backend/main/"
    "libs/contract/constants/errors/errors.ts"
)
GITHUB_API_URL: typing.Final = "https://api.github.com"
BACKEND_REPOSITORY: typing.Final = "remnawave/backend"
ERROR_CODE_EXCEPTION: typing.Final = "HttpExceptionWithErrorCodeType"


@dataclass(frozen=True, slots=True)
class ErrorCode:
    member: str
    value: str
    description: str | None = None


NICIFICATED_ERROR_CODES: typing.Final = (
    ErrorCode(
        "UNAUTHORIZED_ERROR",
        "E401",
        "Unauthorized. HTTP 401.",
    ),
    ErrorCode(
        "FORBIDDEN_ERROR",
        "E403",
        "Forbidden. HTTP 403.",
    ),
    ErrorCode(
        "INTERNAL_SERVER_ERROR",
        "E500",
        "Internal server error. HTTP 500.",
    ),
)


def collect_error_codes(*, github_token: str | None = None) -> list[ErrorCode]:
    source = _download_text(ERRORS_SOURCE_URL, github_token=github_token)

    if not source:
        raise LookupError("Unable to download source.")

    declared = parse_declared_error_codes(source)
    declared.extend(NICIFICATED_ERROR_CODES)

    literals = search_literal_error_codes(github_token=github_token)
    known_values = {error.value for error in declared}

    declared.extend(
        ErrorCode(member=_literal_member_name(value), value=value)
        for value in sorted(literals - known_values)
    )
    return declared


def parse_declared_error_codes(source: str, /) -> list[ErrorCode]:
    errors_block = _errors_constant_block(source)
    errors: list[ErrorCode] = []

    for member, body in _error_entries(errors_block):
        value = _object_string_property(body, "code|error_code")
        if value is None:
            continue

        message = _object_string_property(body, "message")
        http_code = _object_integer_property(body, "httpCode|http_code")
        description = _format_description(message, http_code)
        errors.append(ErrorCode(member=member, value=value, description=description))

    if not errors:
        raise ValueError("Could not parse any error codes from the backend ERRORS constant")

    return sorted(errors, key=lambda error: (_error_code_sort_key(error.value), error.member))


def _error_entries(source: str, /) -> typing.Iterator[tuple[str, str]]:
    start = 0
    entry_pattern = re.compile(r"(?m)^\s*(\w+)\s*:\s*\{")

    while (match := entry_pattern.search(source, start)) is not None:
        member = match.group(1)
        body_start = source.find("{", match.start())
        body_end = _matching_brace(source, body_start)

        yield member, source[body_start + 1 : body_end]

        start = body_end + 1


def search_literal_error_codes(*, github_token: str | None = None) -> set[str]:
    query = f"{ERROR_CODE_EXCEPTION} repo:{BACKEND_REPOSITORY}"
    params = urllib.parse.urlencode({"q": query, "per_page": 100})
    result = _download_json(f"{GITHUB_API_URL}/search/code?{params}", github_token=github_token)

    if not result:
        # gh's code search requires authentication for some callers, so the ERRORS
        # const remains a complete baseline when a token is not configured
        return set()

    items = result.get("items")

    if not isinstance(items, list):
        return set()

    values: set[str] = set()

    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue

        source_url = (
            f"https://raw.githubusercontent.com/{BACKEND_REPOSITORY}/main/{item['path']}"
        )
        source = _download_text(source_url, github_token=github_token)

        if source:
            values.update(_literal_exception_error_codes(source))

    return values


def _download_json(url: str, /, *, github_token: str | None = None) -> dict[str, typing.Any] | None:
    text = _download_text(url, github_token=github_token)

    if text is None:
        return None

    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object from {url}")

    return typing.cast("dict[str, typing.Any]", value)


def _download_text(url: str, /, *, github_token: str | None = None) -> str | None:
    token = github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "remnawave-openapi"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = http.get(url, headers=headers)

    if response.status.as_int() in {401, 403}:
        return None

    return response.raise_for_status() or response.text("utf-8")


def _errors_constant_block(source: str, /) -> str:
    start_match = re.search(r"export\s+const\s+ERRORS\s*=\s*\{", source)

    if start_match is None:
        raise ValueError("Could not find the backend ERRORS constant")

    start = source.find("{", start_match.start())
    end = _matching_brace(source, start)
    return source[start + 1 : end]


def _object_string_property(source: str, name_pattern: str, /) -> str | None:
    match = re.search(rf"(?:{name_pattern})\s*:\s*(['\"])(.*?)\1", source, re.DOTALL)
    return match.group(2) if match else None


def _object_integer_property(source: str, name_pattern: str, /) -> int | None:
    match = re.search(rf"(?:{name_pattern})\s*:\s*(\d+)", source)
    return int(match.group(1)) if match else None


def _format_description(message: str | None, http_code: int | None, /) -> str | None:
    if not message:
        return None

    message = message.rstrip(".") + "."
    return f"{message} HTTP {http_code}." if http_code is not None else message


def _literal_exception_error_codes(source: str, /) -> set[str]:
    values: set[str] = set()
    marker = f"{ERROR_CODE_EXCEPTION}("
    start = 0

    while (call_start := source.find(marker, start)) != -1:
        arguments_start = call_start + len(marker) - 1
        arguments_end = _matching_parenthesis(source, arguments_start)
        arguments = _split_call_arguments(source[arguments_start + 1 : arguments_end])

        if len(arguments) > 1:
            literal = _string_literal(arguments[1])

            if literal is not None:
                values.add(literal)

        start = arguments_end + 1

    return values


def _split_call_arguments(value: str, /) -> list[str]:
    arguments: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False

    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None

            continue

        if character in {"'", '"', "`"}:
            quote = character

        elif character in "([{":
            depth += 1

        elif character in ")]}":
            depth -= 1

        elif character == "," and depth == 0:
            arguments.append(value[start:index].strip())
            start = index + 1

    arguments.append(value[start:].strip())
    return arguments


def _string_literal(value: str, /) -> str | None:
    match = re.fullmatch(r"\s*(['\"])(.*?)\1\s*", value, re.DOTALL)
    return match.group(2) if match else None


def _matching_brace(value: str, start: int, /) -> int:
    return _matching_delimiter(value, start, "{", "}")


def _matching_parenthesis(value: str, start: int, /) -> int:
    return _matching_delimiter(value, start, "(", ")")


def _matching_delimiter(value: str, start: int, opening: str, closing: str, /) -> int:
    depth = 0
    quote: str | None = None
    escaped = False

    for index in range(start, len(value)):
        character = value[index]

        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None

            continue

        if character in {"'", '"', "`"}:
            quote = character

        elif character == opening:
            depth += 1

        elif character == closing:
            depth -= 1

            if depth == 0:
                return index

    raise ValueError(f"Unclosed {opening} in TypeScript source")


def _literal_member_name(value: str, /) -> str:
    words = re.findall(r"[0-9A-Za-z]+", value)
    member = "_".join(word.upper() for word in words) or "ERROR_CODE"
    return f"LITERAL_{member}" if member[:1].isdigit() else member


def _error_code_sort_key(value: str, /) -> tuple[str, int, str]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", value)
    return (match.group(1), int(match.group(2)), value) if match else (value, -1, value)
