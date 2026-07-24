"""Cache the Remnawave backend release source and mine schema names from it.

The nicifier gives inline OpenAPI objects generated names based on their path
context. Those generated names are then mapped to hand-written names inside
``nicifications.yaml``. To keep that mapping fresh, this module downloads the
backend release whose tag matches the OpenAPI ``info.version`` and parses the
TypeScript Zod schemas so inline objects can be matched to their source names.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import typing
from dataclasses import dataclass, field

import wreq.blocking as http

BACKEND_REPOSITORY: typing.Final = "remnawave/backend"
GITHUB_API_URL: typing.Final = "https://api.github.com"
GENERIC_SCHEMA_NAMES: typing.Final = frozenset(
    (
        "Request",
        "RequestQuery",
        "RequestBody",
        "RequestParams",
        "RequestHeaders",
        "Response",
        "ResponseBody",
    )
)
CODELOAD_URL: typing.Final = "https://codeload.github.com"
CONTRACT_GLOB_PREFIX: typing.Final = "libs/contract"
SCHEMA_DECLARATION_RE: typing.Final = re.compile(
    r"(?:export\s+)?const\s+(\w+?)Schema\s*=\s*z\.object\s*\(",
)
LITERAL_PROPERTY_RE: typing.Final = re.compile(
    r"z\.literal\(\s*(['\"])(.*?)\1\s*\)",
)


@dataclass(frozen=True, slots=True)
class TsSchema:
    name: str
    properties: tuple[str, ...]
    literals: tuple[tuple[str, str], ...]

    @property
    def fingerprint(self) -> tuple[typing.Any, ...]:
        return _fingerprint(self.properties, self.literals)


@dataclass(slots=True)
class SchemaIndex:
    by_fingerprint: dict[tuple[typing.Any, ...], set[str]] = field(
        default_factory=dict,
    )

    def add(self, schema: TsSchema, /) -> None:
        if not schema.properties or schema.name in GENERIC_SCHEMA_NAMES:
            return

        self.by_fingerprint.setdefault(schema.fingerprint, set()).add(schema.name)

    def resolve(self, fingerprint: tuple[typing.Any, ...], /) -> str | None:
        names = self.by_fingerprint.get(fingerprint)
        if not names or len(names) > 1:
            return None

        return next(iter(names))


def object_fingerprint(
    properties: typing.Iterable[str],
    literals: typing.Iterable[tuple[str, str]],
    /,
) -> tuple[typing.Any, ...]:
    return (
        tuple(sorted(str(name) for name in properties)),
        tuple(sorted((str(name), str(value)) for name, value in literals)),
    )


_fingerprint = object_fingerprint


def build_schema_index(source_dir: str, /) -> SchemaIndex:
    index = SchemaIndex()

    for schema in _iter_source_schemas(source_dir):
        index.add(schema)

    return index


def _iter_source_schemas(source_dir: str, /) -> typing.Iterator[TsSchema]:
    contract_root = os.path.join(source_dir, *CONTRACT_GLOB_PREFIX.split("/"))
    root = contract_root if os.path.isdir(contract_root) else source_dir

    for current, _, files in os.walk(root):
        for filename in files:
            if not filename.endswith(".ts"):
                continue

            path = os.path.join(current, filename)

            try:
                text = _read_text(path)
            except OSError:
                continue

            yield from parse_ts_schemas(text)


def _read_text(path: str, /) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def parse_ts_schemas(text: str, /) -> typing.Iterator[TsSchema]:
    for match in SCHEMA_DECLARATION_RE.finditer(text):
        name = match.group(1)

        brace_start = text.find("{", match.end() - 1)
        if brace_start == -1:
            continue

        brace_end = _matching(text, brace_start, "{", "}")
        if brace_end == -1:
            continue

        body = text[brace_start + 1 : brace_end]
        properties = tuple(_top_level_keys(body))
        literals = tuple(_top_level_literals(body))
        yield TsSchema(name=name, properties=properties, literals=literals)


def _top_level_keys(body: str, /) -> typing.Iterator[str]:
    for key, _ in _top_level_entries(body):
        yield key


def _top_level_literals(body: str, /) -> typing.Iterator[tuple[str, str]]:
    for key, value in _top_level_entries(body):
        literal = LITERAL_PROPERTY_RE.search(value)
        if literal is not None:
            yield key, literal.group(2)


def _top_level_entries(body: str, /) -> typing.Iterator[tuple[str, str]]:
    index = 0
    length = len(body)

    while index < length:
        key_match = re.compile(r"[\s,]*([A-Za-z_$][\w$]*)\s*:").match(body, index)

        if key_match is None:
            index = _skip_to_next_entry(body, index)
            continue

        key = key_match.group(1)
        value_start = key_match.end()
        value_end = _entry_value_end(body, value_start)

        yield key, body[value_start:value_end]

        index = value_end


def _skip_to_next_entry(body: str, index: int, /) -> int:
    comma = body.find(",", index)
    return len(body) if comma == -1 else comma + 1


def _entry_value_end(body: str, start: int, /) -> int:
    depth = 0
    quote: str | None = None
    escaped = False

    for index in range(start, len(body)):
        character = body[index]

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
            return index + 1

    return len(body)


def _matching(text: str, start: int, opening: str, closing: str, /) -> int:
    depth = 0
    quote: str | None = None
    escaped = False

    for index in range(start, len(text)):
        character = text[index]

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

    return -1


def resolve_source_dir(
    version: str | None,
    /,
    *,
    cache_dir: str,
    repository: str = BACKEND_REPOSITORY,
    github_token: str | None = None,
) -> str | None:
    tag = version if version and _tag_exists(repository, version, github_token=github_token) else None
    if tag is None:
        tag = _latest_release_tag(repository, github_token=github_token)

    if tag is None:
        return None

    destination = os.path.join(cache_dir, _safe_component(repository), _safe_component(tag))
    ready_marker = os.path.join(destination, ".ready")

    if os.path.isfile(ready_marker):
        return destination

    payload = _download_tarball(repository, tag, github_token=github_token)
    if payload is None:
        return None

    _extract_tarball(payload, destination)
    with open(ready_marker, "w", encoding="utf-8") as handle:
        handle.write(tag)

    return destination


def _tag_exists(repository: str, tag: str, /, *, github_token: str | None) -> bool:
    url = f"{GITHUB_API_URL}/repos/{repository}/git/ref/tags/{tag}"
    response = _request(url, github_token=github_token)
    return response is not None and response.status.as_int() == 200


def _latest_release_tag(repository: str, /, *, github_token: str | None) -> str | None:
    url = f"{GITHUB_API_URL}/repos/{repository}/releases/latest"
    response = _request(url, github_token=github_token)
    if response is None or response.status.as_int() != 200:
        return None

    import json

    data = json.loads(response.text("utf-8"))
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else None


def _download_tarball(repository: str, tag: str, /, *, github_token: str | None) -> bytes | None:
    url = f"{CODELOAD_URL}/{repository}/tar.gz/refs/tags/{tag}"
    response = _request(url, github_token=github_token)
    if response is None or response.status.as_int() != 200:
        return None

    return response.bytes()


def _extract_tarball(payload: bytes, destination: str, /) -> None:
    if os.path.isdir(destination):
        shutil.rmtree(destination)

    os.makedirs(destination, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        root_prefix = _common_root(members)

        for member in members:
            relative = _strip_root(member.name, root_prefix)
            if relative is None:
                continue

            target = _safe_join(destination, relative)
            if target is None:
                continue

            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue

                with extracted, open(target, "wb") as handle:
                    shutil.copyfileobj(extracted, handle)


def _common_root(members: list[tarfile.TarInfo], /) -> str:
    for member in members:
        head, _, _ = member.name.partition("/")
        if head:
            return head

    return ""


def _strip_root(name: str, root_prefix: str, /) -> str | None:
    if not root_prefix:
        return name or None

    if name == root_prefix:
        return ""

    prefix = f"{root_prefix}/"
    return name[len(prefix):] if name.startswith(prefix) else None


def _safe_join(destination: str, relative: str, /) -> str | None:
    target = os.path.normpath(os.path.join(destination, relative))
    root = os.path.normpath(destination)

    if target == root or target.startswith(root + os.sep):
        return target

    return None


def _safe_component(value: str, /) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value) or "unknown"


def _request(url: str, /, *, github_token: str | None) -> typing.Any:
    token = github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "remnawave-openapi"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        return http.get(url, headers=headers)
    except Exception:
        return None


__all__ = (
    "SchemaIndex",
    "TsSchema",
    "build_schema_index",
    "object_fingerprint",
    "parse_ts_schemas",
    "resolve_source_dir",
)
