"""Конфигурация Sphinx."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("libs/ndtp", "libs/transit_core", "services/ml", "services/backend", "services/replayer"):
    sys.path.insert(0, str(ROOT / p))
os.environ.setdefault("NDTP_PORT", "0")

project = "Предиктор задержек городского транспорта"
author = "Команда хакатона"
release = "1.0.0"
language = "ru"
extensions = ["myst_parser", "sphinx.ext.autodoc", "sphinx.ext.napoleon", "sphinx.ext.viewcode"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
myst_enable_extensions = ["colon_fence", "deflist", "tasklist"]
myst_heading_anchors = 3
autodoc_mock_imports = ["catboost", "torch", "onnxruntime", "onnx"]
autodoc_member_order = "bysource"
autodoc_default_options = {"members": True, "undoc-members": False, "show-inheritance": False}
napoleon_google_docstring = True
html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = project
exclude_patterns = ["_build", "Dockerfile", "contracts.md", "pitch.md"]
suppress_warnings = ["myst.header"]
napoleon_use_ivar = True
