import unittest

from nicifcations_schema import NicificatedSchema, ObjectSchema, Remnawave, Schema
from nicifier_schema import _schema_signature, hoist_inline_objects, update_object_nicifications
from source_cache import SchemaIndex, parse_ts_schemas


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


class UpdateObjectNicificationsTests(unittest.TestCase):
    def test_inline_object_named_after_source_schema(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "OwnerDto": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "object",
                                "properties": {
                                    "nickname": {"type": "string"},
                                    "avatarUrl": {"type": "string"},
                                },
                            },
                        },
                    },
                }
            }
        }
        index = SchemaIndex()
        source = """
        const ProfileSchema = z.object({
            nickname: z.string(),
            avatarUrl: z.string(),
        });
        """
        for schema in parse_ts_schemas(source):
            index.add(schema)

        nicifications = NicificatedSchema(remnawave=Remnawave(), schema=Schema())
        changes = update_object_nicifications(document, nicifications, index)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["name"], "ProfileDto")
        raw_name = changes[0]["nicification"]
        self.assertEqual(nicifications.schema.objects[raw_name].name, "ProfileDto")

    def test_existing_object_mapping_is_preserved(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "OwnerDto": {
                        "type": "object",
                        "properties": {
                            "profile": {
                                "type": "object",
                                "properties": {"nickname": {"type": "string"}},
                            },
                        },
                    },
                }
            }
        }
        index = SchemaIndex()
        for schema in parse_ts_schemas("const ProfileSchema = z.object({ nickname: z.string() });"):
            index.add(schema)

        nicifications = NicificatedSchema(
            remnawave=Remnawave(),
            schema=Schema(objects={"OwnerProfileDto": ObjectSchema(name="KeepMeDto")}),
        )
        changes = update_object_nicifications(document, nicifications, index)

        self.assertEqual(changes, [])
        self.assertEqual(nicifications.schema.objects["OwnerProfileDto"].name, "KeepMeDto")


if __name__ == "__main__":
    unittest.main()
