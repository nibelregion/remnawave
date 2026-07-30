import json
import pathlib
import typing
from argparse import ArgumentParser
from sys import exit

import msgspec
import wreq.blocking as http
from error_codes import collect_error_codes
from nicifcations_schema import DEFAULT_SCHEMA_PATH, read_schema, write_schema
from nicifier_schema import (
    JSONObject,
    nicificate_openapi_document,
    update_enum_nicifications,
    update_object_nicifications,
)
from retcon.openapi.parser import decode_openapi_document
from source_cache import build_schema_index, resolve_source_dir

REMNA_OAS_URL: typing.Final = "https://cdn.remna.st/docs/openapi.json"
REMNA_OAS_DOCUMENT_TYPE: typing.Final = "json"
DEFAULT_OUTPUT_PATH: typing.Final = pathlib.Path("remnawave.json")
DEFAULT_MIN_OUTPUT_PATH: typing.Final = pathlib.Path("remnawave.min.json")


def download_remnawave(url: str = REMNA_OAS_URL, /) -> bytes:
    response = http.get(url=url)
    return response.raise_for_status() or response.bytes()


def read_openapi_source(source: str, /) -> bytes:
    if source.startswith(("http://", "https://")):
        return download_remnawave(source)
    return pathlib.Path(source).read_bytes()


def decode_raw_openapi(
    payload: bytes,
    document_type: typing.Literal["json", "yaml"] = REMNA_OAS_DOCUMENT_TYPE,
    /,
) -> JSONObject:
    raw = msgspec.json.decode(payload) if document_type == "json" else msgspec.yaml.decode(payload)

    if not isinstance(raw, dict):
        raise ValueError("OpenAPI document root must be an object")

    return typing.cast(JSONObject, raw)


def write_json(path: pathlib.Path, document: JSONObject, /, *, indent: int | None = 2) -> None:
    raw = json.dumps(
        obj=document,
        indent=indent,
        ensure_ascii=False,
        separators=None if indent is not None else (",", ":"),
    )
    path.write_text(data=raw + ("\n" if indent is not None else ""), encoding="utf-8")


def nicification_specification(
    *,
    source: str | None = None,
    nicifications_path: pathlib.Path = DEFAULT_SCHEMA_PATH,
    output_path: pathlib.Path = DEFAULT_OUTPUT_PATH,
    output_min_path: pathlib.Path = DEFAULT_MIN_OUTPUT_PATH,
    diff_path: pathlib.Path | None = None,
    previous_output_path: pathlib.Path | None = None,
    update_nicifications: bool = False,
) -> int:
    nicifications = read_schema(nicifications_path)
    source = source or nicifications.remnawave.schema_url
    document_type = nicifications.remnawave.schema_document_type
    payload = read_openapi_source(source)
    raw_document = decode_raw_openapi(payload, document_type)

    source_dir = _resolve_backend_source(raw_document, nicifications)
    if source_dir is None:
        raise LookupError("Unable to download backend release source.")

    error_codes = collect_error_codes(source_dir)
    previous_document: JSONObject | None = None

    previous_output_path = previous_output_path or output_path
    if previous_output_path.exists():
        previous_document = decode_raw_openapi(previous_output_path.read_bytes())

    if update_nicifications:
        changed = bool(update_enum_nicifications(raw_document, nicifications))
        changed |= _update_object_nicifications_from_source(
            raw_document,
            nicifications,
            source_dir=source_dir,
        )

        if changed:
            write_schema(nicifications, nicifications_path)

    result = nicificate_openapi_document(
        raw_document,
        nicifications,
        previous_document=previous_document,
        error_codes=error_codes,
    )

    decode_openapi_document(result.document, document_type)
    write_json(output_path, result.document)
    write_json(output_min_path, result.document, indent=None)
    write_json(diff_path or pathlib.Path(nicifications.diff.output_path), result.diff)

    return 0


def _update_object_nicifications_from_source(
    raw_document: JSONObject,
    nicifications: typing.Any,
    /,
    *,
    source_dir: str,
) -> bool:
    schema_index = build_schema_index(source_dir)
    return bool(update_object_nicifications(raw_document, nicifications, schema_index))


def _resolve_backend_source(raw_document: JSONObject, nicifications: typing.Any, /) -> str | None:
    version = _document_version(raw_document)
    return resolve_source_dir(
        version,
        cache_dir=nicifications.remnawave.source.cache_dir,
        repository=nicifications.remnawave.source.repository,
    )


def _document_version(raw_document: JSONObject, /) -> str | None:
    info = raw_document.get("info")

    if isinstance(info, dict):
        version = info.get("version")

        if isinstance(version, str) and version:
            return version

    return None


if __name__ == "__main__":
    parser = ArgumentParser(description="build a nicificated remnawave oas3 schema")
    parser.add_argument("-s", "--source", default=None, help="openapi url or local path to JSON document")
    parser.add_argument(
        "-n",
        "--nicifications",
        type=pathlib.Path,
        default=DEFAULT_SCHEMA_PATH,
        help="path to nicifications YAML/JSON",
    )
    parser.add_argument("-o", "--output", type=pathlib.Path, default=DEFAULT_OUTPUT_PATH, help="out openapi JSON path")
    parser.add_argument("-d", "--diff-output", type=pathlib.Path, default=None, help="out diff report JSON path")
    parser.add_argument(
        "-p",
        "--previous-output",
        type=pathlib.Path,
        default=None,
        help="prev generated openapi JSON used to retain removed elements as deprecated",
    )
    parser.add_argument(
        "-u",
        "--update-nicifications",
        action="store_true",
        default=False,
        help="update nicifications from the source openapi document before building",
    )
    args = parser.parse_args()
    exit(
        nicification_specification(
            source=args.source,
            nicifications_path=args.nicifications,
            output_path=args.output,
            diff_path=args.diff_output,
            previous_output_path=args.previous_output,
            update_nicifications=args.update_nicifications,
        ),
    )
