"""Contrôleur baseline : RPM fixe.

Politique la plus simple -- applique un RPM constant sur tous les fans,
quelle que soit la température ou le risque prédit.

Utilisé comme borne inférieure de comparaison.

Niveaux disponibles : 0, 1500, 2500, 3500, 4500 RPM
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# RPM discrets disponibles (identiques à action_class dans les features)
RPM_LEVELS = [0, 1500, 2500, 3500, 4500]


class FixedController:
    """Applique un RPM constant, indépendamment de l'état de la machine."""

    name = "baseline_fixed"

    def __init__(self, rpm: int = 2500):
        if rpm not in RPM_LEVELS:
            raise ValueError(f"rpm={rpm} invalide. Choisir parmi {RPM_LEVELS}")
        self.rpm = rpm

    # ------------------------------------------------------------------
    # Interface commune FanController
    # ------------------------------------------------------------------

    def decide(self, state: pd.Series, risk_score: float = 0.0) -> int:
        """Retourne le RPM cible (constant).

        Args:
            state      : ligne de features (pd.Series) — ignorée
            risk_score : probabilité de panne prédite — ignorée

        Returns:
            int : RPM cible
        """
        return self.rpm

    def decide_batch(self, X: pd.DataFrame, risk_scores: Optional[np.ndarray] = None) -> np.ndarray:
        """Décisions en batch — retourne un tableau de RPM constants."""
        return np.full(len(X), self.rpm, dtype=int)

    def fit(self, X_train: pd.DataFrame, y_train: pd.Series) -> "FixedController":
        """Pas d'entraînement nécessaire."""
        return self

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"rpm": self.rpm}, f)

    @classmethod
    def load(cls, path: str) -> "FixedController":
        with open(path) as f:
            cfg = json.load(f)
        return cls(rpm=cfg["rpm"])

    def __repr__(self) -> str:
        return f"FixedController(rpm={self.rpm})"
