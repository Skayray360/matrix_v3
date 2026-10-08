# Creado por Aldo Garcia.
"""La configuracion historica no se confunde con capacidad realmente ejecutada."""

import pytest

from app.config import Settings
from scripts.local_model_diagnostics import effective_configuration

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("parallel,retries", [(1, 0), (2, 0), (8, 2), (3, 1)])
def test_legacy_values_remain_visible_without_claiming_runtime_effects(parallel, retries):
    settings = Settings(
        _env_file=None,
        ollama_summary_parallel_batches=parallel,
        llm_max_transient_retries=retries,
    )

    inference = effective_configuration(settings)["inference"]

    assert inference["summary_parallel_batches"] == parallel
    assert inference["transient_retries"] == retries
    assert inference["legacy_configuration_fields"] == ["transient_retries", "summary_parallel_batches"]
    assert inference["effective"] == {
        "transient_retries": 0,
        "summary_parallel_batches": 1,
        "summary_execution": "sequential",
    }
