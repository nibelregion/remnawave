import pathlib
import typing
import json
from argparse import ArgumentParser
from sys import exit

import msgspec
import wreq.blocking as http
from retcon.openapi.parser import decode_openapi_document

from error_codes import collect_error_codes
from nicifcations_schema import DEFAULT_SCHEMA_PATH, read_schema, write_schema
from nicifier_schema import JSONObject, nicificate_openapi_document, update_enum_nicifications

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
    document_type: typing.Literal["json", "yaml"] | None = None,
    nicifications_path: pathlib.Path = DEFAULT_SCHEMA_PATH,
    output_path: pathlib.Path = DEFAULT_OUTPUT_PATH,
    output_min_path: pathlib.Path = DEFAULT_MIN_OUTPUT_PATH,
    diff_path: pathlib.Path | None = None,
    previous_output_path: pathlib.Path | None = None,
    update_nicifications: bool = False,
    github_token: str | None = None,
) -> int:
    nicifications = read_schema(nicifications_path)
    source = source or nicifications.remnawave.schema_url
    document_type = document_type or nicifications.remnawave.schema_document_type
    payload = read_openapi_source(source)
    raw_document = decode_raw_openapi(payload, document_type)
    error_codes = collect_error_codes(github_token=github_token)
    previous_document: JSONObject | None = None
    previous_output_path = previous_output_path or output_path
    if previous_output_path.exists():
        previous_document = decode_raw_openapi(previous_output_path.read_bytes())

    if update_nicifications:
        enum_updates = update_enum_nicifications(raw_document, nicifications)
        if enum_updates:
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


def build_arg_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Build a nicificated Remnawave OpenAPI schema.")
    parser.add_argument("--source", default=None, help="OpenAPI URL or local path.")
    parser.add_argument(
        "--document-type",
        default=None,
        choices=("json", "yaml"),
        help="Source OpenAPI document type. Defaults to nicifications.remnawave.schema_document_type.",
    )
    parser.add_argument(
        "--github-token",
        default=None,
        help="GitHub token for Code Search; defaults to GITHUB_TOKEN or GH_TOKEN.",
    )
    parser.add_argument(
        "--nicifications",
        type=pathlib.Path,
        default=DEFAULT_SCHEMA_PATH,
        help="Path to nicifications YAML/JSON.",
    )
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT_PATH, help="Output OpenAPI JSON path.")
    parser.add_argument("--diff-output", type=pathlib.Path, default=None, help="Output diff report JSON path.")
    parser.add_argument(
        "--previous-output",
        type=pathlib.Path,
        default=None,
        help="Previous generated OpenAPI JSON used to retain removed elements as deprecated.",
    )
    parser.add_argument(
        "--update-nicifications",
        action="store_true",
        help="Update enum nicifications from the source OpenAPI document before building.",
    )
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    exit(
        nicification_specification(
            source=args.source,
            document_type=args.document_type,
            nicifications_path=args.nicifications,
            output_path=args.output,
            diff_path=args.diff_output,
            previous_output_path=args.previous_output,
            update_nicifications=args.update_nicifications,
            github_token=args.github_token,
        ),
    )
