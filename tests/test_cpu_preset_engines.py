"""Kokoro-82M (ONNX) and Phonon engines, plus operator licence acceptance.

Both engines are CPU preset-voice narrators added for GPU-less servers. These
tests pin the parts that break silently: registry wiring, the voice lists the
picker reads, the checksum-gated weight download, the phoneme splitter's
window limit, and that the Docker image keeps installing the optional CPU
engines a Railway deployment relies on.
"""
from __future__ import annotations

import hashlib
import io
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_registry_lists_both_engines():
    from services.tts_backend import _REGISTRY, preset_voices_of

    for engine_id in ("kokoro-onnx", "phonon"):
        cls = _REGISTRY[engine_id]
        assert cls.id == engine_id
        assert cls.gpu_compat == ("cpu",)
        assert cls.supports_cloning is False
        assert cls.supports_voice_design is False
        voices = preset_voices_of(cls)
        assert voices, engine_id
        assert cls.DEFAULT_VOICE in voices
        assert len(voices) == len(set(voices))


def test_engines_have_docs_and_install_hints():
    from services.tts_backend import _ENGINE_DOCS, _INSTALL_HINTS

    for engine_id in ("kokoro-onnx", "phonon"):
        assert (ROOT / _ENGINE_DOCS[engine_id]).is_file()
        assert _INSTALL_HINTS[engine_id]


def test_kokoro_voice_list_matches_voice_file_prefixes():
    from engines.kokoro_onnx import PRESET_VOICES, _PREFIX_ESPEAK

    assert len(PRESET_VOICES) == 54
    for voice in PRESET_VOICES:
        assert re.fullmatch(r"[abefhijpz][fm]_[a-z]+", voice), voice
        assert voice[0] in _PREFIX_ESPEAK


@pytest.mark.parametrize(
    ("voice", "language", "expected"),
    [
        ("af_heart", None, "en-us"),
        ("bf_emma", None, "en-gb"),
        ("bf_emma", "en", "en-gb"),  # English keeps a British voice British
        ("af_heart", "fr", "fr-fr"),  # the request language wins
        ("ff_siwis", None, "fr-fr"),
        ("zf_xiaoxiao", None, "cmn"),
        ("af_heart", "xx", "en-us"),  # unknown language: the voice decides
    ],
)
def test_kokoro_espeak_voice(voice, language, expected):
    from engines.kokoro_onnx import espeak_voice_for

    assert espeak_voice_for(voice, language) == expected


def test_kokoro_split_respects_window_and_keeps_text():
    from engines.kokoro_onnx import split_phonemes

    sentence = "hɛloʊ wɜːld, ðɪs ɪz ɐ tɛst. "
    text = (sentence * 60).strip()
    pieces = split_phonemes(text, limit=100)
    assert len(pieces) > 1
    assert all(0 < len(p) <= 100 for p in pieces)
    assert "".join(pieces).replace(" ", "") == text.replace(" ", "")


def test_kokoro_split_slices_one_long_run():
    from engines.kokoro_onnx import split_phonemes

    pieces = split_phonemes("a" * 25, limit=10)
    assert pieces == ["a" * 10, "a" * 10, "a" * 5]


def _fake_urlopen(payloads: dict[str, bytes]):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def urlopen(url, timeout=None):
        return _Resp(payloads[url])

    return urlopen


def test_kokoro_download_verifies_checksum(tmp_path, monkeypatch):
    import engines.kokoro_onnx as ko

    good = {name: f"weights-{name}".encode() for name in ko.MODEL_FILES}
    files = {
        name: (f"https://example.invalid/{name}", hashlib.sha256(data).hexdigest(), len(data))
        for name, data in good.items()
    }
    monkeypatch.setattr(ko, "MODEL_FILES", files)
    urlopen = _fake_urlopen({url: good[name] for name, (url, _, _) in files.items()})

    ko.ensure_model_files(tmp_path, _urlopen=urlopen)
    for name, data in good.items():
        assert (tmp_path / name).read_bytes() == data
        assert not (tmp_path / f"{name}.part").exists()


def test_kokoro_download_discards_a_bad_file(tmp_path, monkeypatch):
    import engines.kokoro_onnx as ko

    files = {"model.onnx": ("https://example.invalid/model.onnx", "0" * 64, 3)}
    monkeypatch.setattr(ko, "MODEL_FILES", files)
    urlopen = _fake_urlopen({"https://example.invalid/model.onnx": b"bad"})

    with pytest.raises(RuntimeError, match="checksum"):
        ko.ensure_model_files(tmp_path, _urlopen=urlopen)
    assert not any(tmp_path.iterdir())


def test_kokoro_rejects_a_language_it_cannot_speak():
    from engines.kokoro_onnx import KokoroOnnxBackend

    with pytest.raises(ValueError):
        KokoroOnnxBackend().generate("Hallo", language="de")


@pytest.fixture
def fake_ptts(monkeypatch):
    import numpy as np

    calls: list[dict] = []

    class TTS:
        sample_rate = 24_000

        def __init__(self, model, **kw):
            calls.append({"init": model, **kw})

        def synth(self, text, **kw):
            calls.append({"text": text, **kw})
            return np.ones(480, dtype=np.float32)

    module = types.SimpleNamespace(TTS=TTS, set_num_threads=lambda n: calls.append({"threads": n}))
    monkeypatch.setitem(sys.modules, "ptts", module)
    return calls


def test_phonon_generates_with_a_preset_voice(fake_ptts):
    from engines.phonon import PhononBackend

    backend = PhononBackend()
    wav = backend.generate("Hello there.", voice="Freya", seed=7)
    assert tuple(wav.shape) == (1, 480)
    init = next(c for c in fake_ptts if "init" in c)
    assert init["init"] == "hf://gradium/phonon-31m-en"
    assert init["lang"] == "en"
    synth = next(c for c in fake_ptts if "text" in c)
    assert synth == {"text": "Hello there.", "voice": "Freya", "seed": 7}


def test_phonon_unknown_voice_falls_back(fake_ptts):
    from engines.phonon import DEFAULT_VOICE, PhononBackend

    PhononBackend().generate("Hi.", voice="Nobody")
    assert next(c for c in fake_ptts if "text" in c)["voice"] == DEFAULT_VOICE


def test_phonon_model_override(fake_ptts, monkeypatch):
    monkeypatch.setenv("OMNIVOICE_PHONON_MODEL", "/models/phonon")
    from engines.phonon import PhononBackend

    assert PhononBackend.configured_identity() == "/models/phonon"
    PhononBackend().generate("Hi.")
    assert next(c for c in fake_ptts if "init" in c)["init"] == "/models/phonon"


def test_phonon_rejects_non_english(fake_ptts):
    from engines.phonon import PhononBackend

    with pytest.raises(ValueError):
        PhononBackend().generate("Bonjour", language="fr")


def test_operator_can_accept_engine_licenses(monkeypatch):
    from services import settings_store

    monkeypatch.setattr(settings_store, "get_text", lambda key, default="0": "0")
    monkeypatch.delenv("OMNIVOICE_ACCEPTED_ENGINE_LICENSES", raising=False)
    assert settings_store.get_license_accepted("supertonic3") is False

    monkeypatch.setenv("OMNIVOICE_ACCEPTED_ENGINE_LICENSES", " supertonic3 , pockettts ")
    assert settings_store.get_license_accepted("supertonic3") is True
    assert settings_store.get_license_accepted("pockettts") is True
    assert settings_store.get_license_accepted("other") is False

    monkeypatch.setenv("OMNIVOICE_ACCEPTED_ENGINE_LICENSES", " , ")
    assert settings_store.get_license_accepted("supertonic3") is False


def test_docker_image_installs_cpu_engine_extras():
    """Railway (CPU-only) relies on these being baked into the image."""
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r'ARG OPTIONAL_EXTRAS="([^"]*)"', dockerfile)
    assert match, "deploy/Dockerfile lost its OPTIONAL_EXTRAS build arg"
    assert set(match.group(1).split(",")) >= {"supertonic", "phonon"}
    assert '".${OPTIONAL_EXTRAS:+[${OPTIONAL_EXTRAS}]}"' in dockerfile

    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    assert extras["phonon"] == ["ptts==0.6.0"]
    assert "supertonic" in extras
