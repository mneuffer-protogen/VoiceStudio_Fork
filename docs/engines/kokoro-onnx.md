# VoiceStudio — Kokoro-82M (ONNX) Engine

Kokoro-82M is a small preset-voice model (Apache-2.0 weights) with 54 voices
across eight languages. This engine runs it on onnxruntime, CPU-only, on
Linux, Windows and macOS. Use it for fast, natural narration on a machine with
no GPU. It cannot clone a voice.

## When to pick it

- Narration with a stock voice on a CPU-only host or server.
- You want a commercial-friendly model license (Apache-2.0).

On a 4-vCPU Linux sandbox it rendered a 12.9-second English paragraph in
3.8 seconds (real-time factor about 0.3). More cores make it faster.

For cloning, use [OmniVoice](omnivoice.md) or [PocketTTS](pockettts.md). On
Apple Silicon, [MLX-Audio](mlx-audio.md) also serves Kokoro.

## Setup

Nothing to install: onnxruntime, `phonemizer` and the espeak-ng wheel already
ship with VoiceStudio. Select **Kokoro-82M** in **Model Catalogue** (TTS tab →
**Use**) or set `OMNIVOICE_TTS_BACKEND=kokoro-onnx`.

On first use the engine downloads two files (about 350 MB) from the
[kokoro-onnx release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0)
on GitHub and checks each against a pinned SHA-256. No Hugging Face access is
needed. A file that fails its checksum is discarded, never used.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMNIVOICE_KOKORO_ONNX_DIR` | `<engines dir>/kokoro-onnx` | Where the weights are stored. Put the two files here yourself for an offline install. |
| `OMNIVOICE_KOKORO_ONNX_THREADS` | all usable CPUs | onnxruntime threads. The default respects container CPU limits. |

## Voices

The first letter is the language and the second the gender (`af_` = American
English female). The default is `af_heart`.

| Language | Voices |
| --- | --- |
| American English | `af_heart` `af_alloy` `af_aoede` `af_bella` `af_jessica` `af_kore` `af_nicole` `af_nova` `af_river` `af_sarah` `af_sky` `am_adam` `am_echo` `am_eric` `am_fenrir` `am_liam` `am_michael` `am_onyx` `am_puck` `am_santa` |
| British English | `bf_alice` `bf_emma` `bf_isabella` `bf_lily` `bm_daniel` `bm_fable` `bm_george` `bm_lewis` |
| Spanish | `ef_dora` `em_alex` `em_santa` |
| French | `ff_siwis` |
| Hindi | `hf_alpha` `hf_beta` `hm_omega` `hm_psi` |
| Italian | `if_sara` `im_nicola` |
| Japanese | `jf_alpha` `jf_gongitsune` `jf_nezumi` `jf_tebukuro` `jm_kumo` |
| Brazilian Portuguese | `pf_dora` `pm_alex` `pm_santa` |
| Mandarin Chinese | `zf_xiaobei` `zf_xiaoni` `zf_xiaoxiao` `zf_xiaoyi` `zm_yunjian` `zm_yunxi` `zm_yunxia` `zm_yunyang` |

## Behaviour notes

- Output is 24 kHz mono. Speed is supported from 0.5 to 2.0.
- The request language picks the pronunciation; with no language, the
  voice's own language is used. English keeps a British voice British.
- Text is phonemized with espeak-ng, so unusual words are still spoken.
  Japanese and Chinese use espeak-ng's own rules, which are weaker than
  Kokoro's original dictionaries.
- Long text is split at sentence, clause and word boundaries to fit the
  model's 510-phoneme window.

## Licences

Weights: Apache-2.0 (hexgrad/Kokoro-82M). ONNX export and phoneme vocabulary:
MIT (thewh1teagle/kokoro-onnx). espeak-ng: GPL-3.0, loaded as a library.
