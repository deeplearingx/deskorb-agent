"""Canonical paths and loaders for the DeskOrb evaluation datasets."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


E2E_TESTS_ROOT = Path(__file__).resolve().parents[1]
E2E_DATASET_PATH = E2E_TESTS_ROOT / "e2e_task_dataset.json"
E2E_EXPANSIONS_PATH = E2E_TESTS_ROOT / "e2e_task_dataset_expansions.json"
E2E_STEP_BASELINES_PATH = E2E_TESTS_ROOT / "e2e_step_baselines.json"


def load_e2e_matrix_cases(*, dataset_path: Path = E2E_DATASET_PATH,
                          expansions_path: Path = E2E_EXPANSIONS_PATH,
                          expected_count: int = 60) -> list[dict[str, Any]]:
    """Load the complete base-plus-expansion matrix and validate its identity."""
    base = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    expanded = json.loads(Path(expansions_path).read_text(encoding="utf-8"))
    cases = [*(base.get("tasks") or []), *(expanded.get("cases") or [])]
    ids = [str(item.get("id") or "") for item in cases]
    if (len(cases) != expected_count or len(set(ids)) != expected_count
            or any(not case_id for case_id in ids)):
        raise ValueError(f"The evaluation matrix must contain {expected_count} unique cases")
    return cases
