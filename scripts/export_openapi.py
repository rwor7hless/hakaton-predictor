"""Выгрузить OpenAPI-спецификации backend и ML в docs/_static/openapi/."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("libs/ndtp", "libs/transit_core", "services/ml", "services/backend"):
    sys.path.insert(0, str(ROOT / p))
os.environ.setdefault("NDTP_PORT", "0")

from backend.api import app as backend_app  # noqa: E402
from mlcore.api import app as ml_app  # noqa: E402
from replayer.app import app as replayer_app  # noqa: E402

out = ROOT / "docs/_static/openapi"
out.mkdir(parents=True, exist_ok=True)
for name, app in (("backend", backend_app), ("ml", ml_app), ("replayer", replayer_app)):
    (out / f"{name}.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
    print(name, len(app.openapi()["paths"]), "paths")
