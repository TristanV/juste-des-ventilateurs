"""Logger de décisions — Juste des Ventilateurs.

Enregistre chaque cycle de décision du superviseur dans un fichier JSONL
(une décision JSON par ligne) pour analyse post-hoc.

Format d'une entrée :
{
    "ts":           "2026-06-09T10:00:00Z",   # timestamp UTC
    "machine_id":   "srv-worker-01",
    "temperature_c": 67.2,
    "risk_score":   0.82,
    "rpm_decided":  3500,
    "rpm_previous": 2500,
    "status":       "on",
    "mode":         "ml",                     # "ml" | "threshold" | "native"
    "predictor":    "logistic",
    "controller":   "supervised"
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"


class DecisionLogger:
    """Logger JSONL des décisions du superviseur.

    Parameters
    ----------
    log_dir   : répertoire de sortie des logs
    run_name  : préfixe du fichier log (ex: "benchmark_stress")
    """

    def __init__(
        self,
        log_dir: str | Path = DEFAULT_LOG_DIR,
        run_name: str = "supervisor",
    ) -> None:
        self.log_dir  = Path(log_dir)
        self.run_name = run_name
        self.log_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.log_path = self.log_dir / f"{run_name}_{ts}.jsonl"
        self._fh = open(self.log_path, "w", encoding="utf-8")
        self._count = 0
        logger.info(f"DecisionLogger → {self.log_path}")

    # ------------------------------------------------------------------

    def log(self, entry: dict[str, Any]) -> None:
        """Écrit une entrée de décision."""
        entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self._fh.write(json.dumps(entry, default=str) + "\n")
        self._fh.flush()
        self._count += 1

    def close(self) -> None:
        self._fh.close()
        logger.info(f"DecisionLogger fermé — {self._count} entrées dans {self.log_path}")

    def __enter__(self) -> "DecisionLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------

    @staticmethod
    def load(log_path: str | Path) -> list[dict]:
        """Charge un fichier JSONL de décisions."""
        import pandas as pd  # noqa: F401 — import local pour éviter la dépendance au top
        entries = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    @staticmethod
    def to_dataframe(log_path: str | Path):
        """Charge un fichier JSONL et retourne un DataFrame pandas."""
        import pandas as pd
        entries = DecisionLogger.load(log_path)
        if not entries:
            import pandas as pd
            return pd.DataFrame()
        df = pd.DataFrame(entries)
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df
