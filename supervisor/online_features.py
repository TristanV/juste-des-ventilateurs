"""OnlineFeatureBuffer — Juste des Ventilateurs.

Maintient un historique glissant de télémétrie par machine et recalcule
les features temporelles, énergétiques et contextuelles attendues par le
modèle de prédiction de pannes (LogisticPredictor).

Les features produites sont strictement alignées sur celles calculées par
features/temporal.py, features/energy.py et features/contextual.py lors
de l'entraînement hors-ligne, avec les mêmes fenêtres temporelles et les
mêmes formules.

Fenêtres utilisées (tick_hz = 1 Hz par défaut) :
    5s  → 5 ticks
    15s → 15 ticks
    30s → 30 ticks
    60s → 60 ticks

Usage :
    buffer = OnlineFeatureBuffer()
    buffer.update("srv-worker-01", raw_snapshot)
    series = buffer.get_features("srv-worker-01")   # pd.Series prête pour predict_proba
"""
from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constantes (alignées sur features/ et config/base.yaml)
# ---------------------------------------------------------------------------

_TICK_HZ        = 1.0          # fréquence de publication jumeaux-chauds
_T_SHUTDOWN_C   = 88.0         # seuil de shutdown (base.yaml)
_HOT_ZONE_RATIO = 0.80         # seuil zone chaude = 80 % du shutdown
_FAN_MAX_RPM    = 5000         # RPM max (base.yaml)
_FAN_P_WORKER_W = 12.0         # puissance nominale fan worker (W)
_FAN_P_MASTER_W = 15.0         # puissance nominale fan master (W)
_PUE_BASELINE   = 1.40

# Nombre de ticks à conserver (fenêtre max = 60 s × 1 Hz + marge)
_BUFFER_SIZE = 70


class OnlineFeatureBuffer:
    """Buffer glissant de télémétrie temps réel par machine.

    Pour chaque machine, conserve les _BUFFER_SIZE derniers ticks et
    recalcule à la demande l'ensemble des features attendues par le modèle.

    Parameters
    ----------
    tick_hz       : fréquence de publication (défaut 1 Hz)
    t_shutdown_c  : seuil de shutdown thermique (°C)
    buffer_size   : nombre de ticks à conserver
    """

    def __init__(
        self,
        tick_hz: float = _TICK_HZ,
        t_shutdown_c: float = _T_SHUTDOWN_C,
        buffer_size: int = _BUFFER_SIZE,
    ) -> None:
        self._tick_hz      = tick_hz
        self._t_shutdown   = t_shutdown_c
        self._buf_size     = buffer_size
        self._hot_threshold = t_shutdown_c * _HOT_ZONE_RATIO

        # Par machine : deque de dicts (un dict = un tick de télémétrie brute)
        self._history: dict[str, deque[dict[str, Any]]] = {}

        # État cumulatif par machine (ne se réinitialise pas entre les ticks)
        self._nb_shutdowns:  dict[str, int] = {}
        self._nb_degraded:   dict[str, int] = {}
        self._prev_status:   dict[str, str] = {}
        self._ticks_since_shutdown: dict[str, int] = {}
        self._ticks_since_fault:    dict[str, int] = {}
        self._time_in_hot_s:        dict[str, float] = {}
        self._time_in_degraded_s:   dict[str, float] = {}
        self._time_in_off_s:        dict[str, float] = {}
        self._energy_fans_kwh:      dict[str, float] = {}

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def update(self, machine_id: str, snapshot: dict) -> None:
        """Enregistre un nouveau tick de télémétrie pour une machine.

        Parameters
        ----------
        machine_id : identifiant de la machine
        snapshot   : dict snapshot REST de la machine
        """
        if machine_id not in self._history:
            self._history[machine_id] = deque(maxlen=self._buf_size)
            self._nb_shutdowns[machine_id]         = 0
            self._nb_degraded[machine_id]          = 0
            self._prev_status[machine_id]          = "on"
            self._ticks_since_shutdown[machine_id] = 11082  # max observe en train
            self._ticks_since_fault[machine_id]    = 11082
            self._time_in_hot_s[machine_id]        = 0.0
            self._time_in_degraded_s[machine_id]   = 0.0
            self._time_in_off_s[machine_id]        = 0.0
            self._energy_fans_kwh[machine_id]      = 0.0

        raw = self._extract_raw(snapshot)
        self._update_cumulative(machine_id, raw)
        self._history[machine_id].append(raw)

    def get_features(self, machine_id: str) -> pd.Series:
        """Retourne les features calculées pour la dernière observation.

        Si l'historique est vide, retourne des valeurs par défaut sûres.
        """
        if machine_id not in self._history or not self._history[machine_id]:
            return self._default_series()

        hist = list(self._history[machine_id])
        cur  = hist[-1]
        n    = len(hist)

        def _vals(key: str, default: float = 0.0) -> np.ndarray:
            return np.array([h.get(key, default) for h in hist], dtype=float)

        def _tail_mean(arr: np.ndarray, w: int) -> float:
            return float(np.mean(arr[max(0, n - w):]))

        def _tail_std(arr: np.ndarray, w: int) -> float:
            s = arr[max(0, n - w):]
            return float(np.std(s)) if len(s) >= 2 else 0.0

        def _delta(arr: np.ndarray, w: int) -> float:
            if n > w:
                return float(arr[-1] - arr[-(w + 1)])
            return 0.0

        # ---- scalaires courants ----
        temp_c      = float(cur.get("temperature_c", 60.0))
        temp_max    = float(cur.get("sensor_temp_max", temp_c))
        temp_mean   = float(cur.get("sensor_temp_mean", temp_c))
        power_w     = float(cur.get("power_w", 0.0))
        energy_kwh  = float(cur.get("energy_kwh", 0.0))
        rpm_mean    = float(cur.get("fan_rpm_mean", 0.0))
        rpm_std     = float(cur.get("fan_rpm_std", 0.0))
        load        = float(cur.get("load_estimated", 0.5))
        fan_count   = float(cur.get("fan_count", 2))
        fan_mode_manual = int(cur.get("fan_mode_manual", 0))
        status      = str(cur.get("status", "on"))

        # ---- séries temporelles ----
        temps       = _vals("temperature_c", temp_c)
        temps_max   = _vals("sensor_temp_max", temp_c)
        rpms        = _vals("fan_rpm_mean", rpm_mean)
        powers      = _vals("power_w", power_w)
        loads       = _vals("load_estimated", load)

        # ---- features temporelles ----
        w5, w15, w30, w60 = 5, 15, 30, 60

        temp_delta_5s   = _delta(temps, w5)
        temp_delta_15s  = _delta(temps, w15)
        temp_delta_30s  = _delta(temps, w30)
        temp_rm_30s     = _tail_mean(temps, w30)
        temp_rm_60s     = _tail_mean(temps, w60)
        temp_std_30s    = _tail_std(temps, w30)

        margin          = _T_SHUTDOWN_C - temp_c
        margin_pct      = max(0.0, margin / _T_SHUTDOWN_C * 100.0)  # en %, aligné features/temporal.py
        margin_delta_30s = -temp_delta_30s  # positif = danger croissant

        load_rm_30s     = _tail_mean(loads, w30)
        load_rm_60s     = _tail_mean(loads, w60)

        rpm_delta_15s   = _delta(rpms, w15)
        rpm_rm_30s      = _tail_mean(rpms, w30)

        # rpm_variance, rpm_cv (sur fenetre 30s)
        rpms_30 = rpms[max(0, n - w30):]
        rpm_variance = float(np.var(rpms_30)) if len(rpms_30) >= 2 else 0.0
        rpm_cv = (float(np.std(rpms_30) / np.mean(rpms_30))
                  if len(rpms_30) >= 2 and np.mean(rpms_30) > 0 else 0.0)

        # rpm_changes_last_60s : nombre de changements de consigne sur 60 ticks
        rpms_60 = rpms[max(0, n - w60):]
        rpm_changes_last_60s = float(int(np.sum(np.abs(np.diff(rpms_60)) > 50)))  # seuil 50 RPM

        power_rm_30s    = _tail_mean(powers, w30)
        power_delta_30s = _delta(powers, w30)

        sm_delta_15s    = _delta(temps_max, w15)
        sm_rm_30s       = _tail_mean(temps_max, w30)

        # ---- features énergétiques (loi cubique P ∝ RPM³) ----
        fan_p_nom = _FAN_P_MASTER_W if cur.get("role") == "master" else _FAN_P_WORKER_W
        rpm_ratio = min(1.0, rpm_mean / _FAN_MAX_RPM)
        power_fans_w = fan_p_nom * (rpm_ratio ** 3) * fan_count
        power_compute_w = max(0.0, power_w - power_fans_w)
        fan_energy_ratio = float(np.clip(power_fans_w / power_w, 0.0, 1.0)) if power_w > 0 else 0.0  # aligné features/energy.py
        pue_estimated = (1.0 + power_fans_w / power_compute_w) if power_compute_w > 0 else _PUE_BASELINE
        # energy_per_temp_unit = power_fans_w / margin_to_shutdown (aligné features/energy.py)
        energy_per_temp_unit = (power_fans_w / margin) if margin > 0 else 0.0

        # power_fans rolling 30s : recalcul sur la fenêtre
        fan_p_series = np.array([
            fan_p_nom * (min(1.0, h.get("fan_rpm_mean", 0.0) / _FAN_MAX_RPM) ** 3) * fan_count
            for h in hist
        ], dtype=float)
        power_fans_rm_30s = _tail_mean(fan_p_series, w30)

        pue_series = np.array([
            (1.0 + fp / (h.get("power_w", 0.0) - fp))
            if (h.get("power_w", 0.0) - fp) > 0
            else _PUE_BASELINE
            for h, fp in zip(hist, fan_p_series)
        ], dtype=float)
        pue_rm_30s = _tail_mean(pue_series, w30)

        # énergie cumulée fans sur session (depuis démarrage superviseur)
        energy_fans_kwh_cum = self._energy_fans_kwh[machine_id]

        # ---- features contextuelles (état cumulatif) ----
        time_in_hot   = self._time_in_hot_s[machine_id]
        nb_shutdowns  = self._nb_shutdowns[machine_id]
        nb_degraded   = self._nb_degraded[machine_id]
        ticks_shutdown = self._ticks_since_shutdown[machine_id]
        ticks_fault    = self._ticks_since_fault[machine_id]

        has_fan_fault    = int(cur.get("has_fan_fault", 0))
        has_power_surge  = int(cur.get("has_power_surge", 0))
        has_sensor_drift = int(cur.get("has_sensor_drift", 0))
        is_recovering    = int(
            status == "on" and self._prev_status.get(machine_id, "on") in ("off", "degraded")
        )

        # ---- features de statut one-hot ----
        is_on       = int(status == "on")
        is_degraded = int(status == "degraded")
        is_off      = int(status == "off")

        # ---- assemblage final (51 features : 47 + 4 nouvelles features statut) ----
        return pd.Series({
            # scalaires courants
            "temperature_c":                temp_c,
            "sensor_temp_max":              temp_max,
            "sensor_temp_mean":             temp_mean,
            "power_w":                      power_w,
            "energy_kwh":                   energy_kwh,
            "fan_rpm_mean":                 rpm_mean,
            "fan_rpm_std":                  rpm_std,
            "load_estimated":               load,
            "fan_mode_manual":              fan_mode_manual,
            # temporelles
            "temp_delta_5s":                temp_delta_5s,
            "temp_delta_15s":               temp_delta_15s,
            "temp_delta_30s":               temp_delta_30s,
            "temp_rolling_mean_30s":        temp_rm_30s,
            "temp_rolling_mean_60s":        temp_rm_60s,
            "temp_rolling_std_30s":         temp_std_30s,
            "margin_to_shutdown":           margin,
            "margin_pct":                   margin_pct,
            "margin_delta_30s":             margin_delta_30s,
            "load_rolling_mean_30s":        load_rm_30s,
            "load_rolling_mean_60s":        load_rm_60s,
            "rpm_delta_15s":                rpm_delta_15s,
            "rpm_rolling_mean_30s":         rpm_rm_30s,
            "rpm_variance":                 rpm_variance,
            "rpm_cv":                       rpm_cv,
            "rpm_changes_last_60s":         rpm_changes_last_60s,
            "power_rolling_mean_30s":       power_rm_30s,
            "power_delta_30s":              power_delta_30s,
            "power_compute_w":              power_compute_w,
            "sensor_max_delta_15s":         sm_delta_15s,
            "sensor_max_rolling_mean_30s":  sm_rm_30s,
            # energetiques
            "power_fans_w":                 power_fans_w,
            "fan_energy_ratio":             fan_energy_ratio,
            "pue_estimated":                pue_estimated,
            "power_fans_rolling_mean_30s":  power_fans_rm_30s,
            "pue_rolling_mean_30s":         pue_rm_30s,
            "energy_fans_kwh_cumulated":    energy_fans_kwh_cum,
            "energy_per_temp_unit":         energy_per_temp_unit,
            # contextuelles
            "time_in_hot_zone_s":           time_in_hot,
            "nb_shutdowns_episode":         nb_shutdowns,
            "nb_degraded_episode":          nb_degraded,
            "ticks_since_last_shutdown":    ticks_shutdown,
            "has_fan_fault":                has_fan_fault,
            "has_power_surge":              has_power_surge,
            "has_sensor_drift":             has_sensor_drift,
            "ticks_since_last_fault":       ticks_fault,
            "is_recovering":                is_recovering,
            "time_in_degraded_s":           self._time_in_degraded_s[machine_id],
            # statut one-hot (nouvelles features v1.5)
            "is_on":                        is_on,
            "is_degraded":                  is_degraded,
            "is_off":                       is_off,
            "time_in_off_s":                self._time_in_off_s[machine_id],
        })

    def machines(self) -> list[str]:
        """Retourne la liste des machines connues du buffer."""
        return list(self._history.keys())

    # ------------------------------------------------------------------
    # Helpers prives
    # ------------------------------------------------------------------

    def _extract_raw(self, snapshot: dict) -> dict[str, Any]:
        """Extrait les champs scalaires bruts d'un snapshot REST."""
        sensors = snapshot.get("sensors", {})
        fans    = snapshot.get("fans", {})

        if isinstance(fans, list):
            rpms = [f.get("rpm", 0) for f in fans if isinstance(f, dict)]
        else:
            rpms = [v.get("rpm", 0) for v in fans.values() if isinstance(v, dict)]
        rpm_mean = float(np.mean(rpms)) if rpms else 0.0
        rpm_std  = float(np.std(rpms)) if len(rpms) > 1 else 0.0

        def _sensor_val(key_nested: str, key_flat: str, default: float) -> float:
            if key_nested in sensors and isinstance(sensors[key_nested], dict):
                return float(sensors[key_nested].get("temp_c", default))
            return float(sensors.get(key_flat, default))

        temp_c   = float(snapshot.get("temperature_c", 60.0))
        temp_max  = _sensor_val("temp_cpu",     "temp_max",  temp_c)
        temp_mean = _sensor_val("temp_chassis", "temp_mean", temp_c)

        faults = snapshot.get("faults", [])
        fault_types = [f.get("type", "") for f in faults] if faults else []

        fan_mode = str(snapshot.get("fan_mode", "auto"))
        if fan_mode == "auto" and isinstance(fans, list) and fans:
            fan_mode = str(fans[0].get("mode", "auto")) if isinstance(fans[0], dict) else "auto"

        return {
            "temperature_c":    temp_c,
            "sensor_temp_max":  temp_max,
            "sensor_temp_mean": temp_mean,
            "power_w":          float(snapshot.get("power_w", snapshot.get("power_watts", 0.0))),
            "energy_kwh":       float(snapshot.get("energy_kwh", snapshot.get("energy_kwh_cumulated", 0.0))),
            "fan_rpm_mean":     rpm_mean,
            "fan_rpm_std":      rpm_std,
            "fan_count":        float(len(rpms)) if rpms else 2.0,
            "load_estimated":   float(snapshot.get("load_estimated", snapshot.get("load", 0.5))),
            "status":           str(snapshot.get("status", "on")),
            "role":             str(snapshot.get("role", "worker")),
            "fan_mode_manual":  int(fan_mode == "manual"),
            "has_fan_fault":    int(any("fan_failure" in t for t in fault_types)),
            "has_power_surge":  int(any("power_surge" in t for t in fault_types)),
            "has_sensor_drift": int(any("sensor_drift" in t for t in fault_types)),
            "has_fault":        int(len(faults) > 0),
        }

    def _update_cumulative(self, machine_id: str, raw: dict) -> None:
        """Met a jour les compteurs cumulatifs depuis le dernier tick."""
        dt     = 1.0 / self._tick_hz
        status = raw.get("status", "on")
        prev   = self._prev_status[machine_id]

        # Compteurs de transitions
        if status == "off" and prev in ("on", "degraded"):
            self._nb_shutdowns[machine_id] += 1
            self._ticks_since_shutdown[machine_id] = 0
        else:
            self._ticks_since_shutdown[machine_id] = min(
                self._ticks_since_shutdown[machine_id] + 1, 11082
            )

        if status == "degraded" and prev == "on":
            self._nb_degraded[machine_id] += 1

        # Ticks depuis derniere panne
        if raw.get("has_fault", 0):
            self._ticks_since_fault[machine_id] = 0
        else:
            self._ticks_since_fault[machine_id] = min(
                self._ticks_since_fault[machine_id] + 1, 11082
            )

        # Duree en zone chaude (reinitialisee si on sort de la zone)
        temp_c = raw.get("temperature_c", 0.0)
        if temp_c > self._hot_threshold:
            self._time_in_hot_s[machine_id] += dt
        else:
            self._time_in_hot_s[machine_id] = 0.0

        # Duree en mode degrade (reinitialisee si on quitte le mode degrade)
        if status == "degraded":
            self._time_in_degraded_s[machine_id] += dt
        else:
            self._time_in_degraded_s[machine_id] = 0.0

        # Duree en etat "off" (reinitialisee des retour en "on" ou "degraded")
        if status == "off":
            self._time_in_off_s[machine_id] += dt
        else:
            self._time_in_off_s[machine_id] = 0.0

        # Energie fans cumulee (loi cubique)
        fan_p_nom = _FAN_P_MASTER_W if raw.get("role") == "master" else _FAN_P_WORKER_W
        rpm_ratio = min(1.0, raw.get("fan_rpm_mean", 0.0) / _FAN_MAX_RPM)
        fan_count = raw.get("fan_count", 2.0)
        power_fans_w = fan_p_nom * (rpm_ratio ** 3) * fan_count
        self._energy_fans_kwh[machine_id] += power_fans_w * dt / 3600.0

        self._prev_status[machine_id] = status

    @staticmethod
    def _default_series() -> pd.Series:
        """Valeurs par defaut sures (51 features : 47 + 4 statut)."""
        return pd.Series({
            "temperature_c": 60.0, "sensor_temp_max": 60.0, "sensor_temp_mean": 60.0,
            "power_w": 0.0, "energy_kwh": 0.0, "fan_rpm_mean": 0.0,
            "fan_rpm_std": 0.0, "load_estimated": 0.5, "fan_mode_manual": 0,
            "temp_delta_5s": 0.0, "temp_delta_15s": 0.0, "temp_delta_30s": 0.0,
            "temp_rolling_mean_30s": 60.0, "temp_rolling_mean_60s": 60.0,
            "temp_rolling_std_30s": 0.0,
            "margin_to_shutdown": 28.0, "margin_pct": 31.8, "margin_delta_30s": 0.0,
            "load_rolling_mean_30s": 0.5, "load_rolling_mean_60s": 0.5,
            "rpm_delta_15s": 0.0, "rpm_rolling_mean_30s": 0.0,
            "rpm_variance": 0.0, "rpm_cv": 0.0, "rpm_changes_last_60s": 0.0,
            "power_rolling_mean_30s": 0.0, "power_delta_30s": 0.0, "power_compute_w": 0.0,
            "sensor_max_delta_15s": 0.0, "sensor_max_rolling_mean_30s": 60.0,
            "power_fans_w": 0.0, "fan_energy_ratio": 0.0, "pue_estimated": _PUE_BASELINE,
            "power_fans_rolling_mean_30s": 0.0, "pue_rolling_mean_30s": _PUE_BASELINE,
            "energy_fans_kwh_cumulated": 0.0, "energy_per_temp_unit": 0.0,
            "time_in_hot_zone_s": 0.0, "nb_shutdowns_episode": 0,
            "nb_degraded_episode": 0, "ticks_since_last_shutdown": 11082,
            "has_fan_fault": 0, "has_power_surge": 0, "has_sensor_drift": 0,
            "ticks_since_last_fault": 11082, "is_recovering": 0,
            "time_in_degraded_s": 0.0,
            # statut one-hot (nouvelles features v1.5)
            "is_on": 1, "is_degraded": 0, "is_off": 0, "time_in_off_s": 0.0,
        })
