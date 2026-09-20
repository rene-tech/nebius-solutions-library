"""Real MP4 decode/remux and workflow gates; mocked generation is labelled as such."""
# ruff: noqa: E402

import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from fs2_video import worker
from fs2_video.contracts import AugmentationRequest, Recipe, recipe_identity
from fs2_video.media import digest, finalize_audio, inspect_video, validate_alignment
from fs2_video.paidf import pipeline_config


@pytest.fixture
def clip(tmp_path):
    output = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x480:rate=16",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ],
        check=True,
    )
    return output


def test_all_frame_decode_and_audio_remux(clip, tmp_path):
    before = inspect_video(clip)
    assert (before["frames"], before["fps"], before["width"], before["height"]) == (
        32,
        16,
        640,
        480,
    )
    copied = tmp_path / "final.mp4"
    finalize_audio(clip, clip, copied, preserve=True)
    after = inspect_video(copied)
    validate_alignment(before, after)
    assert after["audio"] == before["audio"] == [{"codec": "aac"}]
    stripped = tmp_path / "silent.mp4"
    finalize_audio(clip, clip, stripped, preserve=False)
    assert inspect_video(stripped)["audio"] == []
    with pytest.raises(ValueError, match="preserve"):
        validate_alignment(before, {**after, "frames": 31})


def test_symlink_and_invalid_video_rejected(clip, tmp_path):
    link = tmp_path / "link.mp4"
    link.symlink_to(clip)
    with pytest.raises(ValueError):
        inspect_video(link)
    invalid = tmp_path / "invalid.mp4"
    invalid.write_bytes(b"not video" * 20)
    with pytest.raises(ValueError):
        inspect_video(invalid)


@pytest.mark.parametrize(
    "recipe",
    [
        {"weather": "cloudy"},
        {"weather": "overcast", "endpoint": "https://example.com"},
        {"weather": "rain", "max_retries": 5},
        {"weather": "clear", "seed": True},
    ],
)
def test_closed_recipe(recipe):
    with pytest.raises(ValueError):
        Recipe.model_validate(recipe)


def test_identity_binds_backend_provider_and_full_defaults():
    a = recipe_identity(
        Recipe(weather="overcast"),
        vlm_model="vlm",
        llm_model="llm",
        provider_url="https://one",
    )
    b = recipe_identity(
        Recipe(weather="overcast"),
        vlm_model="vlm",
        llm_model="llm",
        provider_url="https://two",
    )
    assert a["sha256"] != b["sha256"]
    assert a["parameters"]["seed"] == 42


def test_actual_upstream_config_contract(tmp_path):
    pytest.importorskip("aug_utils.schema")
    from aug_utils.schema import PipelineConfig

    value = pipeline_config(
        tmp_path / "input.mp4",
        tmp_path,
        Recipe(weather="overcast"),
        {"url": "https://provider.example/v1", "vlm": "vlm", "llm": "llm"},
    )
    validated = PipelineConfig.model_validate(value)
    assert validated.evaluators[1].attribute_verification.vlm_verification.frames == 5
    assert validated.evaluators[0].hallucination_check.params.max_frames is None
    assert validated.pipeline.evaluation.strict


@pytest.mark.parametrize(
    "passed,code,expected",
    [(True, 0, "accepted"), (False, 1, "rejected"), (True, 1, "rejected")],
)
def test_mocked_generation_checkpoint_quality_and_resume(
    clip, tmp_path, monkeypatch, passed, code, expected
):
    """No GPU claim: test worker publication using a real video, fixture generator."""
    for key, value in {
        "PAIDF_PROVIDER_URL": "https://provider.example/v1",
        "PAIDF_VLM_MODEL": "vlm",
        "PAIDF_LLM_MODEL": "llm",
    }.items():
        monkeypatch.setenv(key, value)
    work = tmp_path / "work"
    (work / "inputs").mkdir(parents=True)
    shutil.copyfile(clip, work / "inputs/video-0000.mp4")
    value = {
        "schema": "fs2-serve.nebius.ai/video-augmentation-request/v1",
        "recipe": {"weather": "overcast"},
        "items": [
            {"id": "video-0000", "source_name": "fixture.mp4", "sha256": digest(clip)}
        ],
    }
    request = AugmentationRequest.model_validate(value)
    calls = []

    class FixturePipeline:
        def __init__(self, argv, **kwargs):
            config = json.loads(Path(argv[-1]).read_text())
            out = config["data"][0]["output"]
            calls.append(config)
            shutil.copyfile(clip, out["video"])
            Path(out["metadata"]).write_text(
                json.dumps(
                    {
                        "prompt": "fixture test, not real generation",
                        "hallucination_check": {"passed": True, "score": 1.0},
                        "attribute_verification": {"passed": passed},
                    }
                )
            )

        def wait(self, **kwargs):
            return code

    # Patching only this module's process launcher leaves real ffmpeg intact.
    monkeypatch.setattr(
        worker,
        "subprocess",
        type(
            "Process",
            (),
            {
                "Popen": FixturePipeline,
                "STDOUT": subprocess.STDOUT,
                "SubprocessError": subprocess.SubprocessError,
                "TimeoutExpired": subprocess.TimeoutExpired,
            },
        ),
    )
    operation = str(uuid4())
    result = worker.run(request, work, operation)
    assert result["items"][0]["status"] == expected
    assert result["counts"][expected] == 1
    repeated = worker.run(request, work, operation)
    assert repeated == result and len(calls) == 1
    (work / "outputs/video-0000/video.mp4").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        worker.run(request, work, operation)


def test_batch_requires_exact_approved_recipe(tmp_path, monkeypatch):
    monkeypatch.setattr(
        worker,
        "provider",
        lambda: {"url": "https://provider.example/v1", "vlm": "vlm", "llm": "llm"},
    )
    req = AugmentationRequest.model_validate(
        {
            "schema": "fs2-serve.nebius.ai/video-augmentation-request/v1",
            "recipe": {"weather": "overcast"},
            "approved_recipe_sha256": "a" * 64,
            "items": [{"id": "video-0000", "source_name": "a.mp4", "sha256": "a" * 64}],
        }
    )
    with pytest.raises(ValueError, match="approved recipe"):
        worker.run(req, tmp_path, str(uuid4()))


def test_actual_nvidia_pipeline_with_fixture_provider_and_generation(
    clip, tmp_path, monkeypatch
):
    """Exercise upstream orchestration/evaluators, without claiming real inference."""
    pytest.importorskip("aug_utils.schema")
    from types import SimpleNamespace

    import cli
    import generation.factory
    from generation.adapters.base import BaseAdapter, Result, primary_media
    from generation.adapters.openai_chat import OpenAIChatAdapter

    responses = []

    def fixture_chat(self, **kwargs):
        if kwargs.get("response_format", {}).get("type") == "json_schema":
            content = json.dumps(
                {
                    "variable": "weather_condition",
                    "value": "overcast",
                    "question": "What is the weather?",
                    "options": {"A": "overcast", "B": "clear", "C": "rain"},
                    "correct_answer": "A",
                }
            )
        elif self.model == "llm":
            content = json.dumps(
                {"prompt": "An overcast scene; preserve objects and motion."}
            )
        elif any(
            part.get("type") == "video_url"
            for message in kwargs["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
        ):
            content = "A moving synthetic test pattern."
        else:
            content = "A"
        responses.append(content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    class FixtureGenerator(BaseAdapter):
        def invoke(self, payload):
            return Result(
                media_bytes=primary_media(payload)["bytes"], request_id="fixture-only"
            )

    monkeypatch.setattr(OpenAIChatAdapter, "chat", fixture_chat)
    monkeypatch.setitem(
        generation.factory.ADAPTERS, "openai.video.sync", FixtureGenerator
    )
    output = tmp_path / "upstream"
    output.mkdir()
    config = pipeline_config(
        clip,
        output,
        Recipe(weather="overcast", max_retries=0),
        {"url": "https://fixture.invalid/v1", "vlm": "vlm", "llm": "llm"},
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["paidf", "--config", str(config_path)])
    try:
        cli.main()
    except SystemExit as error:
        assert error.code in (None, 0)
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["hallucination_check"]["passed"] is True
    assert metadata["attribute_verification"]["passed"] is True
    assert len(responses) >= 4
    validate_alignment(inspect_video(clip), inspect_video(output / "generated.mp4"))
