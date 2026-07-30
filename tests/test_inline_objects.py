import unittest

from error_codes import ErrorCode
from nicifcations_schema import NicificatedSchema, ObjectSchema, Remnawave, Schema
from nicifier_schema import (
    _schema_signature,
    hoist_inline_objects,
    nicificate_openapi_document,
    rename_response_schema_components,
    update_object_nicifications,
)
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

    def test_object_nicification_resolves_name_aliases(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "OwnerDto": {
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "object",
                                "properties": {"enabled": {"type": "boolean"}},
                            }
                        },
                    }
                }
            }
        }
        nicifications = NicificatedSchema(
            remnawave=Remnawave(),
            schema=Schema(
                objects={
                    "OwnerPayloadDto": ObjectSchema(name="SharedPayloadDto"),
                    "SharedPayloadDto": ObjectSchema(name="PayloadDto"),
                }
            ),
        )

        hoist_inline_objects(document, nicifications)

        schemas = document["components"]["schemas"]
        self.assertIn("PayloadDto", schemas)
        self.assertNotIn("SharedPayloadDto", schemas)
        self.assertEqual(
            schemas["OwnerDto"]["properties"]["payload"],
            {"$ref": "#/components/schemas/PayloadDto"},
        )

    def test_get_prefix_is_omitted_from_reused_response_object_name(self) -> None:
        payload = {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                }
            },
        }
        document = {
            "components": {
                "schemas": {
                    "GetWidgetResponseDto": {
                        "type": "object",
                        "properties": {"response": payload},
                    },
                    "GetWidgetByIdResponseDto": {
                        "type": "object",
                        "properties": {"response": payload},
                    },
                }
            }
        }
        nicifications = NicificatedSchema(remnawave=Remnawave(), schema=Schema())

        hoist_inline_objects(document, nicifications)

        schemas = document["components"]["schemas"]
        self.assertIn("WidgetResponse", schemas)
        self.assertIn("WidgetResponseDetailDto", schemas)
        self.assertNotIn("GetWidgetResponse", schemas)
        self.assertEqual(
            schemas["GetWidgetResponseDto"]["properties"]["response"],
            {"$ref": "#/components/schemas/WidgetResponse"},
        )
        self.assertEqual(
            schemas["GetWidgetByIdResponseDto"]["properties"]["response"],
            {"$ref": "#/components/schemas/WidgetResponse"},
        )

    def test_get_prefix_is_retained_when_general_name_collides(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "WidgetResponse": {
                        "type": "object",
                        "properties": {"existing": {"type": "string"}},
                    },
                    "GetWidgetResponseDto": {
                        "type": "object",
                        "properties": {
                            "response": {
                                "type": "object",
                                "properties": {"enabled": {"type": "boolean"}},
                            }
                        },
                    },
                    "OtherResponseDto": {
                        "type": "object",
                        "properties": {
                            "response": {
                                "type": "object",
                                "properties": {"count": {"type": "number"}},
                            }
                        },
                    },
                }
            }
        }
        nicifications = NicificatedSchema(remnawave=Remnawave(), schema=Schema())

        hoist_inline_objects(document, nicifications)

        schemas = document["components"]["schemas"]
        self.assertIn("GetWidgetResponse", schemas)
        self.assertNotIn("WidgetResponse2", schemas)
        self.assertEqual(
            schemas["GetWidgetResponseDto"]["properties"]["response"],
            {"$ref": "#/components/schemas/GetWidgetResponse"},
        )

    def test_response_wrapper_schema_omits_get_prefix_and_updates_references(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "GetWidgetResponseDto": {
                        "type": "object",
                        "properties": {"response": {"type": "string"}},
                    },
                    "OwnerDto": {
                        "type": "object",
                        "properties": {
                            "widget": {"$ref": "#/components/schemas/GetWidgetResponseDto"}
                        },
                    },
                }
            },
            "paths": {
                "/widgets": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/GetWidgetResponseDto"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
        }

        changes = rename_response_schema_components(document)

        schemas = document["components"]["schemas"]
        self.assertEqual(changes, [{"old_name": "GetWidgetResponseDto", "name": "WidgetResponseDto"}])
        self.assertIn("WidgetResponseDto", schemas)
        self.assertNotIn("GetWidgetResponseDto", schemas)
        self.assertEqual(
            schemas["OwnerDto"]["properties"]["widget"],
            {"$ref": "#/components/schemas/WidgetResponseDto"},
        )
        self.assertEqual(
            document["paths"]["/widgets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/WidgetResponseDto"},
        )

    def test_response_wrapper_schema_keeps_get_prefix_on_collision(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "WidgetResponseDto": {"type": "object", "properties": {"id": {"type": "string"}}},
                    "GetWidgetResponseDto": {
                        "type": "object",
                        "properties": {"response": {"type": "string"}},
                    },
                }
            }
        }

        self.assertEqual(rename_response_schema_components(document), [])
        self.assertIn("GetWidgetResponseDto", document["components"]["schemas"])

    def test_renamed_response_wrapper_is_not_restored_as_deprecated(self) -> None:
        document = {
            "components": {
                "schemas": {
                    "GetWidgetResponseDto": {
                        "type": "object",
                        "properties": {"response": {"type": "string"}},
                    }
                }
            }
        }
        nicifications = NicificatedSchema(remnawave=Remnawave(), schema=Schema())

        result = nicificate_openapi_document(
            document,
            nicifications,
            previous_document=document,
        )

        schemas = result.document["components"]["schemas"]
        self.assertIn("WidgetResponseDto", schemas)
        self.assertNotIn("GetWidgetResponseDto", schemas)
        self.assertNotIn(
            {"kind": "schema", "schema": "GetWidgetResponseDto"},
            result.diff["deprecation_annotations"],
        )

    def test_error_code_component_is_not_restored_as_deprecated(self) -> None:
        document = {"components": {"schemas": {}}}
        previous_document = {
            "components": {
                "schemas": {
                    "ErrorCode": {"type": "string", "enum": ["OLD"]},
                }
            }
        }
        nicifications = NicificatedSchema(remnawave=Remnawave(), schema=Schema())

        result = nicificate_openapi_document(
            document,
            nicifications,
            previous_document=previous_document,
            error_codes=[ErrorCode(member="CURRENT", value="CURRENT")],
        )

        error_code = result.document["components"]["schemas"]["ErrorCode"]
        self.assertEqual(error_code["enum"], ["CURRENT"])
        self.assertIsNot(error_code.get("deprecated"), True)
        self.assertNotIn(
            {"kind": "schema", "schema": "ErrorCode"},
            result.diff["deprecation_annotations"],
        )


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
