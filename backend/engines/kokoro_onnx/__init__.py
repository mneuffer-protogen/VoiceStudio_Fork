"""kokoro-onnx: Kokoro-82M on onnxruntime, CPU-only, no PyTorch at inference.

Kokoro-82M (hexgrad, Apache-2.0) is a small preset-voice TTS model with 54
voices across eight languages. It has no cloning. It is here for the "fast,
good-sounding narration on a CPU-only host" job: measured at an RTF of about
0.3 on four vCPUs (a 12.9 s paragraph in 3.8 s).

The weights come from the kokoro-onnx project's GitHub release (MIT runner,
Apache-2.0 weights), pinned by SHA-256 and downloaded on first use into the
managed engines folder, so a Hugging Face mirror or token is not needed.

Phonemization uses the ``phonemizer`` module and the ``espeakng_loader``
wheel that KittenTTS already brings in, routed through
``utils.espeak_data.ensure_espeak_data_path`` so a bad data path raises a
normal error instead of killing the process (#3367). The phoneme vocabulary
in ``vocab.json`` is copied from kokoro-onnx 0.6.1 (MIT, thewh1teagle).

The ``mlx-audio`` engine also serves Kokoro, but only on Apple Silicon; the
sherpa-onnx Kokoro path silently drops words missing from its lexicon. This
engine works the same on Linux, Windows and macOS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from services.tts_backend import TTSBackend

if TYPE_CHECKING:
    import torch  # noqa: F401

logger = logging.getLogger("omnivoice.engines.kokoro_onnx")

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

#: Files fetched on first use: name -> (url, sha256, approximate bytes).
#: fp32 rather than int8: on x86 CPUs the fp32 graph measured faster (RTF 0.30
#: vs 0.38 on four vCPUs) and int8 costs some quality.
MODEL_FILES: dict[str, tuple[str, str, int]] = {
    "kokoro-v1.0.onnx": (
        f"{_RELEASE}/kokoro-v1.0.onnx",
        "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        325_532_387,
    ),
    "voices-v1.0.bin": (
        f"{_RELEASE}/voices-v1.0.bin",
        "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        28_214_398,
    ),
}

SAMPLE_RATE = 24_000
#: The model's context, in phonemes (kokoro-onnx MAX_PHONEME_LENGTH).
MAX_PHONEMES = 510

#: The voice-name prefix letter is the voice's language. Values are the
#: espeak-ng voice used to phonemize text for that voice.
_PREFIX_ESPEAK = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
_LANG_ESPEAK = {
    "en": "en-us",
    "es": "es",
    "fr": "fr-fr",
    "hi": "hi",
    "it": "it",
    "ja": "ja",
    "pt": "pt-br",
    "zh": "cmn",
}

PRESET_VOICES: list[str] = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
]
DEFAULT_VOICE = "af_heart"

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE = re.compile(r"(?<=[,;:])\s+")
_SPACE = re.compile(r"\s+")


def model_dir() -> Path:
    """Where the weights live: ``OMNIVOICE_KOKORO_ONNX_DIR`` or the engines folder."""
    custom = (os.environ.get("OMNIVOICE_KOKORO_ONNX_DIR") or "").strip()
    if custom:
        return Path(custom)
    from core.config import get_engines_dir

    return Path(get_engines_dir()) / "kokoro-onnx"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model_files(target: Optional[Path] = None, *, _urlopen=urllib.request.urlopen) -> Path:
    """Download any missing weight file, verify its SHA-256, return the folder.

    A file is written to ``<name>.part`` and renamed only after its hash
    matches, so an interrupted or tampered download never looks installed.
    """
    target = target or model_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name, (url, sha, _size) in MODEL_FILES.items():
        final = target / name
        if final.is_file():
            continue
        part = target / f"{name}.part"
        logger.info("kokoro-onnx: downloading %s", url)
        digest = hashlib.sha256()
        with _urlopen(url, timeout=60) as resp, part.open("wb") as out:
            for block in iter(lambda: resp.read(1 << 20), b""):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise RuntimeError(
                f"kokoro-onnx: {name} failed its checksum (expected {sha}, "
                f"got {digest.hexdigest()}). The download was discarded."
            )
        part.replace(final)
    return target


def split_phonemes(phonemes: str, limit: int = MAX_PHONEMES) -> list[str]:
    """Cut a phoneme string into pieces of at most ``limit`` characters.

    Cuts at sentence ends first, then clause marks, then spaces, and only
    slices mid-word for a single run longer than the limit. Pieces are then
    packed greedily back together up to the limit.
    """
    def atoms(text: str, level: int) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= limit:
            return [text]
        for index, pattern in enumerate((_SENTENCE, _CLAUSE, _SPACE)[level:], start=level):
            parts = pattern.split(text)
            if len(parts) > 1:
                out: list[str] = []
                for part in parts:
                    out.extend(atoms(part, index + 1))
                return out
        return [text[i:i + limit] for i in range(0, len(text), limit)]

    batches: list[str] = []
    for atom in atoms(phonemes, 0):
        if batches and len(batches[-1]) + 1 + len(atom) <= limit:
            batches[-1] = f"{batches[-1]} {atom}"
        else:
            batches.append(atom)
    return batches


def espeak_voice_for(voice: str, language: Optional[str]) -> str:
    """The espeak-ng voice to phonemize with.

    The caller's language wins when it is one Kokoro speaks; English keeps a
    British voice British. Otherwise the voice's own language is used.
    """
    by_voice = _PREFIX_ESPEAK.get(voice[:1], "en-us")
    if language:
        lang = _LANG_ESPEAK.get(language)
        if lang:
            if language == "en" and by_voice.startswith("en"):
                return by_voice
            return lang
    return by_voice


class KokoroOnnxBackend(TTSBackend):
    """Kokoro-82M through onnxruntime on the CPU."""

    id = "kokoro-onnx"
    display_name = "Kokoro-82M (8 langs, 54 preset voices, CPU ONNX, Apache-2.0)"
    gpu_compat: tuple[str, ...] = ("cpu",)
    supports_cloning = False  # preset voices only; ref_audio is ignored
    supports_voice_design = False  # instruct is never read (#3413)
    PRESET_VOICES = PRESET_VOICES
    DEFAULT_VOICE = DEFAULT_VOICE
    language_display_names = {
        "en": "English", "es": "Spanish", "fr": "French", "hi": "Hindi",
        "it": "Italian", "ja": "Japanese", "pt": "Portuguese", "zh": "Chinese",
    }

    def __init__(self) -> None:
        self._session = None
        self._voices = None
        self._vocab: dict[str, int] = {}
        self._phonemizers: dict[str, object] = {}
        self._phonemize_lock = threading.Lock()
        self._load_lock = threading.Lock()

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import onnxruntime  # noqa: F401
            import phonemizer  # noqa: F401
        except ImportError as e:
            return False, f"kokoro-onnx needs onnxruntime and phonemizer: {e}"
        try:
            import espeakng_loader  # noqa: F401
        except ImportError as e:
            return False, f"kokoro-onnx needs the espeakng-loader package: {e}"
        if all((model_dir() / name).is_file() for name in MODEL_FILES):
            return True, "ready (CPU-only via onnxruntime)"
        return True, "ready (CPU-only; downloads about 350 MB from GitHub on first use)"

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def supported_languages(self) -> list[str]:
        return list(_LANG_ESPEAK)

    def model_identity(self) -> Optional[str]:
        return "kokoro-v1.0"

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._load_lock:
            if self._session is not None:
                return
            import numpy as np
            import onnxruntime as ort

            from core.container_limits import effective_cpu_count
            from utils.espeak_data import ensure_espeak_data_path

            folder = ensure_model_files()
            ensure_espeak_data_path()
            vocab_path = Path(__file__).with_name("vocab.json")
            self._vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
            self._voices = np.load(folder / "voices-v1.0.bin")
            opts = ort.SessionOptions()
            threads = int(os.environ.get("OMNIVOICE_KOKORO_ONNX_THREADS", "0") or 0)
            opts.intra_op_num_threads = threads if threads > 0 else effective_cpu_count()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                str(folder / "kokoro-v1.0.onnx"), sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            inputs = {i.name: i for i in session.get_inputs()}
            self._tokens_input = "input_ids" if "input_ids" in inputs else "tokens"
            self._speed_is_int = "int" in (inputs.get("speed").type if "speed" in inputs else "")
            self._session = session
            logger.info("kokoro-onnx: loaded with %d threads", opts.intra_op_num_threads)

    def _phonemize(self, text: str, espeak_voice: str) -> str:
        from phonemizer.backend import EspeakBackend

        with self._phonemize_lock:
            backend = self._phonemizers.get(espeak_voice)
            if backend is None:
                backend = EspeakBackend(
                    espeak_voice, preserve_punctuation=True, with_stress=True,
                )
                self._phonemizers[espeak_voice] = backend
            phonemes = backend.phonemize([text], strip=True)[0]
        phonemes = " ".join(phonemes.split())
        return "".join(p for p in phonemes if p in self._vocab).strip()

    def _infer(self, phonemes: str, style_bank, speed: float):
        import numpy as np

        tokens = [self._vocab[p] for p in phonemes if p in self._vocab]
        if not tokens:
            return np.zeros(0, dtype=np.float32)
        style = style_bank[min(len(tokens), len(style_bank)) - 1]
        feed = {
            self._tokens_input: np.array([[0, *tokens, 0]], dtype=np.int64),
            "style": np.asarray(style, dtype=np.float32),
            "speed": np.array(
                [max(1, round(speed)) if self._speed_is_int else speed],
                dtype=np.int32 if self._speed_is_int else np.float32,
            ),
        }
        return np.asarray(self._session.run(None, feed)[0], dtype=np.float32).ravel()

    def generate(self, text: str, **kw) -> "torch.Tensor":
        import numpy as np
        import torch

        language = self._check_language(kw.get("language"))
        self._ensure_loaded()

        voice = kw.get("voice") or self.DEFAULT_VOICE
        if voice not in self.PRESET_VOICES:
            logger.info(
                "kokoro-onnx: unknown voice %r, falling back to %r", voice, self.DEFAULT_VOICE,
            )
            voice = self.DEFAULT_VOICE
        speed = max(0.5, min(2.0, float(kw.get("speed", 1.0) or 1.0)))

        phonemes = self._phonemize(text, espeak_voice_for(voice, language))
        if not phonemes:
            raise ValueError("Nothing to say: the text produced no speakable sounds.")
        style_bank = self._voices[voice]
        gap = np.zeros(int(0.15 * SAMPLE_RATE), dtype=np.float32)
        parts: list = []
        for piece in split_phonemes(phonemes):
            audio = self._infer(piece, style_bank, speed)
            if audio.size:
                if parts:
                    parts.append(gap)
                parts.append(audio)
        if not parts:
            raise ValueError("Nothing to say: the text produced no speakable sounds.")
        return torch.from_numpy(np.concatenate(parts)).unsqueeze(0)


__all__ = ["KokoroOnnxBackend", "ensure_model_files", "split_phonemes", "espeak_voice_for"]
