import pytest

from verify import bind_input_identity


def test_actual_confidence_input_identity_shape():
    bind_input_identity({"input_identity": {"artifact_id": "raw-example", "sha256": "a"*64},
                         "model_revision": "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86"}, "a"*64)


@pytest.mark.parametrize("value", [{"raw_input_sha256": "a"*64},
                                  {"input_identity": {"sha256": "b"*64}},
                                  {"input_identity": {"sha256": "a"*64}, "model_revision": "wrong"}])
def test_wrong_input_or_revision_not_qualified(value):
    with pytest.raises(ValueError):
        bind_input_identity(value, "a"*64)
