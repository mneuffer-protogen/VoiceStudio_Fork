# VoiceStudio — Phonon Engine

Phonon is Gradium's on-device text-to-speech model. The default checkpoint,
Phonon 31m (`gradium/phonon-31m-en`), is English with 37 preset voices, and
its weights, tokenizer and voices are Apache-2.0. It runs through the `ptts`
Rust runtime: no PyTorch, no espeak, CPU-first. It cannot clone a voice.

## When to pick it

- The fastest good-sounding English narration on a CPU. Gradium reports
  about 30x real time on four laptop CPU threads and 1.7% word errors on its
  hard-sentence test set, both better than Kokoro in the same tests.
- A commercial-friendly license for the default model.

## Setup

```bash
uv sync --extra phonon
```

The Docker image installs it by default. Then select **Phonon** in **Model
Catalogue** (TTS tab → **Use**) or set `OMNIVOICE_TTS_BACKEND=phonon`.

The model (about 70 MB) and each voice you use download from Hugging Face on
first use and are cached in the Hugging Face cache. The repository is not
gated, so no token is needed.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMNIVOICE_PHONON_MODEL` | `hf://gradium/phonon-31m-en` | Checkpoint to load: an `hf://owner/name[@revision]` repo or a local folder. |
| `OMNIVOICE_PHONON_THREADS` | all usable CPUs | Runtime threads. The default respects container CPU limits. |

The larger Phonon 40m models are CC-BY-NC (non-commercial) and gated on
Hugging Face. Point `OMNIVOICE_PHONON_MODEL` at one only if that license fits
your use; the voice list below is for the English 31m model.

## Voices

The default is `Easton`.

| Group | Voices |
| --- | --- |
| US English | Easton, Micah, Trevor, Russell, Preston, Miles, Zoey, Juniper, Everly, Harper, Hadley, Delia |
| UK English | Alfie, Hugh, Edwin, Spencer, Toby, Teddy, Maeve, Freya, Pippa, Elodie-Rose, Imogen, Lottie, Bryony |
| Characters | Captain Silas Blackwater, Morwenna of the Deep, Old Mortlake, Azgorath, Fennick the Young Elf, The Thing Under the Stairs, Vyrmathrax the Ancient, Grub the Ogre, Master Aldric, The Snow Queen, Sir Bramwell, Fairy Godmother |

## Behaviour notes

- Output is 24 kHz mono, English only.
- A seed makes a take repeatable; speed is not adjustable.
- Numbers, currency, emails and URLs are spoken out by the runtime's own
  normalizer.

## Licences

Runtime (`ptts`): MIT OR Apache-2.0. Phonon 31m weights and the
`gradium/phonon-voices` catalog: Apache-2.0.
