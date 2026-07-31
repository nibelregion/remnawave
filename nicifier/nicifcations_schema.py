import pathlib
import typing

import msgspec

DEFAULT_SCHEMA_PATH: typing.Final = pathlib.Path(__file__).with_name("nicifications.yaml")
DEFAULT_REMNA_SCHEMA_URL: typing.Final = "https://cdn.remna.st/docs/openapi.json"
DEFAULT_REMNA_SCHEMA_DOCUMENT_TYPE: typing.Final = "json"
DEFAULT_SOURCE_REPOSITORY: typing.Final = "remnawave/backend"
DEFAULT_SOURCE_CACHE_DIR: typing.Final = str(pathlib.Path(__file__).with_name(".source-cache"))


class Model(msgspec.Struct, omit_defaults=True):
    pass


class Source(Model):
    repository: str = DEFAULT_SOURCE_REPOSITORY
    cache_dir: str = DEFAULT_SOURCE_CACHE_DIR


class Remnawave(Model):
    errors_schema_url: str = "#/components/responses"
    schema_url: str = DEFAULT_REMNA_SCHEMA_URL
    schema_document_type: typing.Literal["json", "yaml"] = DEFAULT_REMNA_SCHEMA_DOCUMENT_TYPE
    source: Source = msgspec.field(default_factory=Source)


class EnumSchema(Model):
    name: str
    members: list[str]
    values: list[str | int | float]
    description: str | None = None
    member_descriptions: dict[str, str] = msgspec.field(default_factory=dict[str, str])


class ErrorSchema(Model):
    name: str
    description: str | None = None


class ObjectSchema(Model):
    name: str
    equivalent_to: str | None = None


class Schema(Model):
    type EnumName = str
    type ErrorName = str
    type ObjectName = str

    enums: dict[EnumName, EnumSchema] = msgspec.field(default_factory=dict[EnumName, EnumSchema])
    errors: dict[ErrorName, ErrorSchema] = msgspec.field(default_factory=dict[ErrorName, ErrorSchema])
    objects: dict[ObjectName, ObjectSchema] = msgspec.field(default_factory=dict[ObjectName, ObjectSchema])


class ErrorResponses(Model):
    enabled: bool = True
    min_occurrences: int = 1


class Deprecations(Model):
    annotate_controllers: bool = True
    annotate_paths: bool = True


class Diff(Model):
    output_path: str = "remnawave.diff.json"


class ResponseContentType(Model):
    """Force a specific media type on a response the backend leaves untyped.

    Some endpoints (e.g. the subscription pages) return rendered `text/html`
    but the generated OpenAPI marks the response as an empty body, so it gets
    collapsed into the shared ``OkResponse``. Declaring the media type here
    keeps the documented content honest.
    """

    path: str
    media_type: str
    status: str = "200"
    type: str = "string"
    description: str | None = None


class NicificatedSchema(Model):
    remnawave: Remnawave
    schema: Schema
    error_responses: ErrorResponses = msgspec.field(default_factory=ErrorResponses)
    deprecations: Deprecations = msgspec.field(default_factory=Deprecations)
    diff: Diff = msgspec.field(default_factory=Diff)
    response_content_types: list[ResponseContentType] = msgspec.field(
        default_factory=list[ResponseContentType],
    )


def read_schema(path: pathlib.Path | None = None, /) -> NicificatedSchema:
    path = path or DEFAULT_SCHEMA_PATH
    raw_bytes = path.read_bytes()

    if path.suffix == ".json":
        raw = msgspec.json.decode(raw_bytes)
    else:
        try:
            raw = msgspec.yaml.decode(raw_bytes)
        except ImportError as exc:
            raise RuntimeError("Reading nicifications.yaml requires PyYAML. Run `uv sync` to install project dependencies.") from exc

    if (
        isinstance(raw, dict)
        and isinstance(raw.get("remnawave"), dict)
        and "schema" in raw["remnawave"]
    ):
        raw["schema"] = raw["remnawave"].pop("schema")

    if isinstance(raw, dict) and isinstance(raw.get("schema"), dict):
        def list_to_dict(schema_list: list[typing.Any]):
            dct: dict[str, typing.Any] = {}

            for item in schema_list:
                if isinstance(item, dict):
                    dct.update(item)

            return dct

        for key, schema_val in raw["schema"].copy().items():
            if key in {"objects", "errors", "enums"} and isinstance(schema_val, list):
                raw["schema"][key] = list_to_dict(schema_val)

    return msgspec.convert(raw, type=NicificatedSchema)


def write_schema(schema: NicificatedSchema, path: pathlib.Path | None = None, /) -> None:
    path = path or DEFAULT_SCHEMA_PATH

    enums: list[dict[str, typing.Any]] = []
    errors: list[dict[str, typing.Any]] = []
    objects: list[dict[str, typing.Any]] = []

    for object_key, object_schema in schema.schema.objects.items():
        object_data: dict[str, typing.Any] = {"name": object_schema.name}
        if object_schema.equivalent_to is not None:
            object_data["equivalent_to"] = object_schema.equivalent_to
        objects.append({object_key: object_data})

    for error_key, error_schema in schema.schema.errors.items():
        error_data: dict[str, typing.Any] = {"name": error_schema.name}

        if error_schema.description is not None:
            error_data["description"] = error_schema.description

        errors.append({error_key: error_data})

    for enum_key, enum_schema in schema.schema.enums.items():
        enum_data: dict[str, typing.Any] = {
            "name": enum_schema.name,
            "members": enum_schema.members,
            "values": enum_schema.values,
        }

        if enum_schema.description is not None:
            enum_data["description"] = enum_schema.description

        if enum_schema.member_descriptions:
            enum_data["member_descriptions"] = enum_schema.member_descriptions

        enums.append({enum_key: enum_data})

    remnawave_data: dict[str, typing.Any] = {
        "errors_schema_url": schema.remnawave.errors_schema_url,
        "schema_document_type": schema.remnawave.schema_document_type,
        "schema": {"objects": objects, "errors": errors, "enums": enums},
    }
    if schema.remnawave.schema_url != DEFAULT_REMNA_SCHEMA_URL:
        remnawave_data["schema_url"] = schema.remnawave.schema_url

    source_data: dict[str, typing.Any] = {}
    if schema.remnawave.source.repository != DEFAULT_SOURCE_REPOSITORY:
        source_data["repository"] = schema.remnawave.source.repository
    if schema.remnawave.source.cache_dir != DEFAULT_SOURCE_CACHE_DIR:
        source_data["cache_dir"] = schema.remnawave.source.cache_dir
    if source_data:
        remnawave_data["source"] = source_data

    document = {
        "remnawave": remnawave_data,
        "error_responses": {
            "enabled": schema.error_responses.enabled,
            "min_occurrences": schema.error_responses.min_occurrences,
        },
        "deprecations": {
            "annotate_controllers": schema.deprecations.annotate_controllers,
            "annotate_paths": schema.deprecations.annotate_paths,
        },
        "diff": {"output_path": schema.diff.output_path},
    }

    if schema.response_content_types:
        document["response_content_types"] = [
            _response_content_type_data(entry) for entry in schema.response_content_types
        ]

    path.write_bytes(msgspec.yaml.encode(document))


def _response_content_type_data(entry: ResponseContentType, /) -> dict[str, typing.Any]:
    data: dict[str, typing.Any] = {"path": entry.path, "media_type": entry.media_type}

    if entry.status != "200":
        data["status"] = entry.status

    if entry.type != "string":
        data["type"] = entry.type

    if entry.description is not None:
        data["description"] = entry.description

    return data


__all__ = ("read_schema", "write_schema")
