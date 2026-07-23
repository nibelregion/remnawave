import unittest

from nicifcations_schema import NicificatedSchema, ObjectSchema, Remnawave, Schema
from nicifier_schema import _schema_signature, hoist_inline_objects


class InlineObjectTests(unittest.TestCase):
    def test_required_order_does_not_change_signature(self) -> None:
        first = {
            "type": "object",
            "properties": {"alpha": {"type": "string"}, "beta": {"type": "number"}},
            "required": ["alpha", "beta"],
        }
        second = {
            "type": "object",
            "properties": {"beta": {"type": "number"}, "alpha": {"type": "string"}},
            "required": ["beta", "alpha"],
        }

        self.assertEqual(_schema_signature(first), _schema_signature(second))

    def test_explicit_name_preserves_semantic_identity(self) -> None:
        payload = {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        }
        document = {
            "components": {
                "schemas": {
                    "FirstDto": {
                        "type": "object",
                        "properties": {"payload": payload},
                    },
                    "SecondDto": {
                        "type": "object",
                        "properties": {"payload": payload},
                    },
                }
            }
        }
        nicifications = NicificatedSchema(
            remnawave=Remnawave(),
            schema=Schema(
                objects={
                    "SecondPayloadDto": ObjectSchema(name="NamedPayloadDto"),
                }
            ),
        )

        changes = hoist_inline_objects(document, nicifications)

        schemas = document["components"]["schemas"]
        self.assertIn("FirstPayloadDto", schemas)
        self.assertIn("NamedPayloadDto", schemas)
        self.assertEqual(
            document["components"]["schemas"]["FirstDto"]["properties"]["payload"],
            {"$ref": "#/components/schemas/FirstPayloadDto"},
        )
        self.assertEqual(
            document["components"]["schemas"]["SecondDto"]["properties"]["payload"],
            {"$ref": "#/components/schemas/NamedPayloadDto"},
        )
        self.assertEqual([change["action"] for change in changes], ["hoisted", "hoisted"])


if __name__ == "__main__":
    unittest.main()
