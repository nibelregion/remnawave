import pathlib
import typing

import msgspec

DEFAULT_SCHEMA_PATH: typing.Final = pathlib.Path(__file__).with_name("nicifications.yaml")
DEFAULT_REMNA_SCHEMA_URL: typing.Final = "https://cdn.remna.st/docs/openapi.json"
DEFAULT_REMNA_SCHEMA_DOCUMENT_TYPE: typing.Final = "json"


class Model(msgspec.Struct, omit_defaults=True):
    pass


class Remnawave(Model):
    errors_schema_url: str = "#/components/responses"
    schema_url: str = DEFAULT_REMNA_SCHEMA_URL
    schema_document_type: typing.Literal["json", "yaml"] = DEFAULT_REMNA_SCHEMA_DOCUMENT_TYPE


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


class NicificatedSchema(Model):
    remnawave: Remnawave
    schema: Schema
    error_responses: ErrorResponses = msgspec.field(default_factory=ErrorResponses)
    deprecations: Deprecations = msgspec.field(default_factory=Deprecations)
    diff: Diff = msgspec.field(default_factory=Diff)


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
        objects.append({object_key: {"name": object_schema.name}})

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

    path.write_bytes(msgspec.yaml.encode(document))


__all__ = ("read_schema", "write_schema")
