from __future__ import annotations

import os
import re
import typing
from dataclasses import dataclass

ERROR_CODE_EXCEPTION: typing.Final = "HttpExceptionWithErrorCodeType"
ERRORS_SOURCE_PATH: typing.Final = (
    "libs",
    "contract",
    "constants",
    "errors",
    "errors.ts",
)


@dataclass(frozen=True, slots=True)
class ErrorCode:
    member: str
    value: str
    description: str | None = None


NICIFICATED_ERROR_CODES: typing.Final = (
    ErrorCode(
        "E401",
        "E401",
        "Unauthorized. HTTP 401.",
    ),
    ErrorCode(
        "E403",
        "E403",
        "Forbidden. HTTP 403.",
    ),
    ErrorCode(
        "E500",
        "E500",
        "Internal server error. HTTP 500.",
    ),
)


def collect_error_codes(source_dir: str, /) -> list[ErrorCode]:
    source = _read_errors_source(source_dir)

    declared = parse_declared_error_codes(source)
    declared.extend(NICIFICATED_ERROR_CODES)

    literals = search_literal_error_codes(source_dir)
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


def search_literal_error_codes(source_dir: str, /) -> set[str]:
    values: set[str] = set()
    for current, directories, files in os.walk(source_dir):
        directories[:] = [
            directory for directory in directories if directory not in {".git", "node_modules"}
        ]
        for filename in files:
            if not filename.endswith(".ts"):
                continue

            try:
                with open(os.path.join(current, filename), encoding="utf-8") as handle:
                    values.update(_literal_exception_error_codes(handle.read()))
            except OSError:
                continue

    return values


def _read_errors_source(source_dir: str, /) -> str:
    path = os.path.join(source_dir, *ERRORS_SOURCE_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise LookupError(f"Unable to read backend error codes from {path}") from error


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
