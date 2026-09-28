#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
startup_status() {
  if [[ -t 1 && "${TERM:-dumb}" != dumb ]]; then
    printf '\r\033[2K  · %s' "$1"
  else
    print -r -- "  · $1"
  fi
}
startup_status 'Подготавливаю запуск…'
trap 'print; print "Запуск остановлен. Сохраните сообщение об ошибке и папку private."; if [[ -t 0 ]]; then read "?Нажмите Enter для выхода…"; fi' ZERR
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
if [[ ! -x .venv/bin/python ]]; then
  startup_status 'Первый запуск: создаю окружение…'
  python3.12 -m venv .venv
  startup_status 'Устанавливаю зависимости… Это может занять несколько минут.'
else
  startup_status 'Проверяю зависимости…'
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Нужно окружение Python 3.12; сохраните private и пересоздайте только .venv"'
.venv/bin/python -m pip --disable-pip-version-check install --quiet --require-hashes -r requirements.lock
if [[ -t 1 && "${TERM:-dumb}" != dumb ]]; then
  printf '\r\033[2K'
fi
.venv/bin/python launch.py "$@"
if [[ -t 0 ]]; then read '?Нажмите Enter для выхода…'; print; fi
