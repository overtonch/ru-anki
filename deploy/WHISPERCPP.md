# whisper.cpp + CoreML backend (optional, currently *slower* than the default)

The default re-transcription backend is **mlx-whisper** (`app/whisper_rt.py`).
This doc covers the alternate **whisper.cpp + CoreML** backend that was built and
benchmarked on 2026-08-28.

## Verdict

On this machine (**base M1**, 8-core, 16 GB) with `large-v3-turbo`:

| backend | 120 s clip | 600 s clip |
|---|---|---|
| mlx-whisper turbo (default) | 16.6 s (7.2× RT) | **69 s (8.7× RT)** |
| whisper.cpp + CoreML | 19.8 s (6.0× RT) | 100 s (6.0× RT) |

whisper.cpp's CoreML encoder ran ~2.9 s per 30-s window — the base M1's ANE isn't
faster than MLX's Metal turbo path here, and whisper.cpp also produced coarser
(window-level) segments. So it's wired up but **opt-in**, not the default. Worth
re-checking on a machine with a larger ANE (M-series Pro/Max/Ultra) or if the
turbo encoder ever gets a smaller ANE-optimised CoreML model.

## Enable it

```sh
RU_WHISPER_BACKEND=whispercpp
```

(set in the launchd plist's `EnvironmentVariables`, then `launchctl kickstart -k`).
`whisper_rt.transcribe()` falls back to MLX automatically if the binary or model
is missing or the process errors.

## What's installed (outside the repo)

```
~/Library/Application Support/ru-anki/whispercpp/whisper.cpp/
  build/bin/whisper-cli                       # the binary (cmake -DWHISPER_COREML=1)
  models/ggml-large-v3-turbo.bin              # 1.6 GB  ggml weights
  models/ggml-large-v3-turbo-encoder.mlmodelc # 2.4 GB  compiled CoreML encoder
```

Total ~4 GB. To reclaim it: `rm -rf ~/Library/Application\ Support/ru-anki/whispercpp`
(and unset `RU_WHISPER_BACKEND`).

## Rebuild from scratch

Needs: Homebrew, full **Xcode.app** (for `coremlc`), Python **3.12** (coremltools
8.x has no 3.13/3.14 native wheels).

```sh
brew install cmake python@3.12
D=~/Library/Application\ Support/ru-anki/whispercpp
mkdir -p "$D" && cd "$D"
git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp

# binary (CoreML + Metal)
cmake -B build -DWHISPER_COREML=1
cmake --build build -j --config Release

# ggml weights
bash ./models/download-ggml-model.sh large-v3-turbo

# CoreML encoder — isolated venv, pinned to what coremltools 8.2 expects
/opt/homebrew/bin/python3.12 -m venv "$D/convert-venv"
"$D/convert-venv/bin/pip" install "torch==2.5.0" "numpy<2.0" "coremltools==8.2" openai-whisper
"$D/convert-venv/bin/pip" install --no-deps ane_transformers
"$D/convert-venv/bin/python" models/convert-whisper-to-coreml.py \
    --model large-v3-turbo --encoder-only True --optimize-ane True
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
    xcrun coremlc compile models/coreml-encoder-large-v3-turbo.mlpackage models/
rm -rf models/ggml-large-v3-turbo-encoder.mlmodelc
mv models/coreml-encoder-large-v3-turbo.mlmodelc models/ggml-large-v3-turbo-encoder.mlmodelc
rm -rf models/coreml-encoder-large-v3-turbo.mlpackage "$D/convert-venv"
```

First `whisper-cli` run compiles the model for the ANE (~10–20 s, one-time, cached
by the OS).
