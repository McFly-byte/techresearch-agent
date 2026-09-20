"""Ablation configurations.

Each config toggles ONE component. All configs share the same model, budget,
and sampling seed unless explicitly noted. The `snapshot()` method returns a
dict that is written alongside results so every run is reproducible.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EvalConfig:
    name: str
    # Toggles.
    use_retrieval: bool = True
    use_reflection: bool = True
    use_verifier: bool = True
    use_supervisor: bool = True  # False -> single-agent linear
    use_long_term_memory: bool = False
    # Shared knobs (kept constant across ablations).
    max_workers: int = 2
    max_search_rounds: int = 2
    max_iterations: int = 3
    seed: int = 42
    # Dataset-size guard. Token budgets are enforced by the production runner.
    max_questions_per_run: int = 10
    # Model label (not a key).
    model_label: str = "fake-heuristic"

    def snapshot(self) -> dict:
        import subprocess

        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except Exception:
            commit = "unknown"
        return {
            "config": asdict(self),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git_commit": commit,
            "prompt_version": "eval-v1",
            "code_version": "0.0.0",
        }


# The six ablation configs from the phase 5 spec.
CONFIGS: dict[str, EvalConfig] = {
    "llm_only": EvalConfig(
        name="llm_only",
        use_retrieval=False,
        use_reflection=False,
        use_verifier=False,
        use_supervisor=False,
    ),
    "naive_rag": EvalConfig(
        name="naive_rag",
        use_reflection=False,
        use_verifier=False,
        use_supervisor=False,
    ),
    "full": EvalConfig(name="full"),
    "no_verifier": EvalConfig(name="no_verifier", use_verifier=False),
    "no_reflection": EvalConfig(name="no_reflection", use_reflection=False),
    "single_agent": EvalConfig(name="single_agent", use_supervisor=False),
    "no_long_term_memory": EvalConfig(name="no_long_term_memory", use_long_term_memory=False),
}


__all__ = ["CONFIGS", "EvalConfig"]
