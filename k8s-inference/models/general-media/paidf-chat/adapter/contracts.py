"""Reviewed PAIDF subset of the vLLM 0.19 OpenAI request contract.

No prompt, sampling or media rewriting. Unknown fields fail before inference.
The exact upstream SDK sends these fields for caption/prompt/verification.
"""

MODELS = {
    "qwen3-6-27b-fp8": (
        "Qwen/Qwen3.6-27B-FP8",
        "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
    ),
    "qwen2-5-14b-instruct": (
        "Qwen/Qwen2.5-14B-Instruct",
        "cf98f3b3bbb457ad9e2bb7baf9a0125b6b88caa8",
    ),
}
MEDIA_BYTES = 128 * 1024**2
IMAGE_BYTES = 16 * 1024**2


def obj(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def contract(app_id):
    model, revision = MODELS[app_id]
    parts = [
        obj({"type": {"const": "text"}, "text": {"type": "string"}}, ["type", "text"])
    ]
    if app_id == "qwen3-6-27b-fp8":
        for name, media_types, limit in (
            ("image_url", ["image/png", "image/jpeg", "image/webp"], IMAGE_BYTES),
            ("video_url", ["video/mp4"], MEDIA_BYTES),
        ):
            url = {
                "type": "string",
                "pattern": "^data:(?:" + "|".join(media_types) + ");base64,",
                "maxLength": ((limit + 2) // 3) * 4 + 64,
                "x-reference-media-types": media_types,
                "x-reference-max-bytes": limit,
            }
            fields = {"url": url}
            if name == "image_url":
                fields["detail"] = {"enum": ["auto", "low", "high"]}
            parts.append(
                obj(
                    {"type": {"const": name}, name: obj(fields, ["url"])},
                    ["type", name],
                )
            )
    content = {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "minItems": 1, "items": {"oneOf": parts}},
        ]
    }
    schema = obj(
        {
            "model": {
                "const": model,
                "description": "Exact pinned source model; no provider fallback.",
            },
            "messages": {
                "type": "array",
                "minItems": 1,
                "items": obj(
                    {
                        "role": {"enum": ["system", "user", "assistant"]},
                        "content": content,
                    },
                    ["role", "content"],
                ),
            },
            "temperature": {"type": "number", "minimum": 0, "maximum": 2},
            "top_p": {"type": "number", "minimum": 0, "maximum": 1},
            "frequency_penalty": {"type": "number", "minimum": -2, "maximum": 2},
            "presence_penalty": {"type": "number", "minimum": -2, "maximum": 2},
            "max_tokens": {"type": "integer", "minimum": 1},
            "stream": {
                "type": "boolean",
                "description": "Forwarded unchanged. The durable result preserves the original JSON or SSE bytes.",
            },
            "response_format": {
                "oneOf": [
                    obj({"type": {"enum": ["text", "json_object"]}}, ["type"]),
                    obj(
                        {
                            "type": {"const": "json_schema"},
                            "json_schema": obj(
                                {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "schema": {"type": "object"},
                                    "strict": {"type": "boolean"},
                                },
                                ["name", "schema"],
                            ),
                        },
                        ["type", "json_schema"],
                    ),
                ]
            },
            "chat_template_kwargs": {
                "type": "object",
                "description": "NVIDIA reference chat-template settings, forwarded verbatim.",
            },
        },
        ["model", "messages"],
    )
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "x-scientific-source-revision": revision,
            "description": "Unmodified NVIDIA PAIDF caption/prompt/verification OpenAI request over durable native transport; not a pipeline.",
        }
    )
    return schema
