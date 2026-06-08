"""Normalizer — Juste des Ventilateurs.

Transforme les payloads bruts MQTT de jumeaux-chauds en un schéma unifié
exploitable pour le feature engineering et le stockage.

Topics gérés :
    .../telemetry   → enregistrement complet (source principale)
    .../status      → enrichissement de l'enregistrement courant
    .../fault       → enrichissement pannes
    .../summary     → métriques cluster (stocké séparément)

Le schéma de sortie est documenté dans data/schema.md.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Regexp pour extraire machine_id depuis le topic
# ex: dt/cluster_alpha/srv-worker-01/telemetry → srv-worker-01
_TOPIC_RE = re.compile(r"^[^/]+/[^/]+/([^/]+)/(.+)$")


class Normalizer:
    """Normalise les payloads MQTT vers le schéma unifié.

    Parameters
    ----------
    cluster_id : identifiant du cluster (ex: "cluster_alpha")
    """

    def __init__(self, cluster_id: str) -> None:
        self.cluster_id = cluster_id

    def normalize(
        self,
        topic: str,
        msg_type: str,
        payload: dict,
    ) -> dict | None:
        """Normalise un message entrant.

        Retourne un dict conforme au schéma unifié, ou None si le message
        doit être ignoré (ex: topic summary non machine-level).
        """
        if msg_type == "telemetry":
            return self._normalize_telemetry(topic, payload)
        elif msg_type == "status":
            return self._normalize_status(topic, payload)
        elif msg_type == "fault":
            return self._normalize_fault(topic, payload)
        elif msg_type == "summary":
            # Métriques cluster — on les stocke mais sans machine_id
            return self._normalize_summary(payload)
        else:
            logger.debug("Topic ignoré : %s (type=%s)", topic, msg_type)
            return None

    # ------------------------------------------------------------------
    # Normalisation par type de message
    # ------------------------------------------------------------------

    def _normalize_telemetry(self, topic: str, payload: dict) -> dict | None:
        """Normalise un snapshot complet de machine (topic .../telemetry).

        C'est la source principale de données — appelée ~1/s par machine.
        """
        machine_id = self._extract_machine_id(topic)
        if machine_id is None:
            return None

        ts = self._parse_ts(payload.get("ts"))

        # Fans : calcul mean et std
        fans: list[dict] = payload.get("fans", [])
        fan_rpms = [f.get("rpm", 0) for f in fans]
        fan_modes = ",".join(f.get("mode", "auto") for f in fans)
        fan_rpm_mean = float(sum(fan_rpms) / len(fan_rpms)) if fan_rpms else 0.0
        fan_rpm_std = _std(fan_rpms)
        fan_count = len(fans)

        # Sensors : temp max et mean sur toutes les sondes
        sensors: dict[str, dict] = payload.get("sensors", {})
        sensor_temps = [s.get("temp_c", 0.0) for s in sensors.values()]
        sensor_temp_max = max(sensor_temps) if sensor_temps else payload.get("temperature_c", 0.0)
        sensor_temp_mean = (sum(sensor_temps) / len(sensor_temps)) if sensor_temps else payload.get("temperature_c", 0.0)

        # Pannes actives
        faults: list[dict] = payload.get("faults", [])
        has_fault = len(faults) > 0
        fault_types = ",".join(f.get("type", "") for f in faults) if faults else ""

        # Charge estimée depuis power_w (normalisée entre 0 et 1)
        power_w = float(payload.get("power_w", 0.0))
        load_estimated = self._estimate_load(power_w, payload.get("role", "worker"))

        return {
            # Identifiants
            "timestamp": ts,
            "cluster_id": self.cluster_id,
            "machine_id": machine_id,
            "role": payload.get("role", "unknown"),
            "msg_type": "telemetry",
            # État
            "status": payload.get("status", "unknown"),
            # Thermique
            "temperature_c": float(payload.get("temperature_c", 0.0)),
            "sensor_temp_max": round(sensor_temp_max, 3),
            "sensor_temp_mean": round(sensor_temp_mean, 3),
            # Énergie
            "power_w": round(power_w, 3),
            "energy_kwh": float(payload.get("energy_kwh_cumulated", 0.0)),
            # Ventilateurs
            "fan_count": fan_count,
            "fan_rpm_mean": round(fan_rpm_mean, 1),
            "fan_rpm_std": round(fan_rpm_std, 1),
            "fan_modes": fan_modes,
            # Charge
            "load_estimated": round(load_estimated, 3),
            # Pannes
            "has_fault": has_fault,
            "fault_types": fault_types,
            "fault_count": len(faults),
        }

    def _normalize_status(self, topic: str, payload: dict) -> dict | None:
        """Normalise un événement de changement d'état (topic .../status)."""
        machine_id = self._extract_machine_id(topic)
        if machine_id is None:
            return None

        return {
            "timestamp": self._parse_ts(payload.get("ts")),
            "cluster_id": self.cluster_id,
            "machine_id": machine_id,
            "role": "unknown",
            "msg_type": "status_event",
            "status": payload.get("status", "unknown"),
            "status_cause": payload.get("cause", "unknown"),
            # Champs vides (non disponibles dans ce type de message)
            "temperature_c": None,
            "sensor_temp_max": None,
            "sensor_temp_mean": None,
            "power_w": None,
            "energy_kwh": None,
            "fan_count": None,
            "fan_rpm_mean": None,
            "fan_rpm_std": None,
            "fan_modes": None,
            "load_estimated": None,
            "has_fault": None,
            "fault_types": None,
            "fault_count": None,
        }

    def _normalize_fault(self, topic: str, payload: dict) -> dict | None:
        """Normalise un événement de panne (topic .../fault)."""
        machine_id = self._extract_machine_id(topic)
        if machine_id is None:
            return None

        fault_event = payload.get("event", "unknown")  # "injected" | "recovered"
        fault_type = payload.get("type", payload.get("fault_type", "unknown"))
        magnitude = float(payload.get("magnitude", 0.0))

        return {
            "timestamp": self._parse_ts(payload.get("ts")),
            "cluster_id": self.cluster_id,
            "machine_id": machine_id,
            "role": "unknown",
            "msg_type": "fault_event",
            "status": "unknown",
            "fault_event": fault_event,
            "fault_type_event": fault_type,
            "fault_magnitude": magnitude,
            # Champs non disponibles
            "temperature_c": None,
            "sensor_temp_max": None,
            "sensor_temp_mean": None,
            "power_w": None,
            "energy_kwh": None,
            "fan_count": None,
            "fan_rpm_mean": None,
            "fan_rpm_std": None,
            "fan_modes": None,
            "load_estimated": None,
            "has_fault": True,
            "fault_types": fault_type,
            "fault_count": 1,
        }

    def _normalize_summary(self, payload: dict) -> dict | None:
        """Normalise le résumé cluster (topic .../summary).

        Retourne un enregistrement de niveau cluster (machine_id = None).
        """
        return {
            "timestamp": self._parse_ts(payload.get("ts")),
            "cluster_id": self.cluster_id,
            "machine_id": None,
            "role": "cluster",
            "msg_type": "cluster_summary",
            "status": None,
            "machines_total": payload.get("machines_total"),
            "machines_on": payload.get("machines_on"),
            "temperature_c": None,
            "sensor_temp_max": payload.get("t_max_c"),
            "sensor_temp_mean": None,
            "power_w": payload.get("power_total_w"),
            "energy_kwh": None,
            "fan_count": None,
            "fan_rpm_mean": None,
            "fan_rpm_std": None,
            "fan_modes": None,
            "load_estimated": None,
            "has_fault": None,
            "fault_types": None,
            "fault_count": None,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_machine_id(self, topic: str) -> str | None:
        """Extrait le machine_id depuis le topic MQTT."""
        m = _TOPIC_RE.match(topic)
        if m:
            return m.group(1)
        logger.debug("Impossible d'extraire machine_id depuis : %s", topic)
        return None

    @staticmethod
    def _parse_ts(ts_str: Any) -> datetime:
        """Parse un timestamp ISO 8601 vers datetime UTC.

        Fallback vers l'heure courante si invalide.
        """
        if isinstance(ts_str, str):
            try:
                # Gérer le format avec ou sans Z
                ts_str = ts_str.replace("Z", "+00:00")
                return datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass
        return datetime.now(timezone.utc)

    @staticmethod
    def _estimate_load(power_w: float, role: str) -> float:
        """Estime la charge (0..1) depuis la puissance consommée.

        Utilise les valeurs idle/max de base.yaml par rôle.
        """
        # Valeurs de référence depuis base.yaml
        thresholds = {
            "master": {"idle": 200.0, "max": 1700.0},
            "worker": {"idle": 100.0, "max": 1450.0},
        }
        t = thresholds.get(role, {"idle": 100.0, "max": 1500.0})
        idle, max_w = t["idle"], t["max"]
        if max_w <= idle:
            return 0.0
        load = (power_w - idle) / (max_w - idle)
        return max(0.0, min(1.0, load))


def _std(values: list[float]) -> float:
    """Écart-type population."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return variance ** 0.5
