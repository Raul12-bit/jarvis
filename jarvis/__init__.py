"""Джарвис — личный голосовой ИИ-ассистент."""
import os
import sys
from pathlib import Path

__version__ = "5.0.0"

# Проект лежит в папке OneDrive: кэш байткода уносим оттуда, чтобы не синхронизировать мусор.
_base = os.environ.get("LOCALAPPDATA")
if _base and not os.environ.get("JARVIS_HOME"):
    sys.pycache_prefix = str(Path(_base) / "Jarvis" / "pycache")
