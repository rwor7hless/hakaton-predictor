import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "dataset"
for p in ("libs/ndtp", "libs/transit_core", "services/ml", "services/backend", "services/replayer"):
    sys.path.insert(0, str(ROOT / p))

needs_dataset = pytest.mark.skipif(not (DATASET / "validate" / "schedule_plan.csv").exists(),
                                   reason="датасет не найден в data/dataset")
needs_models = pytest.mark.skipif(not (ROOT / "artifacts/models/current/meta.json").exists(),
                                  reason="нет обученных моделей в artifacts/models/current")
