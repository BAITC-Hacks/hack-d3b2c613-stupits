"""
check.py должен завершаться с кодом 0, если всё совпало с эталонами ТЗ,
и с ненулевым кодом при любом расхождении — тогда его можно ставить в CI.
Запуск из корня проекта: python -m pytest
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_check(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(ROOT / "check.py"), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)


def test_check_script_passes_on_real_data():
    result = run_check()
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "ИТОГ: все" in result.stdout and "НЕ СОВПАДАЕТ" not in result.stdout


def test_check_script_fails_on_mismatch(tmp_path):
    raw = json.loads((ROOT / "data" / "city_data.json").read_text(encoding="utf-8"))
    raw["reference_checks"]["baseline_score"] = 99.99  # заведомо неверный эталон
    path = tmp_path / "city_data.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    result = run_check(str(path))
    assert result.returncode == 1, result.stdout[-3000:] + result.stderr[-3000:]
    assert "НЕ СОВПАДАЕТ" in result.stdout and "ИТОГ: НЕ СОВПАЛО" in result.stdout
    assert "базовый Score" in result.stdout
