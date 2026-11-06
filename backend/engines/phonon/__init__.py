"""phonon: Gradium's Phonon on-device TTS through the ``ptts`` runtime.

Phonon 31m (``gradium/phonon-31m-en``) is an English preset-voice model whose
weights, tokenizer and voices are Apache-2.0. The ``ptts`` wheel is a Rust
runtime (MIT OR Apache-2.0) with no PyTorch and no espeak, built for CPUs:
Gradium reports about 30x real time on four laptop threads. It loads a q8
GGUF from the Hugging Face Hub on first use and caches it.

The larger Phonon 40m models are CC-BY-NC and gated, so they are not the
default. ``OMNIVOICE_PHONON_MODEL`` can point at one (or at a local folder);
the engine then reports the voices that checkpoint actually has.

No cloning in the open release: voices are preset embeddings from the
``gradium/phonon-voices`` catalog (Apache-2.0), fetched on first use.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Optional

from services.tts_backend import TTSBackend

if TYPE_CHECKING:
    import torch  # noqa: F401

logger = logging.getLogger("omnivoice.engines.phonon")

DEFAULT_MODEL = "hf://gradium/phonon-31m-en"
SAMPLE_RATE = 24_000

#: English voices of the gradium/phonon-voices catalog (US, UK, characters).
PRESET_VOICES: list[str] = [
    # US English
    "Easton", "Micah", "Trevor", "Russell", "Preston", "Miles",
    "Zoey", "Juniper", "Everly", "Harper", "Hadley", "Delia",
    # UK English
    "Alfie", "Hugh", "Edwin", "Spencer", "Toby", "Teddy",
    "Maeve", "Freya", "Pippa", "Elodie-Rose", "Imogen", "Lottie", "Bryony",
    # Character voices
    "Captain Silas Blackwater", "Morwenna of the Deep", "Old Mortlake",
    "Azgorath", "Fennick the Young Elf", "The Thing Under the Stairs",
    "Vyrmathrax the Ancient", "Grub the Ogre", "Master Aldric",
    "The Snow Queen", "Sir Bramwell", "Fairy Godmother",
]
DEFAULT_VOICE = "Easton"


def configured_model() -> str:
    return (os.environ.get("OMNIVOICE_PHONON_MODEL") or "").strip() or DEFAULT_MODEL


class PhononBackend(TTSBackend):
    """Phonon (Gradium) in-process via ``ptts``; CPU-first."""

    id = "phonon"
    display_name = "Phonon 31m (English, 37 preset voices, CPU, Apache-2.0)"
    gpu_compat: tuple[str, ...] = ("cpu",)
    supports_cloning = False  # preset voices only in the open release
    supports_voice_design = False  # instruct is never read (#3413)
    supports_generation_seed = True  # ptts takes the seed per call
    PRESET_VOICES = PRESET_VOICES
    DEFAULT_VOICE = DEFAULT_VOICE
    language_display_names = {"en": "English"}

    def __init__(self) -> None:
        self._tts = None
        self._model = configured_model()
        self._lock = threading.Lock()

    @classmethod
    def configured_identity(cls) -> Optional[str]:
        return configured_model()

    def built_identity(self) -> Optional[str]:
        return self._model

    def model_identity(self) -> Optional[str]:
        return self._model.removeprefix("hf://")

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import ptts  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as e:
            return False, (
                f"ptts package not installed ({e}). Install it with "
                "`uv sync --extra phonon`."
            )
        return True, "ready (CPU; downloads about 70 MB from Hugging Face on first use)"

    @property
    def sample_rate(self) -> int:
        if self._tts is not None:
            return int(self._tts.sample_rate)
        return SAMPLE_RATE

    @property
    def supported_languages(self) -> list[str]:
        return ["en"]

    def _ensure_loaded(self) -> None:
        if self._tts is not None:
            return
        with self._lock:
            if self._tts is not None:
                return
            import ptts  # type: ignore[import-not-found]

            from core.container_limits import effective_cpu_count

            threads = int(os.environ.get("OMNIVOICE_PHONON_THREADS", "0") or 0)
            try:
                ptts.set_num_threads(threads if threads > 0 else effective_cpu_count())
            except Exception:  # older runtimes size their own pool
                logger.debug("phonon: set_num_threads unsupported", exc_info=True)
            logger.info("phonon: loading %s", self._model)
            self._tts = ptts.TTS(self._model, device="cpu", quant="q8", lang="en")

    def generate(self, text: str, **kw) -> "torch.Tensor":
        import numpy as np
        import torch

        self._check_language(kw.get("language"))
        self._ensure_loaded()

        voice = kw.get("voice") or self.DEFAULT_VOICE
        if voice not in self.PRESET_VOICES:
            logger.info("phonon: unknown voice %r, falling back to %r", voice, self.DEFAULT_VOICE)
            voice = self.DEFAULT_VOICE
        seed = kw.get("seed")
        call = {"voice": voice}
        if isinstance(seed, int) and seed >= 0:
            call["seed"] = seed
        with self._lock:  # one generation at a time per loaded model
            audio = self._tts.synth(text, **call)
        wav = np.asarray(audio, dtype=np.float32).ravel()
        if not wav.size:
            raise ValueError("Phonon returned no audio for this text.")
        return torch.from_numpy(wav).unsqueeze(0)


__all__ = ["PhononBackend", "PRESET_VOICES", "DEFAULT_VOICE"]
