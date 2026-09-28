#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
trap 'print "Запуск остановлен. Сохраните сообщение об ошибке и папку private."; if [[ -t 0 ]]; then read "?Нажмите Enter для выхода…"; fi' ZERR
if ! command -v python3.12 >/dev/null; then
  print 'Установите Python 3.12 с python.org, затем откройте Start.command ещё раз.'
  read '?Нажмите Enter для выхода…'
  exit 1
fi
if ! xcrun --find clang >/dev/null 2>&1; then
  print 'Установите Command Line Tools: xcode-select --install. Затем повторите запуск.'
  read '?Нажмите Enter для выхода…'
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then python3.12 -m venv .venv; fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Нужно окружение Python 3.12; сохраните private и пересоздайте только .venv"'
.venv/bin/python -m pip --disable-pip-version-check install --quiet --require-hashes -r requirements.lock
.venv/bin/python launch.py "$@"
if [[ -t 0 ]]; then read '?Нажмите Enter для выхода…'; fi
