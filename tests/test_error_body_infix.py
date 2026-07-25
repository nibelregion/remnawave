import unittest

from nicifcations_schema import NicificatedSchema, ObjectSchema, Remnawave, Schema
from nicifier_schema import hoist_inline_objects

ERROR_BODY = {
    "type": "object",
    "properties": {"message": {"type": "string"}, "path": {"type": "string"}},
}


def _document(response_name: str, body: dict, extra_schemas: dict | None = None) -> dict:
    return {
        "components": {
            "schemas": dict(extra_schemas or {}),
            "responses": {
                response_name: {
                    "description": "Boom",
                    "content": {"application/json": {"schema": body}},
                }
            },
        }
    }


def _nicifications(objects: dict[str, ObjectSchema] | None = None) -> NicificatedSchema:
    return NicificatedSchema(remnawave=Remnawave(), schema=Schema(objects=objects or {}))


class ErrorBodyInfixTests(unittest.TestCase):
    def test_body_infix_is_dropped_when_plain_name_is_free(self) -> None:
        document = _document("ServerErrorDto", dict(ERROR_BODY))

        hoist_inline_objects(document, _nicifications())

        schemas = document["components"]["schemas"]
        self.assertIn("ServerErrorDto", schemas)
        self.assertNotIn("ServerErrorBodyDto", schemas)

    def test_body_infix_is_kept_when_plain_name_is_taken(self) -> None:
        taken = {"UserNotFoundErrorDto": {"type": "object", "properties": {"message": {"type": "string"}}}}
        document = _document("UserNotFoundErrorDto", dict(ERROR_BODY), taken)

        hoist_inline_objects(document, _nicifications())

        schemas = document["components"]["schemas"]
        self.assertIn("UserNotFoundErrorBodyDto", schemas)
        self.assertEqual(schemas["UserNotFoundErrorDto"], taken["UserNotFoundErrorDto"])

    def test_deprecated_leftover_does_not_reserve_the_plain_name(self) -> None:
        leftover = {
            "ServerErrorDto": {
                "type": "object",
                "deprecated": True,
                "properties": {"message": {"type": "string"}},
            }
        }
        document = _document("ServerErrorDto", dict(ERROR_BODY), leftover)

        hoist_inline_objects(document, _nicifications())

        self.assertNotIn("ServerErrorBodyDto", document["components"]["schemas"])

    def test_configured_name_wins_over_the_infix_rule(self) -> None:
        document = _document("UserNotFoundErrorDto", dict(ERROR_BODY))
        nicifications = _nicifications(
            {"UserNotFoundErrorBodyDto": ObjectSchema(name="UserNotFoundTimestampedErrorDto")}
        )

        hoist_inline_objects(document, nicifications)

        self.assertIn("UserNotFoundTimestampedErrorDto", document["components"]["schemas"])
