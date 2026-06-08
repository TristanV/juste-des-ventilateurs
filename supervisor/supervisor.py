"""Superviseur de régulation thermique — Juste des Ventilateurs.

Boucle principale de décision :
  1. Lire l'état du cluster via MQTT (ou REST en fallback)
  2. Calculer les features
  3. Évaluer le risque (modèle prédictif)
  4. Décider la consigne RPM (contrôleur)
  5. Envoyer les commandes via REST
  6. Logger décision + résultat observé

Ce module est le point d'entrée Docker : python -m supervisor.supervisor
"""
from __future__ import annotations

import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("supervisor")


def main() -> None:
    api_url = os.getenv("API_BASE_URL", "http://localhost:8000")
    mqtt_host = os.getenv("MQTT_BROKER_HOST", "localhost")
    mqtt_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))
    interval_s = float(os.getenv("DECISION_INTERVAL_S", "5"))
    predictor_model = os.getenv("PREDICTOR_MODEL", "gradient_boosting")
    controller_model = os.getenv("CONTROLLER_MODEL", "score_controller")

    logger.info("=== Juste des Ventilateurs — Superviseur ===")
    logger.info(f"API          : {api_url}")
    logger.info(f"MQTT         : {mqtt_host}:{mqtt_port}")
    logger.info(f"Intervalle   : {interval_s}s")
    logger.info(f"Prédicteur   : {predictor_model}")
    logger.info(f"Contrôleur   : {controller_model}")
    logger.info("---")
    logger.info("Superviseur démarré. En attente d'implémentation des modules.")
    logger.info("Phases suivantes : ingest/ → features/ → models/ → boucle fermée.")

    # Boucle principale (placeholder — sera remplacée par la vraie implémentation)
    try:
        while True:
            logger.info(f"[tick] Cycle de décision — prochaine action dans {interval_s}s")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        logger.info("Arrêt demandé. Supervisor stoppé.")


if __name__ == "__main__":
    main()
