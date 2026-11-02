"""Мостик со старых ярлыков Карен. Они запускают «python -m karen» из её окружения: после переименования такого кода больше нет,
а pythonw падает без единого окна, и человек видел бы просто молчание. Передаём запуск установщику Джарвиса, он всё сделает сам."""
import subprocess
from pathlib import Path

launcher = Path(__file__).resolve().parent.parent / "Jarvis.cmd"
subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/c", str(launcher)], creationflags=subprocess.CREATE_NO_WINDOW)
