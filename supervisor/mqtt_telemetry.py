"""MqttTelemetryConsumer — Juste des Ventilateurs.

Subscriber asyncio sur dt/{cluster}/+/telemetry.
Normalise chaque payload et alimente le OnlineFeatureBuffer à la cadence
de la simulation (events_per_sec, indépendant de la vitesse réelle).

Pourquoi MQTT plutôt que REST :
    Le superviseur Phase 6 lisait GET /cluster/status toutes les 5 secondes
    réelles. À speed=1x cela correspond à 5 secondes simulées : les fenêtres
    glissantes (temp_delta_5s…30s) étaient 5× trop larges. À speed=60x la
    divergence atteint 300×, rendant le prédicteur aveugle.
    Ce consumer reçoit 1 message/seconde simulée par machine, quelle que soit
    la vitesse : le buffer accumule toujours les bonnes fenêtres temporelles.

Sous-échantillonnage des décisions :
    Le buffer est alimenté à chaque tick MQTT. La décision (predict → command)
    n'est déclenchée que tous les `decision_interval_ticks` ticks simulés
    (défaut 5 = toutes les 5 secondes simulées). Ce compteur est géré ici et
    exposé via `ticks_since_last_decision`.

Fallback :
    Si le broker MQTT est indisponible au démarrage ou se déconnecte, le
    consumer logue un warning et laisse le superviseur fonctionner en mode
    REST (comportement Phase 6). Il tente une reconnexion toutes les 5s.

Usage :
    consumer = MqttTelemetryConsumer(buffer, cluster_id="cluster_alpha")
    asyncio.create_task(consumer.run())          # démarre en arrière-plan
    await consumer.wait_ready(timeout=10.0)      # attend première connexion
    ...
    consumer.stop()                              # arrêt propre
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supervisor.online_features import OnlineFeatureBuffer

logger = logging.getLogger("supervisor.mqtt")

# ---------------------------------------------------------------------------
# Constantes par défaut
# ---------------------------------------------------------------------------

_DEFAULT_BROKER_HOST       = os.environ.get("MQTT_BROKER_HOST", "localhost")
_DEFAULT_BROKER_PORT       = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
_DEFAULT_CLUSTER_ID        = os.environ.get("CLUSTER_ID", "cluster_alpha")
_DEFAULT_TOPIC_ROOT        = os.environ.get("MQTT_TOPIC_ROOT", "dt")
_RECONNECT_DELAY_S         = 5.0
_DEFAULT_DECISION_INTERVAL = int(os.environ.get("DECISION_INTERVAL_TICKS", "5"))


class MqttTelemetryConsumer:
    """Subscriber asyncio sur la télémétrie MQTT de jumeaux-chauds.

    Parameters
    ----------
    buffer               : OnlineFeatureBuffer à alimenter
    cluster_id           : identifiant du cluster (défaut: cluster_alpha)
    broker_host          : hostname du broker MQTT
    broker_port          : port du broker MQTT
    topic_root           : racine des topics (défaut: dt)
    decision_interval_ticks : déclenche une décision tous les N ticks
    """

    def __init__(
        self,
        buffer: "OnlineFeatureBuffer",
        cluster_id: str = _DEFAULT_CLUSTER_ID,
        broker_host: str = _DEFAULT_BROKER_HOST,
        broker_port: int = _DEFAULT_BROKER_PORT,
        topic_root: str = _DEFAULT_TOPIC_ROOT,
        decision_interval_ticks: int = _DEFAULT_DECISION_INTERVAL,
    ) -> None:
        self._buffer    = buffer
        self._cluster   = cluster_id
        self._host      = broker_host
        self._port      = broker_port
        self._root      = topic_root
        self._decision_interval = decision_interval_ticks

        # État interne
        self._ready     = asyncio.Event()
        self._stop      = asyncio.Event()
        self._connected = False
        self._available = False          # False si MQTT jamais disponible

        # Compteurs de ticks par machine pour le sous-échantillonnage
        self._tick_counters: dict[str, int] = {}

        # Compteur de messages reçus (diagnostic)
        self.messages_received: int = 0
        self.last_machine_seen: str | None = None

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Boucle principale — reconnexion automatique.

        Cette coroutine tourne jusqu'à stop(). Elle doit être lancée
        via asyncio.create_task().
        """
        try:
            import aiomqtt
        except ImportError:
            logger.warning(
                "aiomqtt non disponible — consumer MQTT désactivé. "
                "Le superviseur fonctionnera en mode REST (fallback Phase 6)."
            )
            self._ready.set()   # ne pas bloquer le superviseur
            return

        topic = f"{self._root}/{self._cluster}/+/telemetry"
        consecutive_failures = 0

        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    identifier="supervisor-telemetry",
                ) as client:
                    await client.subscribe(topic, qos=0)
                    self._connected = True
                    self._available = True
                    consecutive_failures = 0
                    if not self._ready.is_set():
                        logger.info(
                            "MQTT connecté %s:%s — abonné à %s",
                            self._host, self._port, topic,
                        )
                        self._ready.set()
                    else:
                        logger.info("MQTT reconnecté à %s:%s", self._host, self._port)

                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        await self._handle(str(message.topic), bytes(message.payload))

            except Exception as exc:  # aiomqtt.MqttError ou OSError
                self._connected = False
                consecutive_failures += 1
                if consecutive_failures == 1:
                    logger.warning(
                        "MQTT indisponible (%s:%s) — superviseur en mode REST. "
                        "Reconnexion dans %.0fs.",
                        self._host, self._port, _RECONNECT_DELAY_S,
                    )
                elif consecutive_failures % 12 == 0:
                    # Un rappel toutes les ~60s (12 × 5s)
                    logger.warning(
                        "MQTT toujours indisponible (×%d depuis %.0fs) — "
                        "dernière erreur : %s",
                        consecutive_failures,
                        consecutive_failures * _RECONNECT_DELAY_S,
                        exc,
                    )
                if not self._ready.is_set():
                    self._ready.set()   # ne pas bloquer le superviseur

                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stop.wait()),
                        timeout=_RECONNECT_DELAY_S,
                    )
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        """Demande l'arrêt propre du consumer."""
        self._stop.set()

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        """Attend la première connexion (ou échec) avant de continuer.

        Returns True si MQTT disponible, False si timeout ou indisponible.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._connected

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_available(self) -> bool:
        """True si le broker a été joint au moins une fois."""
        return self._available

    def should_decide(self, machine_id: str) -> bool:
        """Retourne True si ce tick doit déclencher une décision pour cette machine.

        La décision est déclenchée tous les decision_interval_ticks ticks.
        """
        count = self._tick_counters.get(machine_id, 0) + 1
        self._tick_counters[machine_id] = count
        return (count % self._decision_interval) == 0

    # ------------------------------------------------------------------
    # Traitement des messages
    # ------------------------------------------------------------------

    async def _handle(self, topic: str, raw: bytes) -> None:
        """Normalise le payload et alimente le buffer."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Extraire machine_id depuis le topic
        # Format : dt/{cluster}/{machine}/telemetry
        parts = topic.split("/")
        if len(parts) < 4:
            return
        machine_id = parts[2]

        # Normaliser vers le format attendu par OnlineFeatureBuffer
        snapshot = self._normalize(machine_id, payload)
        if snapshot is None:
            return

        # Alimenter le buffer
        self._buffer.update(machine_id, snapshot)
        self.messages_received += 1
        self.last_machine_seen = machine_id

    def _normalize(self, machine_id: str, payload: dict) -> dict | None:
        """Transforme le payload MQTT brut en snapshot compatible buffer.

        Le format du payload est celui de jumeaux-chauds (même structure
        que GET /machines/{id} mais livré via MQTT telemetry).
        """
        if not isinstance(payload, dict):
            return None

        # Fans : liste [{idx, rpm, mode}]
        fans = payload.get("fans", [])
        if not isinstance(fans, list):
            fans = []

        # Sensors : {"temp_cpu": {"temp_c": X}, "temp_inlet": {...}, ...}
        sensors = payload.get("sensors", {})
        if not isinstance(sensors, dict):
            sensors = {}

        # Températures capteurs
        sensor_temps = []
        for v in sensors.values():
            if isinstance(v, dict) and "temp_c" in v:
                sensor_temps.append(float(v["temp_c"]))

        temp_c = float(payload.get("temperature_c", 60.0))
        sensor_temp_max  = max(sensor_temps) if sensor_temps else temp_c
        sensor_temp_mean = (sum(sensor_temps) / len(sensor_temps)) if sensor_temps else temp_c

        # Pannes actives
        faults = payload.get("faults", [])
        if not isinstance(faults, list):
            faults = []

        return {
            # Identité
            "machine_id": machine_id,
            "role":        payload.get("role", "worker"),
            "status":      payload.get("status", "on"),
            # Thermique
            "temperature_c":    temp_c,
            "sensor_temp_max":  sensor_temp_max,
            "sensor_temp_mean": sensor_temp_mean,
            # Énergie
            "power_w":          float(payload.get("power_w", 0.0)),
            "energy_kwh":       float(payload.get("energy_kwh_cumulated", 0.0)),
            # Charge
            "load_estimated":   float(payload.get("load_estimated", payload.get("load_factor", 0.5))),
            # Fans (conservé brut pour OnlineFeatureBuffer._extract_raw)
            "fans":             fans,
            # Pannes
            "faults":           faults,
        }
