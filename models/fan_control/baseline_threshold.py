"""Contrôleur baseline : seuils thermiques.

Politique par paliers : la consigne RPM est déterminée uniquement par la
température courante de la machine, comparée à 3 seuils configurables.

    T > T_high   → RPM = rpm_high  (4500 par défaut)
    T > T_medium → RPM = rpm_med   (3500 par défaut)
    T > T_low    → RPM = rpm_low   (2500 par défaut)
    sinon        → RPM = rpm_idle  (1500 par défaut)

Les seuils sont optimisés par grid search sur un jeu de données labelisé
(minimisation du nombre de shutdowns).
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

RPM_LEVELS = [0, 1500, 2500, 3500, 4500]


class ThresholdFanController:
    """Contrôleur RPM à seuils thermiques fixes."""

    name = "baseline_threshold"

    def __init__(
        self,
        t_low: float = 65.0,
        t_medium: float = 72.0,
        t_high: float = 79.0,
        rpm_idle: int = 1500,
        rpm_low: int = 2500,
        rpm_med: int = 3500,
        rpm_high: int = 4500,
    ):
        self.t_low    = t_low
        self.t_medium = t_medium
        self.t_high   = t_high
        self.rpm_idle = rpm_idle
        self.rpm_low  = rpm_low
        self.rpm_med  = rpm_med
        self.rpm_high = rpm_high

        # Remplis après fit()
        self.best_params_: dict = {}

    # ------------------------------------------------------------------
    # Interface commune FanController
    # ------------------------------------------------------------------

    def decide(self, state: pd.Series, risk_score: float = 0.0) -> int:
        """Décision sur une seule observation."""
        temp = float(state.get("temperature_c", 0.0))
        return self._rpm_for_temp(temp)

    def decide_batch(self, X: pd.DataFrame, risk_scores: Optional[np.ndarray] = None) -> np.ndarray:
        """Décisions en batch."""
        temps = X["temperature_c"].values
        return np.array([self._rpm_for_temp(t) for t in temps], dtype=int)

    def _rpm_for_temp(self, temp: float) -> int:
        if temp > self.t_high:
            return self.rpm_high
        if temp > self.t_medium:
            return self.rpm_med
        if temp > self.t_low:
            return self.rpm_low
        return self.rpm_idle

    # ------------------------------------------------------------------
    # Optimisation des seuils
    # ------------------------------------------------------------------

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        t_low_grid: Optional[list] = None,
        t_medium_grid: Optional[list] = None,
        t_high_grid: Optional[list] = None,
    ) -> "ThresholdFanController":
        """Grid search pour minimiser le taux de shutdowns non anticipés.

        Le label utilisé est `failure_60s` (ou toute colonne binaire
        passée dans y_train). On cherche les seuils qui maximisent le
        Recall sur les cas dangereux tout en minimisant les RPM moyens.

        Score = Recall_failure - 0.1 * mean_normalized_rpm
        """
        if "temperature_c" not in X_train.columns:
            warnings.warn("temperature_c absente — fit ignoré, paramètres par défaut conservés.")
            return self

        t_low_grid    = t_low_grid    or [55.0, 60.0, 65.0, 68.0]
        t_medium_grid = t_medium_grid or [68.0, 72.0, 75.0, 78.0]
        t_high_grid   = t_high_grid   or [75.0, 79.0, 82.0, 85.0]

        best_score = -np.inf
        best_params = {}

        for t_lo in t_low_grid:
            for t_med in t_medium_grid:
                if t_med <= t_lo:
                    continue
                for t_hi in t_high_grid:
                    if t_hi <= t_med:
                        continue

                    temps = X_train["temperature_c"].values
                    rpms  = np.array([
                        self.rpm_high if t > t_hi
                        else self.rpm_med if t > t_med
                        else self.rpm_low if t > t_lo
                        else self.rpm_idle
                        for t in temps
                    ])

                    # Recall sur les cas dangereux (y_train == 1)
                    dangerous = y_train.values == 1
                    if dangerous.sum() == 0:
                        continue
                    # On considère qu'une alerte est émise si RPM >= rpm_med
                    alerted = rpms >= self.rpm_med
                    recall  = (alerted & dangerous).sum() / dangerous.sum()

                    # Pénalité énergie
                    mean_rpm_norm = rpms.mean() / self.rpm_high
                    score = recall - 0.1 * mean_rpm_norm

                    if score > best_score:
                        best_score = score
                        best_params = {"t_low": t_lo, "t_medium": t_med, "t_high": t_hi}

        if best_params:
            self.t_low    = best_params["t_low"]
            self.t_medium = best_params["t_medium"]
            self.t_high   = best_params["t_high"]
            self.best_params_ = {**best_params, "score": best_score}

        return self

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "t_low":    self.t_low,
            "t_medium": self.t_medium,
            "t_high":   self.t_high,
            "rpm_idle": self.rpm_idle,
            "rpm_low":  self.rpm_low,
            "rpm_med":  self.rpm_med,
            "rpm_high": self.rpm_high,
            "best_params": self.best_params_,
        }
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ThresholdFanController":
        with open(path) as f:
            cfg = json.load(f)
        obj = cls(
            t_low=cfg["t_low"],
            t_medium=cfg["t_medium"],
            t_high=cfg["t_high"],
            rpm_idle=cfg.get("rpm_idle", 1500),
            rpm_low=cfg.get("rpm_low", 2500),
            rpm_med=cfg.get("rpm_med", 3500),
            rpm_high=cfg.get("rpm_high", 4500),
        )
        obj.best_params_ = cfg.get("best_params", {})
        return obj

    def __repr__(self) -> str:
        return (
            f"ThresholdFanController("
            f"t_low={self.t_low}, t_medium={self.t_medium}, t_high={self.t_high})"
        )
