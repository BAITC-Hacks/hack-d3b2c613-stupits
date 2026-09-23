#!/usr/bin/env bash
# «Аким на 5 часов» — запуск одной командой (Linux / macOS):  bash run.sh
#   1) создаёт виртуальное окружение .venv, если его нет;
#   2) ставит зависимости из requirements.txt и requirements-ui.txt
#      (повторно — только если файлы зависимостей изменились);
#   3) создаёт .env из .env.example, если его нет;
#   4) запускает приложение и открывает http://localhost:8501.
# Настройки (необязательно):
#   PYTHON=python3.12 bash run.sh   — выбрать интерпретатор (нужен Python 3.11–3.14)
#   PORT=8502 bash run.sh           — другой порт
#   NO_BROWSER=1 bash run.sh        — не открывать браузер (сервер без экрана)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8501}"
URL="http://localhost:${PORT}"
VERSION_CHECK='import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)'

venv_python() {
    if [ -x .venv/bin/python ]; then
        echo .venv/bin/python
    elif [ -x .venv/Scripts/python.exe ]; then  # Git Bash на Windows
        echo .venv/Scripts/python.exe
    else
        return 1
    fi
}

find_python() {
    if [ -n "${PYTHON:-}" ]; then
        "$PYTHON" -c "$VERSION_CHECK" 2>/dev/null || echo "Внимание: $PYTHON не из проверенных версий 3.11–3.14." >&2
        echo "$PYTHON"
        return 0
    fi
    for candidate in python3.13 python3.12 python3.11 python3.14 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$VERSION_CHECK" 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# 1. Виртуальное окружение
if VENV_PY="$(venv_python)"; then
    echo "[1/4] Виртуальное окружение .venv уже есть ($("$VENV_PY" --version 2>&1))."
else
    if ! PY="$(find_python)"; then
        echo "Не найден Python 3.11–3.14. Установите его (https://www.python.org/downloads/) и запустите снова." >&2
        exit 1
    fi
    echo "[1/4] Создаю виртуальное окружение .venv ($("$PY" --version 2>&1))..."
    if ! "$PY" -m venv .venv || ! VENV_PY="$(venv_python)"; then
        echo "Не удалось создать .venv. На Debian/Ubuntu установите пакет python3-venv." >&2
        exit 1
    fi
fi

# 2. Зависимости — повторно ставим, только если изменились файлы requirements
cat requirements.txt requirements-ui.txt > .venv/requirements.new
if cmp -s .venv/requirements.new .venv/requirements.installed; then
    echo "[2/4] Зависимости уже установлены."
    rm -f .venv/requirements.new
else
    echo "[2/4] Устанавливаю зависимости, в первый раз это займёт несколько минут..."
    if ! "$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-ui.txt; then
        echo "Не удалось установить зависимости: проверьте интернет и запустите снова." >&2
        exit 1
    fi
    mv .venv/requirements.new .venv/requirements.installed
fi

# 3. Настройки ИИ-советника
if [ -f .env ]; then
    echo "[3/4] Файл .env уже есть."
else
    cp .env.example .env
    echo "[3/4] Создан .env из .env.example: советник работает офлайн, пока в .env не указан ключ OPENAI_API_KEY."
fi

# 4. app.py запускает Python-сервер (ui/web_server.py): HTML-интерфейс и API. Флаг --open открывает браузер.
echo "[4/4] Запускаю приложение: $URL   (остановить — Ctrl+C)"
BROWSER_ARGS=()
if [ -z "${NO_BROWSER:-}" ]; then
    BROWSER_ARGS=(--open)
fi
# ${arr[@]+...} — пустой массив не ломает set -u в bash 3.2 (macOS)
exec "$VENV_PY" -B app.py --host 127.0.0.1 --port "$PORT" ${BROWSER_ARGS[@]+"${BROWSER_ARGS[@]}"}
