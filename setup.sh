#!/usr/bin/env bash
# Установка студии (Mac или Linux). Запуск: bash setup.sh
set -e
cd "$(dirname "$0")"
if [ "$(uname)" = "Darwin" ]; then
  if ! command -v brew >/dev/null; then
    echo "Ставлю Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
  fi
  brew install ffmpeg python@3.12
  # нужен ffmpeg с libass (субтитры). Если в обычном его нет — ставим полный
  if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass "; then
    brew uninstall --ignore-dependencies ffmpeg || true
    brew install ffmpeg-full && brew link --overwrite ffmpeg-full
  fi
  PYB=$(brew --prefix python@3.12)/bin/python3.12
else
  command -v ffmpeg >/dev/null || (sudo apt-get update && sudo apt-get install -y ffmpeg)
  PYB=python3
fi
mkdir -p Исходники Готово work models
[ -x .venv/bin/python ] || $PYB -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q faster-whisper flask numpy
MODEL=$(.venv/bin/python -c "import json;print(json.load(open('studio/config.json'))['whisper_model'])")
if [ ! -f "models/$MODEL/model.bin" ]; then
  echo "Качаю модель Whisper $MODEL (~3 ГБ)…"
  .venv/bin/python -c "from faster_whisper import download_model as d; d('$MODEL', output_dir='models/$MODEL')"
fi
echo "Готово. Запуск: ./рилс Исходники/видео.mp4"
