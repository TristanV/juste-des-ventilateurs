"""Contrôleur ML supervisé : classification de l'action optimale.

Apprend à reproduire la politique "oracle" encodée dans `action_class`
(générée par features/labeler.py à partir des données simulées).

Classes d'action :
    0 → 0    RPM  (arret fans)
    1 → 1500 RPM  (ralenti)
    2 → 2500 RPM  (modere)
    3 → 3500 RPM  (eleve)
    (4 → 4500 RPM si present dans les donnees)

Features d'entrée : toutes les features du splitter +
    risk_score (probabilite de panne, si fourni)

Modèle : RandomForestClassifier (class_weight=balanced)
         avec StandardScaler en prétraitement.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

ACTION_TO_RPM = {0: 0, 1: 1500, 2: 2500, 3: 3500, 4: 4500}
RPM_LEVELS    = [0, 1500, 2500, 3500, 4500]

# Colonnes à exclure des features (reprises du splitter)
NON_FEATURE_COLS = {
    "timestamp", "cluster_id", "machine_id", "role", "msg_type",
    "status", "fault_types", "fan_modes",
    "failure_30s", "failure_60s", "hot_30s",
    "time_to_failure_s", "optimal_rpm", "action_class",
    "machines_total", "machines_on",
}


def _get_feature_cols(df: pd.DataFrame) -> list[str]:
    cols = [c for c in df.columns if c not in NON_FEATURE_COLS]
    # Garder seulement les colonnes numériques
    return [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]


class SupervisedController:
    """Classifier multiclasse qui apprend la politique oracle (action_class)."""

    name = "supervised_controller"

    def __init__(
        self,
        n_estimators: int = 200,
        max_depth: int = 15,
        random_state: int = 42,
    ):
        self.n_estimators  = n_estimators
        self.max_depth     = max_depth
        self.random_state  = random_state

        self._scaler: Optional[StandardScaler]          = None
        self._clf:    Optional[RandomForestClassifier]  = None
        self._feature_cols: list[str]                   = []
        self._action_to_rpm: dict[int, int]             = dict(ACTION_TO_RPM)

    # ------------------------------------------------------------------
    # Entraînement
    # ------------------------------------------------------------------

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,          # action_class (entiers)
        X_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series]   = None,
    ) -> "SupervisedController":
        """Entraîne le classifier sur action_class.

        Args:
            X_train : features (avec ou sans risk_score)
            y_train : colonne action_class (0-4)
        """
        self._feature_cols = _get_feature_cols(X_train)
        X_tr = X_train[self._feature_cols].fillna(0.0)

        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X_tr)

        # Mapper les classes présentes → RPM
        classes = sorted(y_train.dropna().unique().astype(int))
        self._action_to_rpm = {c: ACTION_TO_RPM.get(c, 0) for c in classes}

        self._clf = RandomForestClassifier(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            class_weight="balanced",
            random_state=self.random_state,
            n_jobs=-1,
        )
        self._clf.fit(X_scaled, y_train.fillna(0).astype(int))

        if X_val is not None and y_val is not None:
            X_v = X_val[self._feature_cols].fillna(0.0)
            X_vs = self._scaler.transform(X_v)
            acc = (self._clf.predict(X_vs) == y_val.fillna(0).astype(int).values).mean()
            print(f"  [SupervisedController] Val accuracy : {acc:.3f}")

        return self

    # ------------------------------------------------------------------
    # Décision
    # ------------------------------------------------------------------

    def _predict_class(self, X: pd.DataFrame) -> np.ndarray:
        if self._clf is None:
            raise RuntimeError("Modele non entraine. Appeler fit() d'abord.")
        cols = [c for c in self._feature_cols if c in X.columns]
        Xf = X[cols].reindex(columns=self._feature_cols, fill_value=0.0).fillna(0.0)
        Xs = self._scaler.transform(Xf)
        return self._clf.predict(Xs)

    def decide(self, state: pd.Series, risk_score: float = 0.0) -> int:
        """Décision sur une seule observation."""
        row = state.to_frame().T.copy()
        if "risk_score" not in row.columns:
            row["risk_score"] = risk_score
        action = int(self._predict_class(row)[0])
        return self._action_to_rpm.get(action, 1500)

    def decide_batch(
        self,
        X: pd.DataFrame,
        risk_scores: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Décisions en batch."""
        Xc = X.copy()
        if risk_scores is not None and "risk_score" not in Xc.columns:
            Xc["risk_score"] = risk_scores
        elif "risk_score" not in Xc.columns:
            Xc["risk_score"] = 0.0
        actions = self._predict_class(Xc)
        return np.array([self._action_to_rpm.get(int(a), 1500) for a in actions], dtype=int)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "clf":             self._clf,
            "scaler":          self._scaler,
            "feature_cols":    self._feature_cols,
            "action_to_rpm":   self._action_to_rpm,
            "n_estimators":    self.n_estimators,
            "max_depth":       self.max_depth,
            "random_state":    self.random_state,
        }
        joblib.dump(payload, path, compress=3)

    @classmethod
    def load(cls, path: str) -> "SupervisedController":
        payload = joblib.load(path)
        obj = cls(
            n_estimators=payload.get("n_estimators", 200),
            max_depth=payload.get("max_depth", 15),
            random_state=payload.get("random_state", 42),
        )
        obj._clf          = payload["clf"]
        obj._scaler       = payload["scaler"]
        obj._feature_cols = payload["feature_cols"]
        obj._action_to_rpm = {int(k): v for k, v in payload["action_to_rpm"].items()}
        return obj

    def __repr__(self) -> str:
        fitted = self._clf is not None
        return f"SupervisedController(n_estimators={self.n_estimators}, fitted={fitted})"
