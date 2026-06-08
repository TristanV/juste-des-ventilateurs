# Spécifications Techniques — Juste des Ventilateurs

Projet M2 Data/IA — LaPlateforme_  
Version 1.0 — Juin 2026

---

## 1. Contexte et objectifs

### 1.1 Positionnement

**Juste des Ventilateurs** est un microservice de **maintenance prédictive et de régulation thermique** conçu pour fonctionner en parallèle du jumeau numérique [jumeaux-chauds](https://github.com/TristanV/jumeaux-chauds). Il s'insère dans la boucle opérationnelle d'un datacenter simulé pour :

1. **Anticiper** les pannes thermiques (degraded, shutdown) avant qu'elles surviennent
2. **Piloter** intelligemment les ventilateurs pour maintenir la sécurité thermique tout en limitant la consommation énergétique
3. **Évaluer** et **comparer** plusieurs couples (modèle prédictif, contrôleur prescriptif) contre des baselines

### 1.2 Contraintes système

- **Sécurité absolue** : ne jamais inhiber l'arrêt thermique automatique de jumeaux-chauds
- **Latence** : décision de contrôle en < 1s, fréquence de décision ≥ 1 Hz (configurable)
- **Reproductibilité** : tous les résultats reproductibles (seeds fixés, splits documentés)
- **Indépendance** : le service fonctionne même si jumeaux-chauds redémarre (reconnexion MQTT)

---

## 2. Architecture générale

```
┌─────────────────────────────────────────────────────────────┐
│                    jumeaux-chauds                           │
│  MQTT :1883  ←──────────────────────────────────────────   │
│  REST API :8000  ←──────────────────────────────────────── │
└──────────────────────────┬──────────────────────────────────┘
                           │ télémétrie (MQTT sub)
                           │ commandes (HTTP PUT)
┌──────────────────────────▼──────────────────────────────────┐
│                 juste-des-ventilateurs                       │
│                                                             │
│  ┌──────────┐   ┌──────────┐   ┌────────────────────────┐  │
│  │  Ingest  │──▶│ Features │──▶│  Failure Predictor     │  │
│  │  (MQTT)  │   │ Pipeline │   │  (RF / GBM / LogReg)   │  │
│  └──────────┘   └──────────┘   └──────────┬─────────────┘  │
│       │                                    │ risk_score      │
│       ▼                                    ▼                 │
│  ┌──────────┐                  ┌────────────────────────┐  │
│  │  Storage │                  │  Fan Controller        │  │
│  │  (TS/PQ) │                  │  (Score / Supervised)  │  │
│  └──────────┘                  └──────────┬─────────────┘  │
│                                            │ RPM command     │
│                                            ▼                 │
│                               ┌────────────────────────┐  │
│                               │  Supervisor / Logger   │  │
│                               │  (Decision loop)       │  │
│                               └────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Module Ingest (`ingest/`)

### 3.1 Subscriber MQTT (`ingest/mqtt_subscriber.py`)

**Connexion :**
- Broker : `localhost:1883` (configurable via `.env`)
- Topics : `cluster/+/machine/+` (wildcard)
- QoS : 1 (at least once)
- Reconnexion automatique avec backoff exponentiel

**Payload reçu (format jumeaux-chauds) :**
```json
{
  "id": "machine_01",
  "role": "compute",
  "status": "on",
  "temperature_c": 67.3,
  "power_w": 342.1,
  "energy_kwh_cumulated": 1.23,
  "fans": [{"idx": 0, "rpm": 2800, "mode": "auto"}, ...],
  "sensors": {"s1": {"temp_c": 67.8, "bias_c": 0.5}, ...},
  "faults": [{"type": "fan_failure", "remaining_s": 12.3, "magnitude": 1.0}]
}
```

### 3.2 Normalizer (`ingest/normalizer.py`)

**Schéma unifié de sortie :**

| Champ | Type | Description |
|-------|------|-------------|
| `timestamp` | datetime | Horodatage UTC de réception |
| `cluster_id` | str | ID du cluster (extrait du topic MQTT) |
| `machine_id` | str | ID de la machine |
| `status` | enum | `on` / `degraded` / `off` |
| `temperature_c` | float | Température en °C |
| `power_w` | float | Puissance électrique totale (W) |
| `energy_kwh` | float | Énergie cumulée (kWh) |
| `fan_rpm_mean` | float | RPM moyen de tous les fans |
| `fan_rpm_std` | float | Écart-type des RPM |
| `fan_count` | int | Nombre de ventilateurs |
| `sensor_temp_max` | float | Température max parmi les sondes |
| `sensor_temp_mean` | float | Température moyenne des sondes |
| `has_fault` | bool | Présence d'une panne active |
| `fault_types` | str | Types de pannes actives (CSV) |
| `load_estimated` | float | Charge estimée depuis power_w |

### 3.3 Dataset Exporter (`ingest/dataset_exporter.py`)

- Export par **épisode** (durée configurable, ex: 10 min) ou par **seed** de scénario
- Format : **Parquet** (recommandé pour ML) ou CSV
- Partitionnement : `data/raw/episode={N}/machine={id}/`
- Métadonnées : seed, scénario, timestamps, version du schéma

### 3.4 Backend de stockage

**Option A — TimescaleDB** (si profil `storage` de jumeaux-chauds actif) :
- Table hypertable : `telemetry(timestamp, cluster_id, machine_id, ...)`
- Rétention : 7 jours en ligne
- Agrégations continues : moyennes 1min, 5min

**Option B — Parquet** (mode standalone) :
- Fichiers partitionnés par date et machine
- Indexé par timestamp pour les requêtes fenêtrées

---

## 4. Module Features (`features/`)

### 4.1 Features temporelles (`features/temporal.py`)

Toutes les features sont calculées sur une fenêtre glissante.

| Feature | Calcul | Fenêtre |
|---------|--------|---------|
| `temp_delta_5s` | `temp(t) - temp(t-5s)` | 5s |
| `temp_delta_15s` | `temp(t) - temp(t-15s)` | 15s |
| `temp_delta_30s` | `temp(t) - temp(t-30s)` | 30s |
| `temp_rolling_mean_30s` | Moyenne temp sur 30s | 30s |
| `temp_rolling_mean_60s` | Moyenne temp sur 60s | 60s |
| `margin_to_shutdown` | `t_shutdown - temperature_c` | instant |
| `margin_pct` | `margin_to_shutdown / t_shutdown * 100` | instant |
| `load_rolling_mean_30s` | Moyenne charge sur 30s | 30s |
| `rpm_variance` | Variance des RPM des fans | instant |
| `rpm_cv` | Coefficient de variation RPM | instant |

### 4.2 Features contextuelles (`features/contextual.py`)

| Feature | Description |
|---------|-------------|
| `time_in_hot_zone_s` | Durée cumulée depuis T > 80% seuil shutdown |
| `time_in_degraded_s` | Durée depuis entrée en mode degraded |
| `nb_shutdowns_episode` | Nombre de shutdowns depuis début épisode |
| `nb_degraded_episode` | Nombre de passages en degraded depuis début épisode |
| `cycles_since_last_fault` | Ticks depuis dernière panne |
| `has_fan_fault` | Fan failure active (bool) |
| `has_power_surge` | Power surge active (bool) |
| `fan_mode_manual` | Au moins un fan en mode manual (bool) |
| `rpm_changes_last_60s` | Nombre de changements de consigne ventilateur sur 60s |

### 4.3 Features énergétiques (`features/energy.py`)

| Feature | Description |
|---------|-------------|
| `power_fans_w` | Puissance consommée par les fans (W) |
| `power_compute_w` | Puissance de calcul (W, hors fans) |
| `fan_energy_ratio` | `power_fans / power_total` |
| `pue_estimated` | PUE estimé (1 + fan_energy / compute_energy) |
| `energy_per_temp_unit` | kWh / °C (efficacité du refroidissement) |

### 4.4 Labeler (`features/labeler.py`)

**Labels pour le modèle prédictif :**

| Label | Définition | Usage |
|-------|-----------|-------|
| `failure_60s` | `1` si status=degraded ou off(overheat) dans les 60s suivantes | Prédiction principale |
| `failure_30s` | `1` si status=degraded ou off(overheat) dans les 30s suivantes | Prédiction courte portée |
| `hot_30s` | `1` si température > `0.95 * t_shutdown` dans les 30s | Alerte température |

**Labels pour le contrôleur supervisé :**

| Label | Définition |
|-------|-----------|
| `optimal_rpm` | Consigne RPM minimale permettant de maintenir T < `0.85 * t_shutdown` |
| `action_class` | Index de l'action dans `{0, 1500, 2500, 3500, 4500}` RPM |

---

## 5. Module Failure Prediction (`models/failure_prediction/`)

### 5.1 Interface commune

Tous les modèles implémentent l'interface suivante :

```python
class FailurePredictor:
    def fit(self, X_train, y_train) -> None: ...
    def predict(self, X: pd.DataFrame) -> np.ndarray: ...  # labels binaires
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray: ...  # probabilités
    def save(self, path: str) -> None: ...
    def load(self, path: str) -> None: ...
```

### 5.2 Modèles implémentés

**Baseline heuristique (`baseline_threshold.py`) :**
- Paramètres : `T_warn` (°C), `N_seconds` (durée en zone chaude)
- Règle : `risk = 1 if (temperature_c > T_warn AND time_in_hot_zone_s > N_seconds)`
- Optimisation : grid search sur `T_warn ∈ [60, 85]` et `N ∈ [5, 60]`

**Régression Logistique (`logistic_regression.py`) :**
- Features : toutes (normalisées)
- Régularisation : L2, `C` optimisé par cross-validation
- Calibration Platt pour les probabilités

**Random Forest (`random_forest.py`) :**
- `n_estimators = 200`, `max_depth ∈ [5, 20]`, `class_weight = balanced`
- Feature importance extraite et loggée
- Seuil de décision optimisé sur Recall ≥ 0.85

**Gradient Boosting (`gradient_boosting.py`) :**
- XGBoost ou LightGBM
- Early stopping sur jeu de validation
- Tuning : `learning_rate`, `max_depth`, `n_estimators`, `subsample`

### 5.3 Protocole d'évaluation

**Splits :**
- Train : épisodes 1..N-2
- Validation : épisode N-1
- Test : épisode N (jamais vu pendant l'entraînement)
- Multi-seed : répétition sur 5 seeds différents pour robustesse

**Métriques :**
- Precision, Recall, F1, PR-AUC, ROC-AUC
- **Temps moyen d'anticipation (lead time)** : moyenne de `t_incident - t_first_alert`
- Taux de faux négatifs sur cas critiques (shutdown thermique)

---

## 6. Module Fan Control (`models/fan_control/`)

### 6.1 Interface commune

```python
class FanController:
    def decide(self, state: MachineState, risk_score: float) -> int:
        """Retourne le RPM cible pour le prochain pas de temps."""
        ...
    def fit(self, X_train, y_train) -> None: ...  # pour les méthodes supervisées
```

### 6.2 Contrôleurs implémentés

**Baseline fixe (`baseline_fixed.py`) :**
- RPM constant, plusieurs niveaux : 0 (off), 1500 (low), 2500 (medium), 3500 (high), 4500 (max)

**Baseline seuils (`baseline_threshold.py`) :**
```python
if temperature_c > T_high: rpm = 4500
elif temperature_c > T_medium: rpm = 3500
elif temperature_c > T_low: rpm = 2500
else: rpm = 1500
```
Seuils `T_low`, `T_medium`, `T_high` optimisés par grid search.

**PID simple (`baseline_pid.py`) :**
- Cible : `T_target = 0.80 * t_shutdown`
- Erreur : `e(t) = temperature_c - T_target`
- Commande : `rpm(t) = rpm_min + Kp*e + Ki*∫e + Kd*Δe` (clampé dans `[0, 4500]`)

**Contrôleur ML supervisé (`supervised_controller.py`) :**
- Classifier multiclasse (5 classes = 5 niveaux RPM)
- Features : état courant + risk_score du prédicteur
- Labels : `action_class` généré par simulation avec oracle (baseline PID optimisée)

**Contrôleur à score multi-objectif (`score_controller.py`) :**
```python
J(t) = α·risk(t) + β·heat(t) + γ·energy(t) + δ·|ΔRPM_t|
```
- `risk(t)` : probabilité de panne prédite par le modèle
- `heat(t)` : `temperature_c / t_shutdown` (proportion du seuil atteint)
- `energy(t)` : `rpm / rpm_max` (proxy consommation ventilateur)
- `|ΔRPM_t|` : pénalité de changement brusque de consigne
- Pour chaque action candidate, on choisit celle qui minimise J(t)
- Paramètres α, β, γ, δ optimisés offline

---

## 7. Module Supervisor (`supervisor/`)

### 7.1 Boucle de supervision (`supervisor/supervisor.py`)

**Cycle de décision (fréquence : toutes les 5s par défaut) :**

```
1. Lire l'état de chaque machine (via MQTT ou GET /machines/{id})
2. Calculer les features (pipeline features/)
3. Évaluer le risque (modèle prédictif → risk_score ∈ [0,1])
4. Décider la consigne RPM (contrôleur → rpm_target)
5. Si machine en mode auto : passer en mode manual (PUT /machines/{id}/fan_mode)
6. Envoyer la consigne (PUT /machines/{id}/fan_speed)
7. Logger la décision et les métriques observées
8. Attendre le prochain cycle
```

**Garanties :**
- L'arrêt thermique automatique de jumeaux-chauds reste ACTIF (non court-circuité)
- Si le superviseur crash, les machines restent dans leur dernier mode (safe by default)
- Timeout REST : 500ms max par commande

### 7.2 Logger de décisions (`supervisor/decision_logger.py`)

Chaque décision est loggée avec :
- `timestamp`, `machine_id`, `temperature_c`, `status`
- `risk_score`, `failure_predicted` (bool)
- `rpm_before`, `rpm_decided`, `fan_mode`
- `event` : `shutdown`, `degraded`, `recovery`, `normal`

Stockage : Parquet ou TimescaleDB selon configuration.

---

## 8. Module Evaluation (`evaluation/`)

### 8.1 Métriques globales de benchmark

| Métrique | Description | Sens |
|---------|-------------|------|
| `nb_shutdowns` | Nombre total d'arrêts thermiques | ↓ mieux |
| `nb_degraded_episodes` | Nombre de passages en mode dégradé | ↓ mieux |
| `T_mean` | Température moyenne sur l'épisode (°C) | ↓ mieux |
| `T_max` | Température maximale observée (°C) | ↓ mieux |
| `energy_total_kwh` | Énergie totale consommée | ↓ mieux |
| `energy_fans_kwh` | Énergie consommée par les fans | ↓ mieux |
| `fan_energy_pct` | Part des fans dans l'énergie totale (%) | info |
| `incidents_avoided_pct` | Shutdowns évités vs baseline native (%) | ↑ mieux |

### 8.2 Scénarios d'évaluation

| Scénario | Description | Durée |
|----------|-------------|-------|
| `nominal` | Charge sine_wave standard | 30 min |
| `stress` | Charge élevée + pannes fréquentes | 30 min |
| `heatwave` | Température ambiante croissante | 30 min |
| `busy_weeks` | Cycles jour/semaine réalistes | 60 min |

---

## 9. Configuration et déploiement

### 9.1 Variables d'environnement (`.env`)

```env
# jumeaux-chauds connection
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
API_BASE_URL=http://localhost:8000

# Storage
STORAGE_BACKEND=parquet  # ou timescaledb
PARQUET_DATA_DIR=./data
TIMESCALEDB_URL=postgresql://user:pass@localhost:5432/telemetry

# Supervisor
DECISION_INTERVAL_S=5
PREDICTOR_MODEL=gradient_boosting  # ou logistic_regression, random_forest, threshold
CONTROLLER_MODEL=score_controller  # ou supervised, pid, threshold, fixed
RISK_THRESHOLD=0.6  # seuil d'alerte du prédicteur

# Feature engineering
T_SHUTDOWN_DEFAULT_C=95.0  # fallback si non disponible via API
ROLLING_WINDOW_S=60
```

### 9.2 Structure Docker

```yaml
# docker-compose.yml
services:
  supervisor:
    build: .
    depends_on: [mosquitto]
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./models:/app/models
    network_mode: host  # pour rejoindre le réseau jumeaux-chauds
```

### 9.3 Requirements principaux

```
# Core
paho-mqtt>=1.6
httpx>=0.27          # client HTTP async pour l'API jumeaux-chauds
pandas>=2.0
numpy>=1.24
pyarrow>=14.0        # Parquet

# ML
scikit-learn>=1.4
xgboost>=2.0
lightgbm>=4.0
joblib>=1.3

# Storage (optionnel)
psycopg2-binary>=2.9

# Config
python-dotenv>=1.0
omegaconf>=2.3       # cohérence avec jumeaux-chauds
```

---

## 10. Intégration avec jumeaux-chauds

### 10.1 Connexion MQTT

Topics suivis :
- `cluster/{cluster_id}/machine/{machine_id}` → payload snapshot complet

### 10.2 Commandes REST utilisées

| Endpoint | Usage dans juste-des-ventilateurs |
|----------|----------------------------------|
| `GET /cluster/status` | Initialisation : découverte des machines et paramètres thermiques |
| `GET /machines/{id}` | Fallback si MQTT indisponible |
| `PUT /machines/{id}/fan_mode` | Passage en mode manual avant contrôle |
| `PUT /machines/{id}/fan_speed` | Envoi de la consigne RPM |

### 10.3 Récupération des paramètres thermiques

Au démarrage, le superviseur requête `GET /cluster/status` pour récupérer les seuils `t_shutdown_c` et `t_restart_c` de chaque machine, utilisés pour le calcul des features `margin_to_shutdown` et des labels.

---

## 11. Conventions de code

- Python 3.11+, typage statique (type hints complets)
- Formatage : `black` + `ruff`
- Tests : `pytest`, couverture ≥ 80% sur les modules critiques
- Logging : module `logging` standard, niveau configurable
- Nommage : `snake_case` pour fonctions/variables, `PascalCase` pour classes
- Docstrings : format Google style
