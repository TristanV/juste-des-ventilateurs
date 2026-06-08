"""Subscriber MQTT — Juste des Ventilateurs.

Collecte la télémétrie publiée par jumeaux-chauds et la transmet
au normalizer pour stockage.

Topics suivis (convention jumeaux-chauds) :
    dt/{cluster}/+/telemetry      QoS 0  — snapshot complet machine (1/s)
    dt/{cluster}/+/status         QoS 1  — changements d'état (on/degraded/off)
    dt/{cluster}/+/fault          QoS 1  — injections et recovery de pannes
    dt/{cluster}/summary          QoS 1  — KPI cluster (toutes les 5s)

Usage :
    # Collecte pendant 10 minutes, export Parquet
    python -m ingest.mqtt_subscriber --duration 600 --episode 001

    # Collecte continue (mode daemon)
    python -m ingest.mqtt_subscriber --continuous
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Callable

import aiomqtt
import httpx
from dotenv import load_dotenv

from ingest.normalizer import Normalizer
from ingest.dataset_exporter import DatasetExporter

load_dotenv()

logger = logging.getLogger(__name__)


class MqttSubscriber:
    """Subscriber MQTT async avec reconnexion automatique.

    Parameters
    ----------
    broker_host : adresse du broker Mosquitto
    broker_port : port (défaut 1883)
    cluster_id  : identifiant du cluster jumeaux-chauds (ex: "cluster_alpha")
    topic_root  : racine des topics (ex: "dt")
    on_message  : callback appelé pour chaque message normalisé
    """

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        cluster_id: str,
        topic_root: str,
        on_message: Callable[[dict], None],
    ) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.cluster_id = cluster_id
        self.topic_root = topic_root
        self.on_message = on_message
        self._stop = asyncio.Event()
        self._normalizer = Normalizer(cluster_id=cluster_id)
        self._stats = {"received": 0, "errors": 0, "last_ts": None}

    def stop(self) -> None:
        """Arrête proprement la boucle de collecte."""
        self._stop.set()

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def _build_topics(self) -> list[str]:
        """Construit la liste des topics à souscrire."""
        root = self.topic_root
        cid = self.cluster_id
        return [
            f"{root}/{cid}/+/telemetry",   # snapshot machine (1/s)
            f"{root}/{cid}/+/status",       # changements d'état
            f"{root}/{cid}/+/fault",        # pannes
            f"{root}/{cid}/summary",        # KPI cluster
        ]

    async def run(self) -> None:
        """Boucle principale avec reconnexion automatique (pattern aiomqtt v2)."""
        topics = self._build_topics()
        logger.info(
            "Connexion MQTT → %s:%s | cluster=%s",
            self.broker_host, self.broker_port, self.cluster_id,
        )
        logger.info("Topics : %s", topics)

        reconnect_delay = 1.0

        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self.broker_host,
                    port=self.broker_port,
                    identifier=f"jdv-subscriber-{os.getpid()}",
                ) as client:
                    # Souscrire à tous les topics
                    for topic in topics:
                        await client.subscribe(topic, qos=1)
                    logger.info("MQTT connecté et souscriptions actives.")
                    reconnect_delay = 1.0  # reset après connexion réussie

                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        await self._handle_message(message)

            except aiomqtt.MqttError as exc:
                if self._stop.is_set():
                    break
                logger.warning(
                    "MQTT déconnecté (%s) — reconnexion dans %.0fs", exc, reconnect_delay
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)  # backoff exponentiel

        logger.info(
            "Subscriber arrêté. Messages reçus: %d, erreurs: %d",
            self._stats["received"], self._stats["errors"],
        )

    async def _handle_message(self, message: aiomqtt.Message) -> None:
        """Parse et normalise un message MQTT entrant."""
        topic = str(message.topic)
        try:
            payload = json.loads(message.payload)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Payload non-JSON sur %s : %s", topic, exc)
            self._stats["errors"] += 1
            return

        self._stats["received"] += 1
        self._stats["last_ts"] = datetime.now(timezone.utc).isoformat()

        # Déterminer le type de message depuis le topic
        parts = topic.split("/")
        msg_type = parts[-1] if parts else "unknown"

        try:
            record = self._normalizer.normalize(
                topic=topic,
                msg_type=msg_type,
                payload=payload,
            )
            if record is not None:
                self.on_message(record)
        except Exception as exc:
            logger.warning("Erreur normalisation topic=%s : %s", topic, exc)
            self._stats["errors"] += 1


# ---------------------------------------------------------------------------
# Récupération des métadonnées depuis l'API jumeaux-chauds
# ---------------------------------------------------------------------------

async def _fetch_cluster_metadata(api_base_url: str) -> dict:
    """Interroge GET /cluster/status pour récupérer les specs machines et le scénario.

    Retourne un dict avec :
      - machines : {machine_id: {t_shutdown_c, t_restart_c, fan_max_rpm, role}}
      - scenario : scénario actif (si exposé)
      - cluster_id : identifiant du cluster
    """
    result: dict = {"machines": {}, "scenario": "unknown", "cluster_id": "unknown"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{api_base_url}/cluster/status")
            resp.raise_for_status()
            data = resp.json()

        result["cluster_id"] = data.get("cluster_id", "unknown")
        result["scenario"] = data.get("scenario", os.getenv("SCENARIO", "unknown"))

        # Extraire les specs thermiques de chaque machine
        for machine_id, machine in data.get("machines", {}).items():
            result["machines"][machine_id] = {
                "role": machine.get("role", "unknown"),
                "t_shutdown_c": _extract_thermal(machine, "t_shutdown_c", 88.0),
                "t_restart_c": _extract_thermal(machine, "t_restart_c", 50.0),
                "fan_max_rpm": _extract_fan(machine, "fan_max_rpm", 5000),
                "fan_count": len(machine.get("fans", [])),
            }
        logger.info(
            "Métadonnées cluster récupérées : %d machines, scénario=%s",
            len(result["machines"]), result["scenario"],
        )
    except Exception as exc:
        logger.warning(
            "Impossible de récupérer les métadonnées cluster depuis %s : %s — "
            "les specs machines seront approximées.",
            api_base_url, exc,
        )
    return result


def _extract_thermal(machine: dict, key: str, default: float) -> float:
    """Cherche une valeur thermique dans le snapshot machine (peut être imbriquée)."""
    # Le snapshot API expose temperature_c et status, mais pas t_shutdown directement.
    # On utilise les valeurs de référence de base.yaml par rôle comme fallback.
    role = machine.get("role", "worker")
    role_defaults = {
        "master": {"t_shutdown_c": 90.0, "t_restart_c": 55.0},
        "worker": {"t_shutdown_c": 88.0, "t_restart_c": 50.0},
    }
    return role_defaults.get(role, {}).get(key, default)


def _extract_fan(machine: dict, key: str, default: int) -> int:
    """Extrait les specs ventilateur depuis le snapshot machine."""
    fans = machine.get("fans", [])
    if key == "fan_max_rpm":
        # fan_max_rpm n'est pas exposé dans le snapshot — valeur fixe de base.yaml
        return 5000
    if key == "fan_count":
        return len(fans)
    return default


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------

async def _run_collection(
    broker_host: str,
    broker_port: int,
    cluster_id: str,
    topic_root: str,
    episode_id: str,
    output_dir: str,
    duration_s: float | None,
    api_base_url: str,
) -> None:
    """Lance la collecte et exporte en Parquet à la fin."""
    # Timestamp réel de début
    ts_start_real = datetime.now(timezone.utc)

    # Récupérer les métadonnées cluster avant de commencer
    cluster_meta = await _fetch_cluster_metadata(api_base_url)

    exporter = DatasetExporter(output_dir=output_dir, episode_id=episode_id)
    records: list[dict] = []

    def on_message(record: dict) -> None:
        records.append(record)
        if len(records) % 100 == 0:
            logger.info("Collecte en cours : %d enregistrements", len(records))

    subscriber = MqttSubscriber(
        broker_host=broker_host,
        broker_port=broker_port,
        cluster_id=cluster_id,
        topic_root=topic_root,
        on_message=on_message,
    )

    # Arrêt sur signal ou après durée
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Signal reçu — arrêt en cours...")
        subscriber.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, OSError):
            pass  # Windows ne supporte pas add_signal_handler

    # Lancement avec timeout optionnel
    if duration_s is not None:
        logger.info("Collecte pendant %.0f secondes...", duration_s)
        try:
            await asyncio.wait_for(subscriber.run(), timeout=duration_s)
        except asyncio.TimeoutError:
            subscriber.stop()
            logger.info("Durée atteinte.")
    else:
        logger.info("Collecte continue (Ctrl+C pour arrêter)...")
        await subscriber.run()

    # Timestamp réel de fin
    ts_end_real = datetime.now(timezone.utc)
    real_duration_s = (ts_end_real - ts_start_real).total_seconds()

    # Export
    if records:
        logger.info("Export de %d enregistrements...", len(records))
        path = exporter.export_parquet(records)
        logger.info("Données exportées : %s", path)

        # Extraire les timestamps simulés depuis les enregistrements collectés
        ts_values = [
            r["timestamp"] for r in records
            if r.get("timestamp") is not None and r.get("msg_type") == "telemetry"
        ]
        ts_sim_start = min(ts_values).isoformat() if ts_values else None
        ts_sim_end = max(ts_values).isoformat() if ts_values else None
        sim_duration_s = (
            (max(ts_values) - min(ts_values)).total_seconds() if len(ts_values) > 1 else None
        )

        exporter.write_metadata(
            scenario=cluster_meta.get("scenario", os.getenv("SCENARIO", "unknown")),
            duration_s=real_duration_s,
            n_records=len(records),
            extra={
                "ts_start_real": ts_start_real.isoformat(),
                "ts_end_real": ts_end_real.isoformat(),
                "ts_sim_start": ts_sim_start,
                "ts_sim_end": ts_sim_end,
                "sim_duration_s": sim_duration_s,
                "machines": cluster_meta.get("machines", {}),
                "cluster_id": cluster_meta.get("cluster_id", cluster_id),
                "broker_host": broker_host,
                "broker_port": broker_port,
            },
        )
        logger.info(
            "Collecte terminée : %.0fs réelles, %.0fs simulées, %d enregistrements.",
            real_duration_s, sim_duration_s or 0, len(records),
        )
    else:
        logger.warning("Aucun enregistrement collecté.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="Subscriber MQTT — Juste des Ventilateurs")
    parser.add_argument("--duration", type=float, default=None,
                        help="Durée de collecte en secondes (défaut: continu)")
    parser.add_argument("--episode", type=str, default="001",
                        help="ID de l'épisode (ex: 001)")
    parser.add_argument("--continuous", action="store_true",
                        help="Collecte continue sans limite de durée")
    parser.add_argument("--output", type=str,
                        default=os.getenv("PARQUET_DATA_DIR", "./data"),
                        help="Répertoire de sortie")
    args = parser.parse_args()

    broker_host = os.getenv("MQTT_BROKER_HOST", "localhost")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))
    cluster_id = os.getenv("CLUSTER_ID", "cluster_alpha")
    topic_root = os.getenv("MQTT_TOPIC_ROOT", "dt")
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
    duration = None if args.continuous else args.duration

    # Sur Windows, la boucle asyncio par défaut (ProactorEventLoop) ne supporte
    # pas add_reader/add_writer utilisés par aiomqtt. On force SelectorEventLoop.
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(_run_collection(
        broker_host=broker_host,
        broker_port=broker_port,
        cluster_id=cluster_id,
        topic_root=topic_root,
        episode_id=args.episode,
        output_dir=args.output,
        duration_s=duration,
        api_base_url=api_base_url,
    ))


if __name__ == "__main__":
    main()
