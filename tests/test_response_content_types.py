import unittest

from nicifcations_schema import (
    NicificatedSchema,
    Remnawave,
    ResponseContentType,
    Schema,
)
from nicifier_schema import apply_response_content_types, hoist_response_components


def _schema(overrides: list[ResponseContentType]) -> NicificatedSchema:
    return NicificatedSchema(
        remnawave=Remnawave(),
        schema=Schema(),
        response_content_types=overrides,
    )


class ResponseContentTypeTests(unittest.TestCase):
    def test_sets_html_content_on_empty_response(self) -> None:
        document = {
            "paths": {
                "/api/sub/{shortUuid}": {"get": {"responses": {"200": {"description": ""}}}},
            }
        }
        nicifications = _schema(
            [ResponseContentType(path="/api/sub/{shortUuid}", media_type="text/html")]
        )

        changes = apply_response_content_types(document, nicifications)

        response = document["paths"]["/api/sub/{shortUuid}"]["get"]["responses"]["200"]
        self.assertEqual(response["content"], {"text/html": {"schema": {"type": "string"}}})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["media_type"], "text/html")

    def test_html_response_is_not_hoisted_into_ok_response(self) -> None:
        document = {
            "paths": {
                "/api/sub/{shortUuid}": {"get": {"responses": {"200": {"description": ""}}}},
                "/api/sub/{shortUuid}/{clientType}": {
                    "get": {"responses": {"200": {"description": ""}}}
                },
            }
        }
        nicifications = _schema(
            [
                ResponseContentType(path="/api/sub/{shortUuid}", media_type="text/html"),
                ResponseContentType(
                    path="/api/sub/{shortUuid}/{clientType}", media_type="text/html"
                ),
            ]
        )

        apply_response_content_types(document, nicifications)
        hoist_response_components(document, nicificated_schema=nicifications, min_occurrences=1)

        component_responses = document["components"]["responses"]
        for path in document["paths"].values():
            response = path["get"]["responses"]["200"]
            ref = response["$ref"].rsplit("/", 1)[-1]
            resolved = component_responses[ref]
            self.assertEqual(list(resolved["content"]), ["text/html"])
            self.assertEqual(resolved["content"]["text/html"]["schema"], {"type": "string"})
