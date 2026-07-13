from __future__ import annotations

import copy
import hashlib
import json
import re
import typing
from dataclasses import dataclass

from error_codes import ErrorCode
from nicifcations_schema import EnumSchema, NicificatedSchema

type JSON = typing.Any
type JSONObject = dict[str, JSON]

HTTP_METHODS: typing.Final = frozenset(
    (
        "get",
        "put",
        "post",
        "delete",
        "options",
        "head",
        "patch",
        "trace",
    ),
)
COMPONENT_RESPONSE_PREFIX: typing.Final = "#/components/responses/"
ERROR_RESPONSE_NAME_PREFIX: typing.Final = "ErrorResponse"
STATUS_TITLE_OVERRIDES: typing.Final = {
    "400": "BadRequest",
    "401": "Unauthorized",
    "402": "PaymentRequired",
    "403": "Forbidden",
    "404": "NotFound",
    "405": "MethodNotAllowed",
    "406": "NotAcceptable",
    "409": "Conflict",
    "410": "Gone",
    "422": "Validation",
    "429": "TooManyRequests",
    "500": "InternalServer",
    "502": "BadGateway",
    "503": "ServiceUnavailable",
    "504": "GatewayTimeout",
}
NON_ERROR_STATUS_TITLE_OVERRIDES: typing.Final = {
    "200": "Ok",
    "201": "Created",
    "202": "Accepted",
    "203": "NonAuthoritative",
    "204": "NoContent",
    "205": "ResetContent",
    "206": "PartialContent",
    "default": "Ok",
}
WORD_RE: typing.Final = re.compile(r"[0-9A-Za-z]+")
SCHEMA_SIGNATURE_IGNORED_KEYS: typing.Final = frozenset(
    (
        "$comment",
        "description",
        "example",
        "examples",
        "externalDocs",
        "summary",
        "title",
    )
)
ENUM_SCHEMA_REF_PREFIX: typing.Final = "#/components/schemas/"
ERROR_CODE_ENUM_NAME: typing.Final = "ErrorCode"
ENUM_REF_IGNORED_KEYS: typing.Final = frozenset(
    (
        "enum",
        "type",
        "x-enum-descriptions",
        "x-enum-varnames",
        "x-enumNames",
    )
)


class NicificationResult(typing.NamedTuple):
    document: JSONObject
    diff: JSONObject


@dataclass(slots=True)
class InlineObjectCandidate:
    container: JSONObject | list[JSON]
    key: str | int
    schema: JSONObject
    pointer: str
    field_name: str
    parent_model_name: str
    short_name: str
    contextual_name: str
    prefer_contextual_name: bool = False


@dataclass(slots=True)
class EnumOccurrence:
    container: JSONObject | list[JSON]
    key: str | int
    schema: JSONObject
    pointer: str
    values: tuple[str | int | float, ...]
    field_name: str
    parent_model_name: str | None


def nicificate_openapi_document(
    document: JSONObject,
    nicifications: NicificatedSchema,
    /,
    *,
    previous_document: JSONObject | None = None,
    error_codes: typing.Sequence[ErrorCode] = (),
) -> NicificationResult:
    improved = copy.deepcopy(document)
    before_deprecated = collect_deprecations(document)

    enum_changes = apply_enum_nicifications(improved, nicifications)

    response_component_changes: list[JSONObject] = []
    if nicifications.error_responses.enabled:
        response_component_changes = hoist_response_components(
            improved,
            nicificated_schema=nicifications,
            min_occurrences=max(1, nicifications.error_responses.min_occurrences),
        )
        response_component_changes.extend(
            rename_referenced_response_components(improved, nicificated_schema=nicifications)
        )

    inlined_map_object_components = inline_map_object_schema_components(improved)
    inline_object_changes = hoist_inline_objects(improved, nicifications)
    restored_deprecations = restore_removed_elements(improved, previous_document) if previous_document else []
    deprecated_annotations = apply_deprecation_annotations(improved, nicifications)
    deprecated_annotations = [*restored_deprecations, *deprecated_annotations]
    error_code_changes = apply_error_code_enum(improved, error_codes)
    after_deprecated = collect_deprecations(improved)
    hoisted_response_components = [
        change for change in response_component_changes if change.get("action") == "hoisted"
    ]
    renamed_response_components = [
        change for change in response_component_changes if change.get("action") == "renamed"
    ]
    hoisted_error_responses = [
        change for change in hoisted_response_components if change.get("is_error") is True
    ]
    hoisted_inline_objects = [
        change for change in inline_object_changes if change.get("action") == "hoisted"
    ]
    reused_inline_objects = [
        change for change in inline_object_changes if change.get("action") == "reused"
    ]

    diff: JSONObject = {  # type: ignore
        "summary": {
            "deprecated_before": len(before_deprecated),
            "deprecated_after": len(after_deprecated),
            "enum_updates": len(enum_changes),
            "error_code_updates": len(error_code_changes),
            "hoisted_error_responses": len(hoisted_error_responses),
            "hoisted_responses": len(hoisted_response_components),
            "renamed_responses": len(renamed_response_components),
            "inlined_map_object_components": len(inlined_map_object_components),
            "hoisted_inline_objects": len(hoisted_inline_objects),
            "reused_inline_objects": len(reused_inline_objects),
            "deprecation_annotations": len(deprecated_annotations),
        },
        "deprecated": {
            "before": before_deprecated,
            "after": after_deprecated,
            "added": sorted(_diff_items(after_deprecated, before_deprecated), key=_diff_item_sort_key),
            "removed": sorted(_diff_items(before_deprecated, after_deprecated), key=_diff_item_sort_key),
        },
        "enums": enum_changes,
        "error_codes": error_code_changes,
        "error_responses": hoisted_error_responses,
        "responses": response_component_changes,
        "map_object_components": inlined_map_object_components,
        "inline_objects": inline_object_changes,
        "deprecation_annotations": deprecated_annotations,
    }
    return NicificationResult(document=improved, diff=diff)


def apply_error_code_enum(document: JSONObject, error_codes: typing.Sequence[ErrorCode], /) -> list[JSONObject]:
    if not error_codes:
        return []

    unique_codes: dict[str, ErrorCode] = {}
    used_members: set[str] = set()

    for error_code in sorted(error_codes, key=lambda item: (_error_code_sort_key(item.value), item.member)):
        if error_code.value in unique_codes:
            continue

        member = _unique_enum_member_name(error_code.member, used_members)
        unique_codes[error_code.value] = ErrorCode(
            member=member,
            value=error_code.value,
            description=error_code.description,
        )
        used_members.add(member)

    ordered_codes = sorted(unique_codes.values(), key=lambda error_code: _error_code_sort_key(error_code.value))
    enum_schema = EnumSchema(
        name=ERROR_CODE_ENUM_NAME,
        members=[error_code.member for error_code in ordered_codes],
        values=[error_code.value for error_code in ordered_codes],
        description="Error code of the API.",
        member_descriptions={
            error_code.member: error_code.description
            for error_code in ordered_codes
            if error_code.description is not None
        },
    )
    schemas = _ensure_component_schemas(document)
    before = copy.deepcopy(schemas.get(ERROR_CODE_ENUM_NAME))
    schemas[ERROR_CODE_ENUM_NAME] = _enum_component_schema(enum_schema)

    changes: list[JSONObject] = []
    if before != schemas[ERROR_CODE_ENUM_NAME]:
        changes.append(
            {
                "action": "component",
                "schema": ERROR_CODE_ENUM_NAME,
                "members": typing.cast(JSON, enum_schema.members),
                "values": typing.cast(JSON, enum_schema.values),
            }
        )

    for pointer, schema in list(_string_error_code_fields(document, skip_component=ERROR_CODE_ENUM_NAME)):
        if schema == {"$ref": f"{ENUM_SCHEMA_REF_PREFIX}{ERROR_CODE_ENUM_NAME}"}:
            continue

        schema.clear()
        schema["$ref"] = f"{ENUM_SCHEMA_REF_PREFIX}{ERROR_CODE_ENUM_NAME}"
        changes.append({"action": "referenced", "pointer": pointer, "ref": schema["$ref"]})

    return changes


def apply_enum_nicifications(
    document: JSONObject,
    nicifications: NicificatedSchema,
    /,
) -> list[JSONObject]:
    schemas = _ensure_component_schemas(document)
    changes: list[JSONObject] = []

    for enum_key, enum_schema in nicifications.schema.enums.items():
        before = copy.deepcopy(schemas.get(enum_schema.name))
        schema = _enum_component_schema(enum_schema)
        schemas[enum_schema.name] = schema

        if before != schema:
            changes.append(
                {
                    "action": "component",
                    "schema": enum_schema.name,
                    "nicification": enum_key,
                    "members": typing.cast(JSON, enum_schema.members),
                    "values": typing.cast(JSON, enum_schema.values),
                }
            )

    enum_component_names = {enum_schema.name for enum_schema in nicifications.schema.enums.values()}

    for occurrence in _collect_enum_occurrences(document):
        if _is_enum_component_schema_pointer(occurrence.pointer, enum_component_names):
            continue

        match = _find_enum_nicification_match(occurrence.values, occurrence, nicifications)
        if match is None:
            continue

        enum_key, enum_schema = match
        replacement = _enum_ref_schema(occurrence.schema, occurrence.values, enum_schema)
        if replacement == occurrence.schema:
            continue

        _replace_container_child(occurrence.container, occurrence.key, replacement)
        enum_ref = f"{ENUM_SCHEMA_REF_PREFIX}{enum_schema.name}"
        changes.append(
            {
                "action": "referenced" if _is_full_enum_match(occurrence.values, enum_schema) else "variant_referenced",
                "schema": enum_schema.name,
                "nicification": enum_key,
                "source": occurrence.pointer,
                "ref": enum_ref,
                "values": typing.cast(JSON, list(occurrence.values)),
            }
        )

    return changes


def update_enum_nicifications(
    document: JSONObject,
    nicifications: NicificatedSchema,
    /,
) -> list[JSONObject]:
    changes: list[JSONObject] = []
    grouped: dict[tuple[str | int | float, ...], list[EnumOccurrence]] = {}

    for occurrence in _collect_enum_occurrences(document):
        grouped.setdefault(occurrence.values, []).append(occurrence)

    for values, occurrences in sorted(grouped.items(), key=lambda item: (len(item[0]), _canonical_json(list(item[0])))):
        occurrence = occurrences[0]
        match = _find_enum_nicification_match(values, occurrence, nicifications, allow_new_values=True)
        if match is not None:
            enum_key, enum_schema = match
            added_values = _append_enum_values(enum_schema, values, occurrence.schema)
            if added_values:
                changes.append(
                    {
                        "action": "updated",
                        "nicification": enum_key,
                        "name": enum_schema.name,
                        "added_values": typing.cast(JSON, added_values),
                    }
                )
            continue

        if len(values) <= 1:
            continue

        enum_key, enum_name, description = _new_enum_nicification_identity(values, occurrence)
        if enum_key in nicifications.schema.enums:
            enum_schema = nicifications.schema.enums[enum_key]
            added_values = _append_enum_values(enum_schema, values, occurrence.schema)

            if added_values:
                changes.append(
                    {
                        "action": "updated",
                        "nicification": enum_key,
                        "name": enum_schema.name,
                        "added_values": typing.cast(JSON, added_values),
                    }
                )
            continue

        enum_schema = EnumSchema(
            name=enum_name,
            members=_enum_member_names(occurrence.schema, values),
            values=list(values),
            description=description,
        )
        nicifications.schema.enums[enum_key] = enum_schema
        changes.append(
            {
                "action": "added",
                "nicification": enum_key,
                "name": enum_schema.name,
                "values": typing.cast(JSON, list(values)),
            }
        )

    return changes


def hoist_error_responses(
    document: JSONObject,
    /,
    *,
    nicificated_schema: NicificatedSchema,
    min_occurrences: int,
) -> list[JSONObject]:
    return hoist_response_components(
        document,
        nicificated_schema=nicificated_schema,
        min_occurrences=min_occurrences,
        include_non_error=False,
    )


def hoist_response_components(
    document: JSONObject,
    /,
    *,
    nicificated_schema: NicificatedSchema,
    min_occurrences: int,
    include_non_error: bool = True,
) -> list[JSONObject]:
    groups: dict[str, list[tuple[JSONObject, str, JSONObject, str]]] = {}

    for path, method, operation in iter_path_operations(document):
        responses = operation.get("responses")
        if not isinstance(responses, dict):
            continue

        for status_code, raw_response in responses.items():
            status_code = str(status_code)
            if not isinstance(raw_response, dict):
                continue

            if not include_non_error and not _is_error_status(status_code):
                continue

            if _response_ref(raw_response):
                continue

            signature = _response_signature(raw_response)
            location = f"#/paths/{_json_pointer_escape(path)}/{method}/responses/{_json_pointer_escape(status_code)}"
            groups.setdefault(signature, []).append((responses, status_code, raw_response, location))

    components = _ensure_components(document)
    component_responses = components.setdefault("responses", {})

    if not isinstance(component_responses, dict):
        component_responses = {}
        components["responses"] = component_responses

    hoisted: list[JSONObject] = []
    used_names = {str(name) for name in component_responses}
    used_nicification_names: set[str] = set()

    for signature, occurrences in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(occurrences) < min_occurrences:
            continue

        first_status = occurrences[0][1]
        first_response = copy.deepcopy(occurrences[0][2])
        raw_response_name = _unique_name(_raw_response_name(first_status, first_response), used_nicification_names)
        used_nicification_names.add(raw_response_name)
        is_error = _is_error_status(first_status)
        response_name = _unique_response_name(raw_response_name, used_names, nicificated_schema, is_error=is_error)
        used_names.add(response_name)
        component_responses[response_name] = typing.cast(JSON, first_response)

        locations: list[str] = []
        for responses, status_code, _, location in occurrences:
            responses[status_code] = {"$ref": f"{COMPONENT_RESPONSE_PREFIX}{response_name}"}
            locations.append(location)

        hoisted.append(
            {
                "action": "hoisted",
                "name": response_name,
                "nicification": raw_response_name,
                "status_code": first_status,
                "is_error": is_error,
                "occurrences": len(occurrences),
                "locations": locations,
                "signature": signature,
            }
        )

    return hoisted


def rename_referenced_response_components(
    document: JSONObject,
    /,
    *,
    nicificated_schema: NicificatedSchema,
) -> list[JSONObject]:
    components = document.get("components")
    if not isinstance(components, dict):
        return []

    component_responses = components.get("responses")
    if not isinstance(component_responses, dict):
        return []

    references: dict[str, list[tuple[JSONObject, str, dict[str, typing.Any], str]]] = {}
    for path, method, operation in iter_path_operations(document):
        responses = operation.get("responses")
        if not isinstance(responses, dict):
            continue

        for status_code, response in responses.items():
            if not isinstance(response, dict):
                continue

            component_name = _response_component_ref_name(_response_ref(response))
            if component_name is None:
                continue

            location = f"#/paths/{_json_pointer_escape(path)}/{method}/responses/{_json_pointer_escape(str(status_code))}"
            references.setdefault(component_name, []).append((responses, str(status_code), response, location))

    used_names = {str(name) for name in component_responses}
    used_nicification_names: set[str] = set()
    renamed: list[JSONObject] = []

    for old_name, occurrences in sorted(references.items(), key=lambda item: item[0]):
        if old_name not in component_responses:
            continue

        if any(_is_error_status(status_code) for _, status_code, _, _ in occurrences):
            continue

        component_response = component_responses.get(old_name)
        if not isinstance(component_response, dict):
            continue

        first_status = occurrences[0][1]
        raw_response_name = _unique_name(_raw_response_name(first_status, component_response), used_nicification_names)
        used_nicification_names.add(raw_response_name)
        if raw_response_name == old_name:
            continue

        used_names.discard(old_name)
        new_name = _unique_response_name(raw_response_name, used_names, nicificated_schema, is_error=False)
        used_names.add(new_name)
        if new_name == old_name:
            continue

        component_responses[new_name] = component_responses.pop(old_name)
        locations: list[str] = []

        for _, _, response, location in occurrences:
            response["$ref"] = f"{COMPONENT_RESPONSE_PREFIX}{new_name}"
            locations.append(location)

        renamed.append(
            {
                "action": "renamed",
                "name": new_name,
                "old_name": old_name,
                "nicification": raw_response_name,
                "status_code": first_status,
                "is_error": False,
                "occurrences": len(occurrences),
                "locations": locations,
            }
        )

    return renamed


def hoist_duplicate_error_responses(
    document: JSONObject,
    /,
    *,
    nicificated_schema: NicificatedSchema,
    min_occurrences: int,
) -> list[JSONObject]:
    return hoist_error_responses(
        document,
        nicificated_schema=nicificated_schema,
        min_occurrences=min_occurrences,
    )


def inline_map_object_schema_components(document: JSONObject, /) -> list[JSONObject]:
    components = document.get("components")
    if not isinstance(components, dict):
        return []

    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return []

    map_schemas = {
        str(name): schema
        for name, schema in list(schemas.items())
        if isinstance(schema, dict) and _is_map_object_schema(schema)
    }
    if not map_schemas:
        return []

    replacements: dict[str, list[str]] = {name: [] for name in map_schemas}

    def replace_refs(value: JSON, pointer: str) -> JSON:
        if isinstance(value, dict):
            ref_name = _schema_component_ref_name(_schema_ref(value))
            if ref_name in map_schemas:
                replacement = copy.deepcopy(map_schemas[ref_name])
                for key, sibling in value.items():
                    if key != "$ref":
                        replacement[key] = copy.deepcopy(sibling)

                replacements[ref_name].append(pointer)
                return replacement

            for key, child in list(value.items()):
                value[key] = replace_refs(child, f"{pointer}/{_json_pointer_escape(str(key))}")
            return value

        if isinstance(value, list):
            for index, child in enumerate(list(value)):
                value[index] = replace_refs(child, f"{pointer}/{index}")
            return value

        return value

    replace_refs(document, "#")

    inlined: list[JSONObject] = []
    for name, locations in sorted(replacements.items()):
        if not locations:
            continue

        schema = map_schemas[name]
        if name in schemas:
            del schemas[name]

        inlined.append(
            {
                "name": name,
                "locations": locations,
                "schema": copy.deepcopy(schema),
            }
        )

    return inlined


def hoist_inline_objects(
    document: JSONObject,
    nicificated_schema: NicificatedSchema,
    /,
) -> list[JSONObject]:
    components = _ensure_components(document)
    schemas = components.setdefault("schemas", {})
    if not isinstance(schemas, dict):
        schemas = {}
        components["schemas"] = schemas

    candidates = _collect_inline_object_candidates(document, schemas)
    short_name_counts: dict[str, int] = {}
    for candidate in candidates:
        short_name_counts[candidate.short_name] = short_name_counts.get(candidate.short_name, 0) + 1

    used_names = {str(name) for name in schemas}
    used_nicification_names = set(used_names)
    assigned_raw_names: dict[int, str] = {}
    assigned_model_names: dict[int, str] = {}
    assigned_parent_names: dict[int, str] = {}
    schema_signatures: dict[str, str] = {}
    hoisted: list[JSONObject] = []

    def assign_candidate_name(candidate: InlineObjectCandidate) -> tuple[str, str, str]:
        candidate_key = id(candidate)
        if candidate_key in assigned_model_names:
            return (
                assigned_raw_names[candidate_key],
                assigned_model_names[candidate_key],
                assigned_parent_names[candidate_key],
            )

        ancestor = _nearest_inline_object_ancestor(candidate, candidates)
        parent_model_name = candidate.parent_model_name
        if ancestor is not None:
            _, parent_model_name, _ = assign_candidate_name(ancestor)

        raw_base_name = (
            candidate.contextual_name
            if candidate.prefer_contextual_name or short_name_counts[candidate.short_name] > 1
            else candidate.short_name
        )
        raw_model_name = _unique_name(raw_base_name, used_nicification_names)
        used_nicification_names.add(raw_model_name)

        configured_name = _object_nicification_name(raw_model_name, nicificated_schema)
        model_base_name = configured_name
        if model_base_name is None:
            model_base_name = (
                _contextual_schema_model_name(parent_model_name, candidate.field_name)
                if ancestor is not None
                else raw_model_name
            )

        model_name = _unique_name(model_base_name, used_names)
        used_names.add(model_name)
        assigned_raw_names[candidate_key] = raw_model_name
        assigned_model_names[candidate_key] = model_name
        assigned_parent_names[candidate_key] = parent_model_name
        return raw_model_name, model_name, parent_model_name

    for candidate in candidates:
        assign_candidate_name(candidate)

    for candidate in sorted(candidates, key=lambda item: item.pointer.count("/"), reverse=True):
        raw_model_name, model_name, parent_model_name = assign_candidate_name(candidate)
        schema = copy.deepcopy(candidate.schema)
        signature = _schema_signature(schema)
        reused_model_name = schema_signatures.get(signature)

        if reused_model_name is None:
            schemas[model_name] = schema
            schema_signatures[signature] = model_name
            target_model_name = model_name
            action = "hoisted"
        else:
            used_names.discard(model_name)
            target_model_name = reused_model_name
            action = "reused"

        _replace_container_child(candidate.container, candidate.key, {"$ref": f"#/components/schemas/{target_model_name}"})
        hoisted.append(
            {
                "action": action,
                "name": model_name,
                "target": target_model_name,
                "nicification": raw_model_name,
                "field": candidate.field_name,
                "parent": parent_model_name,
                "source": candidate.pointer,
                "signature": signature,
            }
        )

    return sorted(hoisted, key=lambda item: typing.cast(str, item["source"]))


def _collect_inline_object_candidates(
    document: JSONObject,
    schemas: JSONObject,
    /,
) -> list[InlineObjectCandidate]:
    candidates: list[InlineObjectCandidate] = []

    for model_name, schema in list(schemas.items()):
        if not isinstance(schema, dict):
            continue

        _collect_inline_object_candidates_from_schema(
            schema,
            pointer=f"#/components/schemas/{model_name}",
            parent_model_name=str(model_name),
            field_name=str(model_name),
            container=None,
            container_key=None,
            candidates=candidates,
        )

    _collect_inline_object_candidates_from_component_responses(document, candidates)
    _collect_inline_object_candidates_from_component_request_bodies(document, candidates)
    _collect_inline_object_candidates_from_paths(document, candidates)
    return candidates


def _nearest_inline_object_ancestor(
    candidate: InlineObjectCandidate,
    candidates: list[InlineObjectCandidate],
    /,
) -> InlineObjectCandidate | None:
    ancestor: InlineObjectCandidate | None = None

    for other in candidates:
        if other is candidate:
            continue

        other_pointer = other.pointer
        if not candidate.pointer.startswith(f"{other_pointer}/"):
            continue

        if ancestor is None or other_pointer.count("/") > ancestor.pointer.count("/"):
            ancestor = other

    return ancestor


def _collect_inline_object_candidates_from_component_responses(
    document: JSONObject,
    candidates: list[InlineObjectCandidate],
    /,
) -> None:
    components = document.get("components")
    if not isinstance(components, dict):
        return

    responses = components.get("responses")
    if not isinstance(responses, dict):
        return

    for response_name, response in list(responses.items()):
        if isinstance(response, dict):
            _collect_inline_object_candidates_from_content(
                response.get("content"),
                pointer=f"#/components/responses/{_json_pointer_escape(str(response_name))}/content",
                parent_model_name=str(response_name),
                candidates=candidates,
            )


def _collect_inline_object_candidates_from_component_request_bodies(
    document: JSONObject,
    candidates: list[InlineObjectCandidate],
    /,
) -> None:
    components = document.get("components")
    if not isinstance(components, dict):
        return

    request_bodies = components.get("requestBodies")
    if not isinstance(request_bodies, dict):
        return

    for request_body_name, request_body in list(request_bodies.items()):
        if isinstance(request_body, dict):
            _collect_inline_object_candidates_from_content(
                request_body.get("content"),
                pointer=f"#/components/requestBodies/{_json_pointer_escape(str(request_body_name))}/content",
                parent_model_name=str(request_body_name),
                candidates=candidates,
            )


def _collect_inline_object_candidates_from_paths(
    document: JSONObject,
    candidates: list[InlineObjectCandidate],
    /,
) -> None:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return

    for path, path_item in list(paths.items()):
        if not isinstance(path_item, dict):
            continue

        for method, operation in list(path_item.items()):
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue

            operation_name = _operation_schema_context_name(operation, method, str(path))
            request_body = operation.get("requestBody")
            if isinstance(request_body, dict):
                _collect_inline_object_candidates_from_content(
                    request_body.get("content"),
                    pointer=f"#/paths/{_json_pointer_escape(str(path))}/{method}/requestBody/content",
                    parent_model_name=f"{operation_name}Request",
                    candidates=candidates,
                )

            responses = operation.get("responses")
            if isinstance(responses, dict):
                for status_code, response in list(responses.items()):
                    if isinstance(response, dict):
                        _collect_inline_object_candidates_from_content(
                            response.get("content"),
                            pointer=f"#/paths/{_json_pointer_escape(str(path))}/{method}/responses/{_json_pointer_escape(str(status_code))}/content",
                            parent_model_name=f"{operation_name}{_to_pascal_case(str(status_code))}Response",
                            candidates=candidates,
                        )


def _collect_inline_object_candidates_from_content(
    content: JSON,
    /,
    *,
    pointer: str,
    parent_model_name: str,
    candidates: list[InlineObjectCandidate],
) -> None:
    if not isinstance(content, dict):
        return

    for content_type, media in list(content.items()):
        if not isinstance(media, dict):
            continue

        schema = media.get("schema")
        if isinstance(schema, dict) and not _schema_ref(schema):
            _collect_inline_object_candidates_from_schema(
                schema,
                pointer=f"{pointer}/{_json_pointer_escape(str(content_type))}/schema",
                parent_model_name=parent_model_name,
                field_name="body",
                container=media,
                container_key="schema",
                candidates=candidates,
                prefer_contextual_name=True,
            )


def _collect_inline_object_candidates_from_schema(
    schema: JSONObject,
    /,
    *,
    pointer: str,
    parent_model_name: str,
    field_name: str,
    container: JSONObject | None,
    container_key: str | int | None,
    candidates: list[InlineObjectCandidate],
    prefer_contextual_name: bool = False,
) -> None:
    if _is_inline_object_schema(schema) and container is not None and container_key is not None:
        short_name = _schema_model_name_from_field(field_name)
        candidates.append(
            InlineObjectCandidate(
                container=container,
                key=container_key,
                schema=schema,
                pointer=pointer,
                field_name=field_name,
                parent_model_name=parent_model_name,
                short_name=short_name,
                contextual_name=_contextual_schema_model_name(parent_model_name, field_name),
                prefer_contextual_name=prefer_contextual_name,
            )
        )

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for property_name, property_schema in list(properties.items()):
            if isinstance(property_schema, dict) and not _schema_ref(property_schema):
                _collect_inline_object_candidates_from_schema(
                    property_schema,
                    pointer=f"{pointer}/properties/{_json_pointer_escape(str(property_name))}",
                    parent_model_name=parent_model_name,
                    field_name=str(property_name),
                    container=properties,
                    container_key=str(property_name),
                    candidates=candidates,
                )

    items = schema.get("items")
    if isinstance(items, dict) and not _schema_ref(items):
        _collect_inline_object_candidates_from_schema(
            items,
            pointer=f"{pointer}/items",
            parent_model_name=parent_model_name,
            field_name=field_name,
            container=schema,
            container_key="items",
            candidates=candidates,
        )

    additional_properties = schema.get("additionalProperties")
    if isinstance(additional_properties, dict) and not _schema_ref(additional_properties):
        _collect_inline_object_candidates_from_schema(
            additional_properties,
            pointer=f"{pointer}/additionalProperties",
            parent_model_name=parent_model_name,
            field_name=field_name,
            container=schema,
            container_key="additionalProperties",
            candidates=candidates,
        )

    for keyword in ("oneOf", "anyOf", "allOf"):
        variants = schema.get(keyword)
        if not isinstance(variants, list):
            continue

        for index, variant in enumerate(list(variants)):
            if isinstance(variant, dict) and not _schema_ref(variant):
                _collect_inline_object_candidates_from_schema(
                    variant,
                    pointer=f"{pointer}/{keyword}/{index}",
                    parent_model_name=parent_model_name,
                    field_name=field_name,
                    container=typing.cast("JSONObject", variants),
                    container_key=index,
                    candidates=candidates,
                )


def apply_deprecation_annotations(
    document: JSONObject,
    nicifications: NicificatedSchema,
    /,
) -> list[JSONObject]:
    annotations: list[JSONObject] = []

    if nicifications.deprecations.annotate_controllers:
        for tag in _deprecated_controller_tags(document):
            tags = document.setdefault("tags", [])
            if not isinstance(tags, list):
                tags = []
                document["tags"] = tags

            tag_obj = _find_tag_object(tags, tag)
            if tag_obj is None:
                tag_obj = typing.cast("JSON", {"name": tag})
                tags.append(tag_obj)

            if tag_obj.get("x-deprecated") is not True:
                tag_obj["x-deprecated"] = True
                annotations.append({"kind": "controller", "name": tag})

    if nicifications.deprecations.annotate_paths:
        paths = document.get("paths")
        if isinstance(paths, dict):
            for path, path_item in paths.items():
                if not isinstance(path_item, dict):
                    continue

                operations = [
                    operation
                    for method, operation in path_item.items()
                    if method in HTTP_METHODS and isinstance(operation, dict)
                ]
                if operations and all(operation.get("deprecated") is True for operation in operations):
                    if path_item.get("x-deprecated") is not True:
                        path_item["x-deprecated"] = True
                        annotations.append({"kind": "path", "path": path})

    return annotations


def restore_removed_elements(
    document: JSONObject,
    previous_document: JSONObject,
    /,
) -> list[JSONObject]:
    """Keep removed API surface from the previous generated schema as deprecated."""
    annotations: list[JSONObject] = []
    current_paths = document.get("paths")
    previous_paths = previous_document.get("paths")

    if not isinstance(current_paths, dict):
        current_paths = {}
        document["paths"] = current_paths

    if isinstance(previous_paths, dict):
        current_operation_ids = {
            operation_id
            for _, _, operation in iter_path_operations(document)
            if (operation_id := _operation_id(operation)) is not None
        }

        for path, previous_path_item in previous_paths.items():
            if not isinstance(previous_path_item, dict):
                continue

            path_item = current_paths.get(path)
            if not isinstance(path_item, dict):
                path_item = copy.deepcopy(previous_path_item)
                current_paths[path] = path_item
                path_restored = False

                for method, operation in _path_item_operations(path_item):
                    operation_id = _operation_id(operation)
                    if operation_id is not None and operation_id in current_operation_ids:
                        del path_item[method]
                        continue

                    _mark_deprecated(operation)
                    annotations.append(_operation_annotation(path, method, operation))
                    path_restored = True

                if path_restored:
                    _mark_path_item_parameters_deprecated(path_item, previous_document)
                    path_item["x-deprecated"] = True
                    annotations.append({"kind": "path", "path": path})
                elif not _path_item_operations(path_item):
                    del current_paths[path]
                continue

            for method, previous_operation in _path_item_operations(previous_path_item):
                operation_id = _operation_id(previous_operation)
                if operation_id is not None and operation_id in current_operation_ids:
                    continue
                if method in path_item:
                    continue

                restored_operation = copy.deepcopy(previous_operation)
                _mark_deprecated(restored_operation)
                path_item[method] = restored_operation
                annotations.append(_operation_annotation(path, method, restored_operation))

            _restore_removed_path_parameters(
                path,
                path_item,
                previous_path_item,
                document,
                previous_document,
                annotations,
            )

    _restore_removed_operation_parameters(document, previous_document, annotations)
    _restore_removed_component_schemas(document, previous_document, annotations)
    _restore_removed_parameter_components(document, previous_document, annotations)
    _restore_missing_component_references(document, previous_document)
    return annotations


def _restore_removed_path_parameters(
    path: str,
    path_item: JSONObject,
    previous_path_item: JSONObject,
    document: JSONObject,
    previous_document: JSONObject,
    annotations: list[JSONObject],
    /,
) -> None:
    _restore_removed_parameters(
        path_item,
        previous_path_item,
        document=document,
        previous_document=previous_document,
        annotation_context={"kind": "parameter", "path": path, "method": None},
        annotations=annotations,
    )


def _mark_path_item_parameters_deprecated(path_item: JSONObject, document: JSONObject, /) -> None:
    parameters = path_item.get("parameters")
    if not isinstance(parameters, list):
        return

    for index, parameter in enumerate(parameters):
        materialized = _materialize_parameter(parameter, document)
        if materialized is None:
            continue
        _mark_deprecated(materialized)
        parameters[index] = materialized


def _restore_removed_operation_parameters(
    document: JSONObject,
    previous_document: JSONObject,
    annotations: list[JSONObject],
    /,
) -> None:
    previous_operations = {
        operation_id: (path, method, operation)
        for path, method, operation in iter_path_operations(previous_document)
        if (operation_id := _operation_id(operation)) is not None
    }

    for path, method, operation in iter_path_operations(document):
        operation_id = _operation_id(operation)
        if operation_id is None or operation_id not in previous_operations:
            continue

        _, _, previous_operation = previous_operations[operation_id]
        _restore_removed_parameters(
            operation,
            previous_operation,
            document=document,
            previous_document=previous_document,
            annotation_context={"kind": "parameter", "path": path, "method": method},
            annotations=annotations,
        )


def _restore_removed_parameters(
    target: JSONObject,
    previous: JSONObject,
    /,
    *,
    document: JSONObject,
    previous_document: JSONObject,
    annotation_context: JSONObject,
    annotations: list[JSONObject],
) -> None:
    previous_parameters = previous.get("parameters")
    if not isinstance(previous_parameters, list):
        return

    parameters = target.get("parameters")
    if not isinstance(parameters, list):
        parameters = []
        target["parameters"] = parameters

    current_parameter_keys = {
        parameter_key
        for parameter in parameters
        if (parameter_key := _parameter_identity(parameter, document)) is not None
    }
    for index, previous_parameter in enumerate(previous_parameters):
        parameter_key = _parameter_identity(previous_parameter, previous_document)
        if parameter_key is not None and parameter_key in current_parameter_keys:
            continue

        restored_parameter = _materialize_parameter(previous_parameter, previous_document)
        if restored_parameter is None:
            continue

        _mark_deprecated(restored_parameter)
        parameters.append(restored_parameter)
        if parameter_key is not None:
            current_parameter_keys.add(parameter_key)
        annotations.append(
            {
                **annotation_context,
                "location": _json_str(restored_parameter.get("in")),
                "name": _json_str(restored_parameter.get("name")) or f"parameter[{index}]",
            }
        )


def _restore_removed_component_schemas(
    document: JSONObject,
    previous_document: JSONObject,
    annotations: list[JSONObject],
    /,
) -> None:
    schemas = _ensure_component_schemas(document)
    previous_schemas = _component_schemas(previous_document)

    for schema_name, previous_schema in previous_schemas.items():
        schema = schemas.get(schema_name)
        if schema is None:
            schemas[schema_name] = _deprecated_schema_copy(previous_schema)
            annotations.append({"kind": "schema", "schema": schema_name})
            continue

        _restore_removed_schema_properties(schema, previous_schema, schema_name, annotations)


def _restore_removed_schema_properties(
    schema: JSONObject,
    previous_schema: JSONObject,
    schema_name: str,
    annotations: list[JSONObject],
    /,
) -> None:
    properties = schema.get("properties")
    previous_properties = previous_schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(previous_properties, dict):
        return

    for field_name, previous_field in previous_properties.items():
        field = properties.get(field_name)
        if not isinstance(field, dict):
            if not isinstance(previous_field, dict):
                continue
            properties[field_name] = _deprecated_schema_copy(previous_field)
            annotations.append({"kind": "field", "schema": schema_name, "field": field_name})
            continue

        if isinstance(previous_field, dict):
            _restore_removed_schema_properties(field, previous_field, schema_name, annotations)


def _restore_removed_parameter_components(
    document: JSONObject,
    previous_document: JSONObject,
    annotations: list[JSONObject],
    /,
) -> None:
    previous_parameters = _component_parameters(previous_document, create=False)
    if not previous_parameters:
        return

    parameters = _component_parameters(document)
    for parameter_name, previous_parameter in previous_parameters.items():
        if parameter_name in parameters:
            continue

        parameters[parameter_name] = _deprecated_schema_copy(previous_parameter)
        annotations.append({"kind": "parameter_component", "parameter": parameter_name})


def _restore_missing_component_references(document: JSONObject, previous_document: JSONObject, /) -> None:
    """Restore dependencies of retained deprecated operations without adding unrelated components."""
    components = document.get("components")
    previous_components = previous_document.get("components")
    if not isinstance(components, dict) or not isinstance(previous_components, dict):
        return

    pending = list(_iter_component_references(document))
    restored: set[tuple[str, str]] = set()
    while pending:
        section, name = pending.pop()
        key = (section, name)
        if key in restored:
            continue
        restored.add(key)

        current_section = components.get(section)
        previous_section = previous_components.get(section)
        if not isinstance(previous_section, dict):
            continue
        if isinstance(current_section, dict) and name in current_section:
            continue

        previous_component = previous_section.get(name)
        if not isinstance(previous_component, dict):
            continue
        if not isinstance(current_section, dict):
            current_section = {}
            components[section] = current_section

        restored_component = copy.deepcopy(previous_component)
        current_section[name] = restored_component
        pending.extend(_iter_component_references(restored_component))


def _iter_component_references(value: JSON, /) -> typing.Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        ref = _json_str(value.get("$ref"))
        if ref is not None and ref.startswith("#/components/"):
            parts = ref.removeprefix("#/components/").split("/", maxsplit=1)
            if len(parts) == 2 and parts[0] and parts[1]:
                yield (parts[0], parts[1])
        for child in value.values():
            yield from _iter_component_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_component_references(child)


def _path_item_operations(path_item: JSONObject, /) -> typing.Iterator[tuple[str, JSONObject]]:
    for method, operation in list(path_item.items()):
        if method in HTTP_METHODS and isinstance(operation, dict):
            yield method, operation


def _operation_id(operation: JSONObject, /) -> str | None:
    operation_id = operation.get("operationId")
    return operation_id if isinstance(operation_id, str) and "_" in operation_id else None


def _operation_annotation(path: str, method: str, operation: JSONObject, /) -> JSONObject:
    return {
        "kind": "operation",
        "path": path,
        "method": method,
        "operation_id": _json_str(operation.get("operationId")),
    }


def _mark_deprecated(value: JSONObject, /) -> None:
    value["deprecated"] = True


def _deprecated_schema_copy(schema: JSONObject, /) -> JSONObject:
    copied = copy.deepcopy(schema)
    if "$ref" in copied:
        return {"allOf": [copied], "deprecated": True}

    _mark_deprecated(copied)
    return copied


def _component_parameters(document: JSONObject, /, *, create: bool = True) -> dict[str, JSONObject]:
    components = document.get("components")
    if not isinstance(components, dict):
        if not create:
            return {}
        components = {}
        document["components"] = components

    parameters = components.get("parameters")
    if not isinstance(parameters, dict):
        if not create:
            return {}
        parameters = {}
        components["parameters"] = parameters

    return typing.cast("dict[str, JSONObject]", parameters)


def _parameter_identity(parameter: JSON, document: JSONObject, /) -> tuple[str, str] | None:
    materialized = _materialize_parameter(parameter, document)
    if materialized is None:
        return None

    location = _json_str(materialized.get("in"))
    name = _json_str(materialized.get("name"))
    return (location, name) if location is not None and name is not None else None


def _materialize_parameter(parameter: JSON, document: JSONObject, /) -> JSONObject | None:
    if not isinstance(parameter, dict):
        return None

    ref_name = _component_ref_name(_json_str(parameter.get("$ref")), "#/components/parameters/")
    if ref_name is not None:
        referenced_parameter = _component_parameters(document, create=False).get(ref_name)
        return copy.deepcopy(referenced_parameter) if isinstance(referenced_parameter, dict) else None

    return copy.deepcopy(parameter)


def collect_deprecations(document: JSONObject, /) -> list[JSONObject]:
    deprecated: list[JSONObject] = []
    tags = document.get("tags")

    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("x-deprecated") is True:
                deprecated.append({"kind": "controller", "name": _json_str(tag.get("name")) or "<unnamed>"})

    paths = document.get("paths")
    if isinstance(paths, dict):
        for path, path_item in paths.items():
            if isinstance(path_item, dict) and path_item.get("x-deprecated") is True:
                deprecated.append({"kind": "path", "path": path})

    for path, method, operation in iter_path_operations(document):
        if operation.get("deprecated") is True:
            deprecated.append(
                {
                    "kind": "operation",
                    "path": path,
                    "method": method,
                    "operation_id": _json_str(operation.get("operationId")),
                    "tags": _json_str_list(operation.get("tags")),
                }
            )

        parameters = operation.get("parameters")
        if isinstance(parameters, list):
            for index, parameter in enumerate(parameters):
                if isinstance(parameter, dict) and parameter.get("deprecated") is True:
                    deprecated.append(
                        {
                            "kind": "parameter",
                            "path": path,
                            "method": method,
                            "location": _json_str(parameter.get("in")),
                            "name": _json_str(parameter.get("name")) or f"parameter[{index}]",
                        }
                    )

    schemas = _component_schemas(document)
    for schema_name, schema in schemas.items():
        if schema.get("deprecated") is True:
            deprecated.append({"kind": "schema", "schema": schema_name})

        for field_name, field in _schema_properties(schema).items():
            if isinstance(field, dict) and field.get("deprecated") is True:
                deprecated.append({"kind": "field", "schema": schema_name, "field": field_name})

        for enum_pointer, enum_schema in _iter_inline_enums(schema, f"#/components/schemas/{schema_name}"):
            if enum_schema.get("deprecated") is True:
                deprecated.append({"kind": "enum", "schema": schema_name, "pointer": enum_pointer})

    return sorted(deprecated, key=_diff_item_sort_key)


def iter_operations(document: JSONObject, /) -> typing.Iterator[JSONObject]:
    for _, _, operation in iter_path_operations(document):
        yield operation


def iter_path_operations(document: JSONObject, /) -> typing.Iterator[tuple[str, str, JSONObject]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        for method, operation in path_item.items():
            if method in HTTP_METHODS and isinstance(operation, dict):
                yield (path, method, operation)


def _component_schemas(document: JSONObject, /) -> dict[str, JSONObject]:
    components = document.get("components")
    if not isinstance(components, dict):
        return {}

    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return {}

    return {
        str(name): schema
        for name, schema in schemas.items()
        if isinstance(schema, dict)
    }


def _ensure_component_schemas(document: JSONObject, /) -> JSONObject:
    components = _ensure_components(document)
    schemas = components.setdefault("schemas", {})
    if not isinstance(schemas, dict):
        schemas = {}
        components["schemas"] = schemas

    return schemas


def _ensure_components(document: JSONObject, /) -> JSONObject:
    components = document.get("components")
    if isinstance(components, dict):
        return components

    components = {}
    document["components"] = components
    return components


def _schema_properties(schema: JSONObject, /) -> dict[str, JSON]:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return {str(name): value for name, value in properties.items()}

    return {}


def _collect_enum_occurrences(document: JSONObject, /) -> list[EnumOccurrence]:
    occurrences: list[EnumOccurrence] = []

    def visit(value: JSON, pointer: str, container: JSONObject | list[JSON] | None, key: str | int | None) -> None:
        if isinstance(value, dict):
            enum_values = _enum_values(value)
            if enum_values is not None and container is not None and key is not None:
                field_name, parent_model_name = _enum_pointer_context(pointer)
                occurrences.append(
                    EnumOccurrence(
                        container=container,
                        key=key,
                        schema=value,
                        pointer=pointer,
                        values=enum_values,
                        field_name=field_name,
                        parent_model_name=parent_model_name,
                    )
                )

            for child_key, child in list(value.items()):
                if child_key == "$ref":
                    continue

                visit(child, f"{pointer}/{_json_pointer_escape(str(child_key))}", value, str(child_key))
            return

        if isinstance(value, list):
            for index, child in enumerate(list(value)):
                visit(child, f"{pointer}/{index}", value, index)

    visit(document, "#", None, None)
    return occurrences


def _enum_values(schema: JSONObject, /) -> tuple[str | int | float, ...] | None:
    values = schema.get("enum")
    if not isinstance(values, list) or not values:
        return None

    if not all(isinstance(value, str | int | float) and not isinstance(value, bool) for value in values):
        return None

    return tuple(values)


def _enum_pointer_context(pointer: str, /) -> tuple[str, str | None]:
    parts = _json_pointer_parts(pointer)
    parent_model_name = parts[2] if len(parts) > 2 and parts[:2] == ["components", "schemas"] else None

    for index in range(len(parts) - 2, -1, -1):
        if parts[index] == "properties" and index + 1 < len(parts):
            return parts[index + 1], parent_model_name

    if parent_model_name is not None:
        return parent_model_name, parent_model_name

    return parts[-1] if parts else "enum", parent_model_name


def _is_enum_component_schema_pointer(pointer: str, enum_component_names: set[str], /) -> bool:
    parts = _json_pointer_parts(pointer)
    return len(parts) == 3 and parts[:2] == ["components", "schemas"] and parts[2] in enum_component_names


def _enum_component_schema(enum_schema: EnumSchema, /) -> JSONObject:
    schema: JSONObject = {
        "type": _enum_json_schema_type(enum_schema.values),
        "enum": typing.cast(JSON, list(enum_schema.values)),
        "x-enum-varnames": typing.cast(JSON, list(enum_schema.members)),
        "x-enumNames": typing.cast(JSON, list(enum_schema.members)),
    }

    if enum_schema.description:
        schema["description"] = enum_schema.description

    if enum_schema.member_descriptions:
        schema["x-enum-descriptions"] = typing.cast(JSON, copy.deepcopy(enum_schema.member_descriptions))

    return schema


def _string_error_code_fields(
    document: JSONObject,
    /,
    *,
    skip_component: str | None = None,
) -> typing.Iterator[tuple[str, JSONObject]]:
    def visit(value: JSON, pointer: str, *, is_error_code_property: bool = False) -> typing.Iterator[tuple[str, JSONObject]]:
        if not isinstance(value, dict):
            if isinstance(value, list):
                for index, child in enumerate(value):
                    yield from visit(child, f"{pointer}/{index}")

            return

        if is_error_code_property and _is_string_schema(value):
            yield pointer, value
            return

        for key, child in value.items():
            if (
                skip_component is not None
                and pointer == "#/components/schemas"
                and key == skip_component
            ):
                continue

            yield from visit(
                child,
                f"{pointer}/{_json_pointer_escape(str(key))}",
                is_error_code_property=key == "errorCode",
            )

    yield from visit(document, "#")


def _is_string_schema(schema: JSONObject, /) -> bool:
    if schema.get("type") == "string":
        return True

    enum_values = schema.get("enum")
    return isinstance(enum_values, list) and all(isinstance(item, str) for item in enum_values)


def _error_code_sort_key(value: str, /) -> tuple[str, int, str]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", value)
    return (match.group(1), int(match.group(2)), value) if match else (value, -1, value)


def _enum_json_schema_type(values: list[str | int | float] | tuple[str | int | float, ...], /) -> str:
    if values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return "integer"

    if values and all(isinstance(value, int | float) and not isinstance(value, bool) for value in values):
        return "number"

    return "string"


def _find_enum_nicification_match(
    values: tuple[str | int | float, ...],
    occurrence: EnumOccurrence,
    nicifications: NicificatedSchema,
    /,
    *,
    allow_new_values: bool = False,
) -> tuple[str, EnumSchema] | None:
    value_set = set(values)
    candidate_keys = _enum_candidate_keys(occurrence)
    ranked: list[tuple[int, str, EnumSchema]] = []

    for enum_key, enum_schema in nicifications.schema.enums.items():
        enum_values = tuple(enum_schema.values)
        enum_value_set = set(enum_values)
        rank = 0

        if values == enum_values:
            rank = 100_000
        elif value_set == enum_value_set:
            rank = 90_000
        elif value_set and value_set.issubset(enum_value_set):
            rank = 70_000
        elif allow_new_values:
            if enum_key in candidate_keys:
                rank = 60_000
            else:
                overlap = len(value_set & enum_value_set)
                if overlap:
                    rank = 40_000 + overlap * 1_000

        if rank:
            rank += _enum_context_score(enum_key, enum_schema, occurrence)
            ranked.append((rank, enum_key, enum_schema))

    if not ranked:
        return None

    ranked.sort(key=lambda item: (item[0], -len(item[2].values), item[1]), reverse=True)
    _, enum_key, enum_schema = ranked[0]
    return enum_key, enum_schema


def _append_enum_values(
    enum_schema: EnumSchema,
    values: tuple[str | int | float, ...],
    source_schema: JSONObject,
    /,
) -> list[str | int | float]:
    added_values: list[str | int | float] = []
    source_members = _enum_member_names(source_schema, values)
    used_members = set(enum_schema.members)

    for index, value in enumerate(values):
        if value in enum_schema.values:
            continue

        member_name = source_members[index] if index < len(source_members) else _enum_member_name(value, values)
        member_name = _unique_enum_member_name(member_name, used_members)
        enum_schema.values.append(value)
        enum_schema.members.append(member_name)
        used_members.add(member_name)
        added_values.append(value)

    return added_values


def _new_enum_nicification_identity(
    values: tuple[str | int | float, ...],
    occurrence: EnumOccurrence,
    /,
) -> tuple[str, str, str | None]:
    parent_name = occurrence.parent_model_name or "Schema"
    base_name = _contextual_schema_model_name(parent_name, occurrence.field_name).removesuffix("Dto")
    return _to_snake_case(base_name), base_name, None


def _enum_ref_schema(
    schema: JSONObject,
    values: tuple[str | int | float, ...],
    enum_schema: EnumSchema,
    /,
) -> JSONObject:
    enum_ref = f"{ENUM_SCHEMA_REF_PREFIX}{enum_schema.name}"
    siblings = {
        str(key): copy.deepcopy(value)
        for key, value in schema.items()
        if str(key) not in ENUM_REF_IGNORED_KEYS
    }

    if _is_full_enum_match(values, enum_schema) and not siblings:
        return {"$ref": enum_ref}

    replacement: JSONObject = {"allOf": [{"$ref": enum_ref}]}

    if not _is_full_enum_match(values, enum_schema):
        members = _enum_members_for_values(values, enum_schema)
        replacement["enum"] = typing.cast(JSON, list(values))
        replacement["x-enum-ref"] = enum_ref

        if len(values) == 1:
            replacement["x-enum-value"] = typing.cast(JSON, values[0])
            replacement["x-enum-member"] = members[0] if members else _enum_member_name(values[0], values)
        else:
            replacement["x-enum-members"] = typing.cast(JSON, members)

    replacement.update(siblings)
    return replacement


def _is_full_enum_match(values: tuple[str | int | float, ...], enum_schema: EnumSchema, /) -> bool:
    return len(values) == len(enum_schema.values) and set(values) == set(enum_schema.values)


def _enum_members_for_values(values: tuple[str | int | float, ...], enum_schema: EnumSchema, /) -> list[str]:
    members_by_value = {
        value: member
        for value, member in zip(enum_schema.values, enum_schema.members, strict=False)
    }
    return [members_by_value.get(value, _enum_member_name(value, values)) for value in values]


def _enum_member_names(schema: JSONObject, values: tuple[str | int | float, ...], /) -> list[str]:
    for key in ("x-enumNames", "x-enum-varnames"):
        raw_names = schema.get(key)
        if isinstance(raw_names, list) and len(raw_names) == len(values) and all(isinstance(item, str) for item in raw_names):
            return typing.cast(list[str], raw_names)

    used: set[str] = set()
    members: list[str] = []
    for value in values:
        member = _unique_enum_member_name(_enum_member_name(value, values), used)
        used.add(member)
        members.append(member)

    return members


def _enum_member_name(value: str | int | float, values: tuple[str | int | float, ...], /) -> str:
    if value == "":
        return "NOTHING"

    raw_value = str(value)
    prefix = _common_enum_value_prefix(values)
    if prefix and raw_value.startswith(prefix):
        raw_value = raw_value.removeprefix(prefix)

    words = _identifier_words(raw_value)
    if not words:
        return "VALUE"

    member = "_".join(word.upper() for word in words)
    if member[:1].isdigit():
        member = f"V{member}"

    return member


def _unique_enum_member_name(member_name: str, used_members: set[str], /) -> str:
    candidate = member_name
    counter = 2

    while candidate in used_members:
        candidate = f"{member_name}_{counter}"
        counter += 1

    return candidate


def _common_enum_value_prefix(values: tuple[str | int | float, ...], /) -> str:
    string_values = [value for value in values if isinstance(value, str) and "." in value]
    if len(string_values) != len(values):
        return ""

    first_prefix = string_values[0].split(".", 1)[0]
    if all(value.startswith(f"{first_prefix}.") for value in string_values):
        return f"{first_prefix}."

    return ""


def _enum_candidate_keys(occurrence: EnumOccurrence, /) -> set[str]:
    keys: set[str] = set()
    field_name = _to_pascal_case(occurrence.field_name)

    if occurrence.parent_model_name:
        parent_name = occurrence.parent_model_name.removesuffix("Dto").removesuffix("Request").removesuffix("Response")
        if parent_name:
            keys.add(_to_snake_case(parent_name))
            keys.add(_to_snake_case(f"{parent_name}{field_name}"))

    if occurrence.field_name:
        keys.add(_to_snake_case(occurrence.field_name))

    return keys


def _enum_context_score(enum_key: str, enum_schema: EnumSchema, occurrence: EnumOccurrence, /) -> int:
    score = 0
    context = " ".join(
        part.lower()
        for part in (occurrence.pointer, occurrence.field_name, occurrence.parent_model_name or "")
    )
    candidate_keys = _enum_candidate_keys(occurrence)

    if enum_key in candidate_keys:
        score += 500

    for token in set(_identifier_words(enum_key) + _identifier_words(enum_schema.name)):
        if len(token) > 1 and token in context:
            score += 10

    return score


def _iter_inline_enums(schema: JSONObject, pointer: str) -> typing.Iterator[tuple[str, JSONObject]]:
    if isinstance(schema.get("enum"), list):
        yield (pointer, schema)

    for keyword in ("oneOf", "anyOf", "allOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for index, variant in enumerate(variants):
                if isinstance(variant, dict):
                    yield from _iter_inline_enums(variant, f"{pointer}/{keyword}/{index}")

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, property_schema in properties.items():
            if isinstance(property_schema, dict):
                yield from _iter_inline_enums(property_schema, f"{pointer}/properties/{name}")

    items = schema.get("items")
    if isinstance(items, dict):
        yield from _iter_inline_enums(items, f"{pointer}/items")


def _find_enum_schema_matches(
    schemas: dict[str, JSONObject],
    enum_key: str,
    enum_name: str,
    enum_values: list[str | int | float],
    /,
) -> list[tuple[str, JSONObject]]:
    exact_names = {_to_pascal_case(enum_key), enum_key, enum_name}
    matches: list[tuple[str, JSONObject]] = []

    for schema_name, schema in schemas.items():
        raw_enum = schema.get("enum")
        if not isinstance(raw_enum, list):
            continue

        if schema_name in exact_names or raw_enum == enum_values:
            matches.append((schema_name, schema))

    return matches


def _enum_metadata(schema: JSONObject, /) -> JSONObject:
    return {
        "description": schema.get("description"),
        "x-enum-varnames": copy.deepcopy(schema.get("x-enum-varnames")),
        "x-enumNames": copy.deepcopy(schema.get("x-enumNames")),
        "x-enum-descriptions": copy.deepcopy(schema.get("x-enum-descriptions")),
    }


def _response_ref(response: JSONObject, /) -> str | None:
    ref = response.get("$ref")
    return ref if isinstance(ref, str) else None


def _schema_ref(schema: JSONObject, /) -> str | None:
    ref = schema.get("$ref")
    return ref if isinstance(ref, str) else None


def _replace_container_child(
    container: JSONObject | list[JSON],
    key: str | int,
    value: JSONObject,
    /,
) -> None:
    if isinstance(container, list) and isinstance(key, int):
        container[key] = value
        return

    if isinstance(container, dict) and isinstance(key, str):
        container[key] = value


def _is_inline_object_schema(schema: JSONObject, /) -> bool:
    return not _is_map_object_schema(schema) and "$ref" not in schema and (
        schema.get("type") == "object"
        or isinstance(schema.get("properties"), dict)
    )


def _is_map_object_schema(schema: JSONObject, /) -> bool:
    if "$ref" in schema or isinstance(schema.get("properties"), dict):
        return False

    if schema.get("type") != "object":
        return False

    allowed_keys = {
        "type",
        "additionalProperties",
        "nullable",
        "description",
        "title",
        "default",
        "example",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "minProperties",
        "maxProperties",
    }
    if any(key not in allowed_keys and not str(key).startswith("x-") for key in schema):
        return False

    additional_properties = schema.get("additionalProperties")
    return (
        "additionalProperties" not in schema
        or isinstance(additional_properties, bool)
        or isinstance(additional_properties, dict)
    )


def _response_signature(response: JSONObject, /) -> str:
    canonical = _canonical_json(response)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schema_signature(schema: JSONObject, /) -> str:
    canonical = _canonical_json(_schema_signature_value(schema))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schema_signature_value(value: JSON, /) -> JSON:
    if isinstance(value, dict):
        return {
            str(key): _schema_signature_value(child)
            for key, child in value.items()
            if not _is_schema_signature_ignored_key(str(key))
        }

    if isinstance(value, list):
        return [_schema_signature_value(item) for item in value]

    return value


def _is_schema_signature_ignored_key(key: str, /) -> bool:
    return key in SCHEMA_SIGNATURE_IGNORED_KEYS or key.startswith("x-")


def _canonical_json(value: JSON, /) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _is_error_status(status_code: str, /) -> bool:
    if status_code == "default":
        return False

    try:
        return int(status_code) >= 300
    except ValueError:
        return False


def _raw_response_name(
    status_code: str,
    response: JSONObject,
    /,
) -> str:
    if not _is_error_status(status_code):
        ref_schema_name = _response_schema_ref_name(response)
        if ref_schema_name is not None:
            return _non_error_response_name_from_schema_ref(status_code, ref_schema_name)

        status_name = NON_ERROR_STATUS_TITLE_OVERRIDES.get(status_code) or _to_pascal_case(status_code)
        description = response.get("description")
        description_name = _to_pascal_case(description) if isinstance(description, str) and description.strip() else ""
        if description_name and description_name != status_name:
            return f"{description_name.removesuffix('Response')}Response"

        return f"{status_name}Response"

    status_name = STATUS_TITLE_OVERRIDES.get(status_code) or _to_pascal_case(status_code)
    description = response.get("description")
    description_name = _to_pascal_case(description) if isinstance(description, str) and description.strip() else ""

    if (
        description_name
        and description_name != status_name
        and not description_name.startswith(status_name)
        and not status_name.startswith(description_name)
    ):
        base = description_name.removesuffix("Error") + ("ErrorDto" if "content" in response else "Error")
    else:
        base = status_name.removesuffix("Error") + "Error"

    return base


def _unique_response_name(
    raw_name: str,
    used_names: set[str],
    nicificated_schema: NicificatedSchema,
    /,
    *,
    is_error: bool,
) -> str:
    nicification = nicificated_schema.schema.errors.get(raw_name) if is_error else None
    configured_name = nicification.name if nicification is not None else raw_name
    return _unique_name(configured_name, used_names)


def _response_schema_ref_name(response: JSONObject, /) -> str | None:
    content = response.get("content")
    if not isinstance(content, dict):
        return None

    for media_type in ("application/json", "application/problem+json"):
        media = content.get(media_type)
        if not isinstance(media, dict):
            continue

        schema = media.get("schema")
        if not isinstance(schema, dict):
            continue

        schema_name = _schema_component_ref_name(_schema_ref(schema))
        if schema_name is not None:
            return schema_name

    for media in content.values():
        if not isinstance(media, dict):
            continue

        schema = media.get("schema")
        if not isinstance(schema, dict):
            continue

        schema_name = _schema_component_ref_name(_schema_ref(schema))
        if schema_name is not None:
            return schema_name

    return None


def _non_error_response_name_from_schema_ref(status_code: str, schema_name: str, /) -> str:
    status_name = NON_ERROR_STATUS_TITLE_OVERRIDES.get(status_code) or _to_pascal_case(status_code)
    model_name = schema_name.removesuffix("Dto")
    return f"{status_name}{model_name}"


def _response_component_ref_name(ref: str | None, /) -> str | None:
    return _component_ref_name(ref, COMPONENT_RESPONSE_PREFIX)


def _schema_component_ref_name(ref: str | None, /) -> str | None:
    return _component_ref_name(ref, "#/components/schemas/")


def _component_ref_name(ref: str | None, prefix: str, /) -> str | None:
    if ref is None or not ref.startswith(prefix):
        return None

    name = ref.removeprefix(prefix)
    return name if name else None


def _unique_object_name(
    raw_name: str,
    used_names: set[str],
    nicificated_schema: NicificatedSchema,
    /,
) -> str:
    configured_name = _object_nicification_name(raw_name, nicificated_schema) or raw_name
    return _unique_name(configured_name, used_names)


def _object_nicification_name(raw_name: str, nicificated_schema: NicificatedSchema, /) -> str | None:
    nicification = nicificated_schema.schema.objects.get(raw_name)
    return nicification.name if nicification is not None else None


def _unique_name(name: str, used_names: set[str], /) -> str:
    candidate = name
    counter = 2

    while candidate in used_names:
        candidate = f"{name}{counter}"
        counter += 1

    return candidate


def _operation_schema_context_name(operation: JSONObject, method: str, path: str, /) -> str:
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id:
        return _to_pascal_case(operation_id)

    parts = [part.strip("{}") for part in path.split("/") if part]
    path_name = "".join(_to_pascal_case(part) for part in parts) or "Root"
    return f"{_to_pascal_case(method)}{path_name}"


def _deprecated_controller_tags(document: JSONObject, /) -> set[str]:
    operations_by_tag: dict[str, list[JSONObject]] = {}

    for operation in iter_operations(document):
        tags = operation.get("tags")
        if not isinstance(tags, list):
            continue

        for tag in tags:
            if isinstance(tag, str):
                operations_by_tag.setdefault(tag, []).append(operation)

    return {
        tag
        for tag, operations in operations_by_tag.items()
        if operations and all(operation.get("deprecated") is True for operation in operations)
    }


def _find_tag_object(tags: list[JSON], tag_name: str, /) -> JSONObject | None:
    for tag in tags:
        if isinstance(tag, dict) and tag.get("name") == tag_name:
            return tag

    return None


def _diff_items(left: list[JSONObject], right: list[JSONObject], /) -> list[JSONObject]:
    right_keys = {_canonical_json(item) for item in right}
    return [item for item in left if _canonical_json(item) not in right_keys]


def _diff_item_sort_key(item: JSONObject, /) -> str:
    return _canonical_json(item)


def _json_str(value: JSON, /) -> str | None:
    return value if isinstance(value, str) else None


def _json_str_list(value: JSON, /) -> list[str]:
    if not isinstance(value, list):
        return []

    return [item for item in value if isinstance(item, str)]


def _to_pascal_case(value: str, /) -> str:
    words = WORD_RE.findall(value)
    return "".join(word[:1].upper() + word[1:] for word in words) or "Response"


def _to_snake_case(value: str, /) -> str:
    return "_".join(_identifier_words(value))


def _identifier_words(value: str, /) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return [word.lower() for word in WORD_RE.findall(value)]


def _schema_model_name_from_field(field_name: str, /) -> str:
    name = _to_pascal_case(field_name)
    return name if name.endswith(("Dto", "Object", "Model")) else f"{name}Dto"


def _contextual_schema_model_name(parent_model_name: str, field_name: str, /) -> str:
    parent = parent_model_name.removesuffix("Dto")
    field = _schema_model_name_from_field(field_name).removeprefix(parent)

    if parent.endswith("Response") and field.endswith("ResponseDto"):
        field = field.removesuffix("ResponseDto")
    elif field_name == "response" and field == "ResponseDto":
        field = "Response"

    return f"{parent}{field}"


def _json_pointer_escape(value: str, /) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_pointer_unescape(value: str, /) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _json_pointer_parts(pointer: str, /) -> list[str]:
    if pointer == "#":
        return []

    return [_json_pointer_unescape(part) for part in pointer.removeprefix("#/").split("/") if part]


__all__ = (
    "NicificationResult",
    "apply_deprecation_annotations",
    "apply_enum_nicifications",
    "collect_deprecations",
    "hoist_error_responses",
    "hoist_duplicate_error_responses",
    "hoist_inline_objects",
    "hoist_response_components",
    "inline_map_object_schema_components",
    "nicificate_openapi_document",
    "update_enum_nicifications",
)
