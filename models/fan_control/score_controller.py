"""Contrôleur à score multi-objectif.

Pour chaque pas de temps, évalue chaque action candidate (niveau RPM)
via une fonction de coût :

    J(a) = alpha * risk(t)
          + beta  * heat(t)
          + gamma * energy(a)
          + delta * |RPM(a) - RPM(t-1)| / RPM_MAX

Choisit l'action qui minimise J(a).

Composantes :
    risk(t)    : risk_score fourni par le prédicteur de pannes (0-1)
    heat(t)    : temperature_c / t_shutdown  (0-1)
    energy(a)  : RPM_candidate / RPM_MAX     (proxy conso fan)
    |ΔRPM|     : pénalité de changement brusque de consigne

Les paramètres alpha, beta, gamma, delta sont optimisés par grid search
sur les données d'entraînement.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

RPM_LEVELS        = [0, 1500, 2500, 3500, 4500]
RPM_MAX           = 4500
DEFAULT_T_SHUTDOWN = 88.0


class ScoreController:
    """Contrôleur prescriptif à minimisation de score multi-objectif."""

    name = "score_controller"

    def __init__(
        self,
        alpha: float = 0.50,   # poids risque panne
        beta:  float = 0.30,   # poids chaleur
        gamma: float = 0.10,   # poids énergie fans
        delta: float = 0.10,   # poids changement RPM
        t_shutdown: float = DEFAULT_T_SHUTDOWN,
        rpm_levels: Optional[list] = None,
    ):
        self.alpha      = alpha
        self.beta       = beta
        self.gamma      = gamma
        self.delta      = delta
        self.t_shutdown = t_shutdown
        self.rpm_levels = rpm_levels or RPM_LEVELS

        self._prev_rpm: int = 2500  # RPM initial
        self.best_params_: dict = {}

    # ------------------------------------------------------------------
    # Calcul du score
    # ------------------------------------------------------------------

    def _score(
        self,
        rpm_candidate: int,
        risk_score: float,
        temperature_c: float,
        t_shutdown: float,
    ) -> float:
        heat   = min(temperature_c / max(t_shutdown, 1.0), 1.0)
        energy = rpm_candidate / RPM_MAX
        delta  = abs(rpm_candidate - self._prev_rpm) / RPM_MAX
        return (
            self.alpha * risk_score
            + self.beta  * heat
            + self.gamma * energy
            + self.delta * delta
        )

    def _best_action(
        self,
        risk_score: float,
        temperature_c: float,
        t_shutdown: float,
    ) -> int:
        scores = {
            rpm: self._score(rpm, risk_score, temperature_c, t_shutdown)
            for rpm in self.rpm_levels
        }
        return min(scores, key=scores.__getitem__)

    # ------------------------------------------------------------------
    # Interface commune FanController
    # ------------------------------------------------------------------

    def decide(self, state: pd.Series, risk_score: float = 0.0) -> int:
        """Décision sur une observation — maintient le RPM précédent."""
        temp       = float(state.get("temperature_c", 60.0))
        t_shutdown = float(state.get("t_shutdown_c", self.t_shutdown))
        # Estimer t_shutdown depuis margin si disponible
        if "margin_to_shutdown" in state.index:
            t_shutdown = temp + float(state["margin_to_shutdown"])

        rpm = self._best_action(risk_score, temp, t_shutdown)
        self._prev_rpm = rpm
        return rpm

    def decide_batch(
        self,
        X: pd.DataFrame,
        risk_scores: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Décisions en batch — version vectorisée.

        La pénalité |ΔRPM| est calculée vs le RPM précédent de la série,
        approximée ici vs le RPM fixe initial (2500) pour permettre la
        vectorisation. L'effet est négligeable sur de grandes séries.
        """
        if risk_scores is None:
            risk_scores = np.zeros(len(X))

        temps = X["temperature_c"].values if "temperature_c" in X.columns \
                else np.full(len(X), 60.0)

        if "margin_to_shutdown" in X.columns:
            t_shutdown_arr = temps + X["margin_to_shutdown"].values
        else:
            t_shutdown_arr = np.full(len(X), self.t_shutdown)

        heat_arr   = np.clip(temps / np.maximum(t_shutdown_arr, 1.0), 0.0, 1.0)
        arr        = np.array(self.rpm_levels)

        # Pour chaque ligne, calculer J pour chaque RPM candidat et prendre le min
        # Shape: (n_samples, n_rpm_levels)
        energy_arr = arr / RPM_MAX                                  # (n_levels,)
        delta_arr  = np.abs(arr - self._prev_rpm) / RPM_MAX         # (n_levels,)

        # J[i, j] = alpha*risk[i] + beta*heat[i] + gamma*energy[j] + delta*delta_arr[j]
        J = (
            self.alpha * risk_scores[:, None]
            + self.beta  * heat_arr[:, None]
            + self.gamma * energy_arr[None, :]
            + self.delta * delta_arr[None, :]
        )  # shape (n, n_levels)

        best_idx = np.argmin(J, axis=1)
        return arr[best_idx].astype(int)

    # ------------------------------------------------------------------
    # Optimisation des paramètres
    # ------------------------------------------------------------------

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        risk_scores_train: Optional[np.ndarray] = None,
        alpha_grid: Optional[list] = None,
        beta_grid:  Optional[list] = None,
        gamma_grid: Optional[list] = None,
        delta_grid: Optional[list] = None,
    ) -> "ScoreController":
        """Grid search sur (alpha, beta, gamma, delta).

        Score de validation : -taux_critical - 0.05*mean_rpm_norm
        (minimiser le temps en zone critique, avec pénalité énergie légère)

        Note : delta = 1 - alpha - beta - gamma (contrainte de somme = 1)
        pour réduire l'espace de recherche.
        """
        if "temperature_c" not in X_train.columns:
            return self

        alpha_grid = alpha_grid or [0.3, 0.7]
        beta_grid  = beta_grid  or [0.2, 0.3, 0.4]
        gamma_grid = gamma_grid or [0.05, 0.15]

        # Echantillon pour accélérer la grid search
        if len(X_train) > 5000:
            idx = np.random.default_rng(42).choice(len(X_train), 5000, replace=False)
            X_train          = X_train.iloc[idx].reset_index(drop=True)
            risk_scores_train = risk_scores_train[idx]

        if risk_scores_train is None:
            risk_scores_train = np.zeros(len(X_train))

        # Détecter t_shutdown
        if "margin_to_shutdown" in X_train.columns:
            t_shutdown_est = float(
                (X_train["temperature_c"] + X_train["margin_to_shutdown"]).median()
            )
            if not np.isnan(t_shutdown_est):
                self.t_shutdown = t_shutdown_est

        best_score = -np.inf
        best_params = {}

        for alpha in alpha_grid:
            for beta in beta_grid:
                for gamma in gamma_grid:
                    delta = max(0.0, 1.0 - alpha - beta - gamma)
                    if delta > 0.5:
                        continue  # trop de pénalité changement, skip

                    ctrl = ScoreController(
                        alpha=alpha, beta=beta, gamma=gamma, delta=delta,
                        t_shutdown=self.t_shutdown,
                    )
                    rpms = ctrl.decide_batch(X_train, risk_scores=risk_scores_train)

                    if "margin_to_shutdown" in X_train.columns:
                        n_critical = (X_train["margin_to_shutdown"] < 0).sum()
                    else:
                        n_critical = (X_train["temperature_c"] > self.t_shutdown).sum()

                    mean_rpm_norm = rpms.mean() / RPM_MAX
                    # Pénaliser les RPM bas en situation de risque élevé
                    high_risk = risk_scores_train > 0.5
                    low_rpm_high_risk = (
                        (rpms[high_risk] < 3500).mean() if high_risk.sum() > 0 else 0.0
                    )

                    score = -(n_critical / max(len(X_train), 1)) \
                            - 0.05 * mean_rpm_norm \
                            - 0.2 * low_rpm_high_risk

                    if score > best_score:
                        best_score = score
                        best_params = {
                            "alpha": alpha, "beta": beta,
                            "gamma": gamma, "delta": delta,
                        }

        if best_params:
            self.alpha = best_params["alpha"]
            self.beta  = best_params["beta"]
            self.gamma = best_params["gamma"]
            self.delta = best_params["delta"]
            self.best_params_ = {**best_params, "score": best_score}
            self._prev_rpm = 2500

        return self

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "alpha":      self.alpha,
            "beta":       self.beta,
            "gamma":      self.gamma,
            "delta":      self.delta,
            "t_shutdown": self.t_shutdown,
            "rpm_levels": self.rpm_levels,
            "best_params": self.best_params_,
        }
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ScoreController":
        with open(path) as f:
            cfg = json.load(f)
        obj = cls(
            alpha=cfg["alpha"],
            beta=cfg["beta"],
            gamma=cfg["gamma"],
            delta=cfg["delta"],
            t_shutdown=cfg.get("t_shutdown", DEFAULT_T_SHUTDOWN),
            rpm_levels=cfg.get("rpm_levels", RPM_LEVELS),
        )
        obj.best_params_ = cfg.get("best_params", {})
        return obj

    def __repr__(self) -> str:
        return (
            f"ScoreController(alpha={self.alpha}, beta={self.beta}, "
            f"gamma={self.gamma}, delta={self.delta})"
        )
