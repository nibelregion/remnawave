import unittest

from source_cache import (
    SchemaIndex,
    build_schema_index,
    object_fingerprint,
    parse_ts_schemas,
)


class ParseTsSchemasTests(unittest.TestCase):
    def test_extracts_properties_and_literals(self) -> None:
        source = """
        import { z } from 'zod';

        const TcpHeaderNoneSchema = z.object({
            type: z.literal('none'),
        });

        export const TcpTransportOptionsSchema = z.object({
            header: TcpHeaderSchema.nullable(),
        });
        """

        schemas = {schema.name: schema for schema in parse_ts_schemas(source)}

        self.assertIn("TcpHeaderNone", schemas)
        self.assertEqual(schemas["TcpHeaderNone"].properties, ("type",))
        self.assertEqual(schemas["TcpHeaderNone"].literals, (("type", "none"),))
        self.assertEqual(schemas["TcpTransportOptions"].properties, ("header",))

    def test_nested_objects_do_not_leak_into_parent(self) -> None:
        source = """
        const OuterSchema = z.object({
            inner: z.object({ a: z.string(), b: z.number() }),
            name: z.string(),
        });
        """

        schemas = {schema.name: schema for schema in parse_ts_schemas(source)}

        self.assertEqual(schemas["Outer"].properties, ("inner", "name"))


class SchemaIndexTests(unittest.TestCase):
    def test_ambiguous_fingerprint_is_not_resolved(self) -> None:
        source = """
        const FooSchema = z.object({ value: z.string() });
        const BarSchema = z.object({ value: z.string() });
        """
        index = SchemaIndex()
        for schema in parse_ts_schemas(source):
            index.add(schema)

        fingerprint = object_fingerprint(["value"], [])
        self.assertIsNone(index.resolve(fingerprint))

    def test_literal_disambiguates_matching_properties(self) -> None:
        source = """
        const NoneSchema = z.object({ type: z.literal('none') });
        const HttpSchema = z.object({ type: z.literal('http'), body: z.string() });
        """
        index = SchemaIndex()
        for schema in parse_ts_schemas(source):
            index.add(schema)

        self.assertEqual(
            index.resolve(object_fingerprint(["type"], [("type", "none")])),
            "None",
        )

    def test_generic_command_schema_names_are_ignored(self) -> None:
        source = "export const ResponseSchema = z.object({ response: FooSchema });"
        index = build_schema_index_from_text(source)

        self.assertEqual(index.by_fingerprint, {})


def build_schema_index_from_text(text: str, /) -> SchemaIndex:
    index = SchemaIndex()
    for schema in parse_ts_schemas(text):
        index.add(schema)

    return index
