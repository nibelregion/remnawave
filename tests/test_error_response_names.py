import unittest

from nicifcations_schema import ErrorSchema, NicificatedSchema, Remnawave, Schema
from nicifier_schema import (
    _raw_response_name,
    hoist_response_components,
    rename_error_response_components_from_refs,
)

SCHEMA_REF = "#/components/schemas/NodeNotFoundErrorDto"


def _nicifications(errors: dict[str, ErrorSchema] | None = None) -> NicificatedSchema:
    return NicificatedSchema(remnawave=Remnawave(), schema=Schema(errors=errors or {}))


def _document(response: dict, *, status: str = "404", operation_id: str = "NodeController_getNode") -> dict:
    return {
        "paths": {
            "/api/nodes/{uuid}": {
                "get": {"operationId": operation_id, "responses": {status: response}}
            }
        }
    }


class RawResponseNameTests(unittest.TestCase):
    def test_description_is_used_when_present(self) -> None:
        name = _raw_response_name("404", {"description": "Node not found"})

        self.assertEqual(name, "NodeNotFoundError")

    def test_operation_id_method_is_used_without_description(self) -> None:
        operation = {"operationId": "NodeController_getNodeMetadata"}

        name = _raw_response_name("404", {"description": ""}, operation)

        self.assertEqual(name, "GetNodeMetadataNotFoundError")

    def test_status_name_is_the_last_resort(self) -> None:
        name = _raw_response_name("404", {"description": ""}, {})

        self.assertEqual(name, "NotFoundError")


class RenameFromRefTests(unittest.TestCase):
    def _hoist_and_rename(self, document: dict, nicifications: NicificatedSchema) -> list[dict]:
        hoisted = hoist_response_components(
            document, nicificated_schema=nicifications, min_occurrences=1
        )
        return rename_error_response_components_from_refs(
            document, nicificated_schema=nicifications, hoisted_changes=hoisted
        )

    def test_ref_body_drives_the_component_name(self) -> None:
        document = _document(
            {
                "description": "Node not found (see errorCode for more details)",
                "content": {"application/json": {"schema": {"$ref": SCHEMA_REF}}},
            }
        )
        nicifications = _nicifications()

        changes = self._hoist_and_rename(document, nicifications)

        self.assertEqual([change["name"] for change in changes], ["NodeNotFoundErrorDtoSchema"])
        self.assertIn("NodeNotFoundErrorDtoSchema", document["components"]["responses"])
        self.assertEqual(
            document["paths"]["/api/nodes/{uuid}"]["get"]["responses"]["404"]["$ref"],
            "#/components/responses/NodeNotFoundErrorDtoSchema",
        )

    def test_union_body_keeps_the_description_name(self) -> None:
        document = _document(
            {
                "description": "Node or Metadata not found",
                "content": {
                    "application/json": {"schema": {"oneOf": [{"$ref": SCHEMA_REF}]}}
                },
            }
        )

        changes = self._hoist_and_rename(document, _nicifications())

        self.assertEqual(changes, [])

    def test_success_response_is_left_alone(self) -> None:
        document = _document(
            {
                "description": "Node found",
                "content": {"application/json": {"schema": {"$ref": SCHEMA_REF}}},
            },
            status="200",
        )

        changes = self._hoist_and_rename(document, _nicifications())

        self.assertEqual(changes, [])

    def test_configured_error_name_is_never_overridden(self) -> None:
        document = _document(
            {
                "description": "Node not found (see errorCode for more details)",
                "content": {"application/json": {"schema": {"$ref": SCHEMA_REF}}},
            }
        )
        nicifications = _nicifications(
            {"NodeNotFoundSeeErrorCodeForMoreDetailsErrorDto": ErrorSchema(name="PinnedError")}
        )

        changes = self._hoist_and_rename(document, nicifications)

        self.assertEqual(changes, [])
        self.assertIn("PinnedError", document["components"]["responses"])
