# Roadmap — Juste des Ventilateurs

Projet M2 Data/IA — LaPlateforme_  
Version 1.3 — Juin 2026

---

## Vue d'ensemble

Le projet est organisé en 7 phases successives, chacune livrant des artefacts exploitables. Les phases 1 à 3 sont des fondations ; les phases 4 à 6 constituent le cœur ML et l'évaluation comparative. La phase 7 renforce la robustesse du superviseur en conditions réelles.

```
Phase 1 : Prise en main             [Semaine 1]
Phase 2 : Ingestion & stockage      [Semaine 1-2]
Phase 3 : Feature engineering       [Semaine 2-3]
Phase 4 : Modèle prédictif          [Semaine 3-4]
Phase 5 : Contrôleur prescriptif    [Semaine 4-5]
Phase 6 : Boucle fermée & éval      [Semaine 5-6]
Phase 7 : Superviseur robuste       [Semaine 6-7]
```

---

## Phase 1 — Prise en main et compréhension ✅

**Objectif :** Comprendre l'environnement jumeaux-chauds et valider la connectivité.

### Tâches

- [x] Structure du projet créée (répertoires, packages Python, Docker)
- [x] Documentation initiale (README, roadmap, specifications)
- [x] Supervisor placeholder opérationnel (Docker tourne sans erreur)
- [x] Analyse complète des topics MQTT de jumeaux-chauds
  - Root : `dt/`, cluster : `cluster_alpha`
  - Topics : `.../telemetry` (QoS 0, 1/s), `.../status` (QoS 1), `.../fault` (QoS 1), `.../summary` (QoS 1)
- [x] Analyse des endpoints REST de pilotage des ventilateurs
- [x] Identification des seuils thermiques par rôle (base.yaml)
- [x] `notebooks/01_ingestion_eda.ipynb` : exploration des données brutes et comparaison des scénarios

### Livrables ✅
- Structure du projet, Docker, supervisor placeholder
- `documents/roadmap.md`, `documents/specifications.md`, `README.md`
- `.env.example`, `build-clean-app.bat`
- `notebooks/01_ingestion_eda.ipynb`

---

## Phase 2 — Ingestion et stockage des données ✅

**Objectif :** Collecter en continu la télémétrie et constituer des datasets reproductibles.

### Tâches

- [x] `ingest/mqtt_subscriber.py` : subscriber MQTT async avec reconnexion automatique (backoff exponentiel)
  - Souscription : `dt/cluster_alpha/+/telemetry`, `.../status`, `.../fault`, `.../summary`
  - Mode CLI : `--duration N` (épisode borné) ou `--continuous` (daemon)
- [x] `ingest/normalizer.py` : parsing et normalisation des payloads
  - Schéma unifié : timestamp, cluster_id, machine_id, role, status, temperature_c, power_w, fan_rpm_mean/std, load_estimated, has_fault, fault_types...
  - Gestion des 4 types de messages : telemetry, status_event, fault_event, cluster_summary
- [x] `ingest/dataset_exporter.py` : export Parquet partitionné par machine et épisode
  - Fallback CSV si pandas/pyarrow non disponibles
  - `metadata.json` par épisode (scenario, seed, durée, n_records)
- [x] `tests/test_ingest.py` : 19 tests unitaires (Normalizer + DatasetExporter)
- [x] `data/schema.md` : schéma unifié documenté
- [x] `notebooks/01_ingestion_eda.ipynb` : exploration interactive des données brutes

### Livrables ✅
- `ingest/mqtt_subscriber.py`, `ingest/normalizer.py`, `ingest/dataset_exporter.py`
- `ingest_mqtt_simulations.bat` : collecte automatisée des 6 scénarios à x60
- `ingest_gen_features.bat` : feature engineering en batch sur tous les épisodes
- `tests/test_ingest.py`
- `data/schema.md`

### Commandes de collecte
```bash
# Collecte multi-scenarios automatisee (recommande)
ingest_mqtt_simulations.bat

# Collecte manuelle d'un episode
python -m ingest.mqtt_subscriber --duration 600 --episode 001 --scenario nominal

# Collecte continue
python -m ingest.mqtt_subscriber --continuous --episode 001 --scenario stress
```

---

## Phase 3 — Feature engineering

**Objectif :** Construire des features pertinentes pour la prédiction de pannes et le contrôle.

### Tâches

- [x] `features/temporal.py` : dérivées de température (5s/15s/30s), rolling means, marge au shutdown, RPM variance/CV
- [x] `features/contextual.py` : durée en zone chaude, compteurs shutdowns/degraded, indicateurs de pannes, changements de consigne RPM, flag récupération
- [x] `features/energy.py` : puissance fans (loi cubique RPM³), fan_energy_ratio, pue_estimated, energy_fans_kwh_cumulated
- [x] `features/labeler.py` : failure_60s / failure_30s / hot_30s (forward-looking), time_to_failure_s, optimal_rpm / action_class
- [x] `features/pipeline.py` : pipeline complet CLI, traitement multi-machine, export Parquet
- [x] `tests/test_features.py` : 28 tests unitaires
- [x] `notebooks/02_feature_engineering.ipynb` : distributions, corrélations, split temporel

### Livrables ✅
- `features/temporal.py`, `features/contextual.py`, `features/energy.py`
- `features/labeler.py`, `features/pipeline.py`
- `tests/test_features.py`
- `notebooks/02_feature_engineering.ipynb`

### Commandes
```bash
# Feature engineering en batch (tous les episodes)
ingest_gen_features.bat

# Episode specifique
ingest_gen_features.bat 003

# Manuel
python -m features.pipeline \
  --input data/raw/episode=001 \
  --output data/processed/episode=001 \
  --config data/raw/episode=001/metadata.json

pytest tests/test_features.py -v
```

---

## Phase 4 — Modèle d'anticipation de pannes ✅

**Objectif :** Prédire avec suffisamment d'avance les incidents thermiques.

### Module : `models/failure_prediction/`

### Stratégie de split : Option A — fenêtre temporelle (70/15/15)

Chaque épisode est coupé chronologiquement en train/val/test, puis les morceaux sont concaténés. Aucun leakage temporel, chaque scénario est représenté dans les 3 splits.

```
Split sur 6 épisodes (304k lignes, 47 features) :
  train : 212 864 lignes  (pos failure_60s = 22.0%)
  val   :  45 611 lignes
  test  :  45 621 lignes  (pos failure_60s = 18.8%)
```

### Tâches

- [x] **Splitter temporel** (`models/failure_prediction/splitter.py`)
  - `TemporalSplitter` : split 70/15/15 par épisode, concaténation globale
  - `split()` retourne X_train/val/test, y_train/val/test + feature_cols
  - `split_with_meta()` retourne les DataFrames complets pour le calcul du lead time
- [x] **Baseline heuristique** (`models/failure_prediction/baseline_threshold.py`)
  - Règle : `T > T_warn ET time_in_hot_zone_s > N`
  - Grid search sur T_warn ∈ [60, 85]°C et N ∈ [0, 30]s
- [x] **Modèle 1 : Régression Logistique** (`models/failure_prediction/logistic_regression.py`)
  - StandardScaler + CalibratedClassifierCV (Platt)
  - C optimisé par validation, seuil optimisé sur Recall ≥ 0.85
- [x] **Modèle 2 : Random Forest** (`models/failure_prediction/random_forest.py`)
  - class_weight="balanced", grid search profondeur/n_estimators
  - Feature importance loggée, seuil optimisé
- [x] **Gradient Boosting** (`models/failure_prediction/gradient_boosting.py`)
  - XGBoost (fallback LightGBM, puis sklearn)
  - Early stopping sur val, scale_pos_weight automatique
- [x] **Évaluation comparative** (`evaluation/failure_prediction_eval.py`)
  - Métriques : Precision, Recall, F1, PR-AUC, ROC-AUC
  - **Lead time** : temps moyen entre première alerte et incident (fenêtre 120s)
  - Taux de faux négatifs sur shutdowns
  - Tableau comparatif + export JSON
- [x] Sauvegarde des modèles : `models/failure_prediction/saved/` (joblib)
- [x] Notebook : `notebooks/03_failure_prediction.ipynb`

### Métriques cibles
- Recall sur cas dangereux ≥ 0.85
- Temps moyen d'anticipation ≥ 30s avant incident
- F1-score > baseline heuristique

### Résultats obtenus (6 épisodes, 304k lignes, 47 features)

| Modèle | F1 | Recall | PR-AUC | Lead time médian | Détectés |
|--------|----|--------|--------|------------------|---------|
| baseline | 0.141 | 0.130 | 0.171 | 8.7s | 10/14 |
| logistic | 0.851 | 0.930 | 0.812 | 72s | **12/14** |
| random_forest | 0.877 | 0.930 | 0.759 | 5876s | 4/14 |
| gradient_boosting | **0.877** | **0.931** | 0.757 | 5876s | 4/14 |

**Recommandation Phase 6 :** régression logistique (meilleur taux de détection 12/14, PR-AUC supérieur).

### Livrables ✅
- `models/failure_prediction/splitter.py`
- `models/failure_prediction/baseline_threshold.py`
- `models/failure_prediction/logistic_regression.py`
- `models/failure_prediction/random_forest.py`
- `models/failure_prediction/gradient_boosting.py`
- `models/failure_prediction/saved/` (4 modèles joblib)
- `evaluation/failure_prediction_eval.py`
- `evaluation/results/failure_prediction_results_failure_60s.json`
- `notebooks/03_failure_prediction.ipynb`
- `train_models.bat`

### Commandes
```bash
# Entrainement + evaluation comparative (tous les modeles)
train_models.bat

# Label specifique
train_models.bat failure_30s

# EDA rapide pour verifier les splits avant entrainement
python ingest_quick_EDA.py --processed-only

# Evaluation seule (sans re-entrainement)
python -m evaluation.failure_prediction_eval --label failure_60s --models baseline logistic random_forest gradient_boosting
```

---

## Phase 5 — Contrôleur de régulation des ventilateurs ✅

**Objectif :** Définir une politique d'actionnement des ventilateurs, sûre et sobre en énergie.

### Module : `models/fan_control/`

### Espace d'actions
Actions discrétisées : `RPM ∈ {0, 1500, 2500, 3500, 4500}` par ventilateur.

### Tâches

- [x] **Baseline 1 : Ventilateur fixe** (`models/fan_control/baseline_fixed.py`)
  - RPM constant (niveaux testés : 1500, 2500, 4500)
  - Interface : `decide(state, risk_score)`, `decide_batch(X)`, `save()`/`load()`
- [x] **Baseline 2 : Contrôle à seuils** (`models/fan_control/baseline_threshold.py`)
  - `if T > T_high: RPM=4500; elif T > T_med: RPM=3500; elif T > T_low: RPM=2500; else: RPM=1500`
  - Seuils optimisés par grid search (score = Recall_failure - 0.1*mean_rpm_norm)
- [x] **Baseline 3 : PID simple** (`models/fan_control/baseline_pid.py`)
  - Cible : `T_target = 0.80 × t_shutdown`
  - Commande clampée → quantifiée au niveau RPM discret le plus proche
  - Gains Kp, Ki, Kd optimisés par grid search
- [x] **Contrôleur ML supervisé** (`models/fan_control/supervised_controller.py`)
  - RandomForestClassifier multiclasse sur `action_class` (0-3)
  - Features : toutes les features du splitter + risk_score optionnel
  - StandardScaler + class_weight=balanced
- [x] **Contrôleur à score multi-objectif** (`models/fan_control/score_controller.py`)
  - `J(a) = α·risk + β·heat + γ·energy(a) + δ·|ΔRPM|/RPM_MAX`
  - Paramètres α, β, γ, δ optimisés par grid search
- [ ] **(Optionnel avancé) Bandit contextuel** (`models/fan_control/contextual_bandit.py`)
- [x] **Évaluation comparative** (`evaluation/fan_control_eval.py`)
  - Métriques : mean_rpm, T_mean, %temps_critique, action_accuracy, rpm_mae, high_rpm_when_dangerous
  - risk_scores fournis par le prédicteur logistic (Phase 4)
- [x] Notebook : `notebooks/04_fan_control.ipynb`

### Métriques cibles
- Réduction du nombre de shutdowns vs baseline auto native ≥ 50%
- Consommation énergétique des fans ≤ baseline "full speed" (économie ≥ 20%)

### Livrables ✅
- `models/fan_control/baseline_fixed.py`
- `models/fan_control/baseline_threshold.py`
- `models/fan_control/baseline_pid.py`
- `models/fan_control/supervised_controller.py`
- `models/fan_control/score_controller.py`
- `evaluation/fan_control_eval.py`
- `train_fan_controllers.bat`
- `tests/test_phase5_controllers.py`
- `notebooks/04_fan_control.ipynb`

### Commandes
```bash
# Entrainement + evaluation comparative (tous les controleurs)
train_fan_controllers.bat

# Evaluation seule
python -m evaluation.fan_control_eval --label failure_60s

# Controleurs specifiques
python -m evaluation.fan_control_eval --models baseline_pid score_controller

# Tests
pytest tests/test_phase5_controllers.py -v
pytest tests/test_phase5_controllers.py -v -m "not slow"
```

---

## Phase 6 — Boucle fermée et évaluation comparative

**Objectif :** Coupler prédicteur + contrôleur en temps réel et mesurer l'impact réel.

### Tâches

- [x] **Service de supervision** (`supervisor/supervisor.py`)
  - Boucle : lire état REST → évaluer risque (prédicteur logistic) → décider RPM (contrôleur supervisé) → envoyer commande REST → logger
  - Fréquence de décision configurable (défaut 5s)
  - Modes : `ml` | `threshold` | `native`
  - Override automatique RPM_HIGH si risk_score >= 0.60
  - Passage en mode `manual` avant prise de main, retour `auto` à l'arrêt
- [x] **Logger de décisions** (`supervisor/decision_logger.py`)
  - Format JSONL : timestamp, machine_id, temperature_c, risk_score, rpm_decided, rpm_previous, mode, risk_override
  - `to_dataframe()` pour chargement dans les notebooks
- [x] **Protocole d'évaluation** (`evaluation/benchmark.py`)
  - 3 modes comparés sur le jeu de test offline :
    1. **native** : RPM oracle du simulateur, aucune intervention
    2. **threshold** : contrôleur à seuils externe
    3. **ml** : prédicteur logistic + contrôleur supervisé (recommandé)
  - Métriques : mean_rpm, T_mean, T_max, %critique, action_accuracy, lead_time, détection incidents
- [x] **Test de robustesse** (`evaluation/robustness.py`)
  - Évaluation par scénario (basic, busy_weeks, heatwave, nominal, stress, trace_replay)
  - Comparaison ML vs natif : delta_rpm, delta_power, react_rate
- [x] **Rapport final** (`documents/rapport_analyse.md`)
  - Résultats chiffrés, analyse critique, recommandations
- [x] Notebook final : `notebooks/05_evaluation_comparative.ipynb`

### Résultats Phase 6 (offline replay, failure_60s)

| Mode | MeanRPM | AccAct | DangHigh | LeadTime médian | Incidents détectés |
|------|---------|--------|----------|-----------------|-------------------|
| native | 984 | 0.200 | 0.000 | 0s | 0/7986 |
| threshold | 1815 | 0.417 | 0.105 | 0s | 0/7986 |
| **ml** | **2681** | **0.786** | **1.000** | **120s** | **7981/7986** |

### Livrables ✅
- `supervisor/supervisor.py` : service de supervision temps réel
- `supervisor/decision_logger.py` : logger JSONL
- `evaluation/benchmark.py` : benchmark comparatif 3 modes
- `evaluation/robustness.py` : test robustesse par scénario
- `evaluation/results/benchmark_results.json`
- `evaluation/results/robustness_results.json`
- `documents/rapport_analyse.md`
- `notebooks/05_evaluation_comparative.ipynb`
- `run_phase6.bat`
- `tests/test_phase6_supervisor.py`

### Commandes
```bash
# Evaluation comparative offline (recommande)
run_phase6.bat

# Benchmark seul
python -m evaluation.benchmark --label failure_60s

# Robustesse par scenario
python -m evaluation.robustness --label failure_60s

# Supervisor temps reel (jumeaux-chauds doit tourner)
python -m supervisor.supervisor --mode ml --duration 300 --dry-run
python -m supervisor.supervisor --mode ml --duration 300

# Tests
pytest tests/test_phase6_supervisor.py -v -m "not slow"
```

---

## Phase 7 — Superviseur robuste et télémétrie temps réel

**Objectif :** Corriger les limitations du superviseur Phase 6 découvertes en test live contre jumeaux-chauds, et le rendre robuste à toute vitesse de simulation.

### Contexte et diagnostic

Trois problèmes ont été identifiés lors des tests live :

**Problème 1 — Features nulles (corrigé Phase 6 post-livraison)**
Le superviseur calculait les features à partir d'un snapshot unique (pas de mémoire temporelle). Toutes les features glissantes (`temp_delta_*`, `temp_rolling_*`, etc.) étaient fixées à 0.0, rendant le prédicteur aveugle aux montées en température. Le modèle voyait toujours le profil d'une machine froide et stable.

**Problème 2 — Fréquence de lecture inadaptée**
Le superviseur lit l'API REST toutes les 5 secondes réelles. Jumeaux-chauds publie 1 snapshot par seconde simulée (`events_per_sec=1.0`). À `speed=1x`, le buffer accumule 1 point toutes les 5 secondes au lieu de 1 par seconde : les fenêtres glissantes sont 5× trop larges. À `speed=60x`, la divergence atteint 300×.

**Problème 3 — Logs trop verbeux**
Chaque GET `/cluster/status` générait une ligne de log httpx, soit ~12 lignes/minute masquant les informations utiles (décisions, risques, anomalies).

### Tâche 1 — Correctifs déjà livrés (fin Phase 6) ✅

- [x] `supervisor/online_features.py` : `OnlineFeatureBuffer` — fenêtre glissante 70 ticks par machine
  - Recalcule `temp_delta_5/15/30s`, `temp_rolling_mean/std_30/60s`, `margin_*`, `rpm_*`, `power_*`
  - Calcule `pue_estimated`, `power_fans_w` (loi cubique RPM³), `energy_fans_kwh_cumulated`
  - Maintient les compteurs cumulatifs : `time_in_hot_zone_s`, `nb_shutdowns_episode`, `ticks_since_last_fault`, `is_recovering`
  - Aligné exactement sur `features/temporal.py`, `features/energy.py`, `features/contextual.py`
- [x] `supervisor/supervisor.py` : `RPM_MIN=800` — plancher de ventilation (évite RPM=0 à froid)
- [x] `supervisor/supervisor.py` : `_machines_iter()` — normalisation liste/dict pour compat API
- [x] `models/failure_prediction/logistic_regression.py` : `load()` rétrocompat joblib multi-clés
- [x] `docker-compose.yml` : `host.docker.internal` (fix Docker Desktop Windows)
- [x] `evaluation/_compat.py` : UTF-8 stdout Windows CMD

### Tâche 2 — Nettoyage des logs superviseur 🔲

**Objectif :** Rendre le flux de logs lisible en conditions réelles, avec le principe "silence = tout va bien".

- [ ] Passer en `DEBUG` : tous les logs httpx (GET/PUT HTTP 200 OK), `[cycle N] t=Xs`
- [ ] Configurer httpx pour ne logger qu'en WARNING par défaut : `logging.getLogger("httpx").setLevel(logging.WARNING)`
- [ ] Garder en `INFO` reformaté :
  - **Une ligne par cycle** (toutes les N secondes) : `[t=120s  speed=1x]  cluster — 5 on  T_max=67.3°C  risk_max=0.12`
  - **Une ligne par machine** uniquement si `risk > RISK_LOG_THRESHOLD` (défaut 0.05) **ou** RPM change : `srv-worker-01  T=67.3°C  risk=0.42  RPM 1500→3500 [RISK OVERRIDE]`
  - Connexions initiales et passage en mode manual/auto
  - Warnings et erreurs
- [ ] **Déduplication des erreurs répétitives** : si la même erreur se répète consécutivement, n'afficher qu'un message résumé toutes les N occurrences (ex: `GET /cluster/status échoué (×12 depuis 60s) — dernière erreur : [Errno 101]`)
- [ ] Variable d'environnement `RISK_LOG_THRESHOLD` (défaut 0.05) configurable via `.env`

**Livrables :**
- `supervisor/supervisor.py` modifié (log reformaté, déduplication)

### Tâche 3 — Option E : télémétrie via MQTT (subscriber dédié) 🔲

**Pourquoi Option E et non C**

L'Option C (corriger `tick_hz` du buffer selon `speed_multiplier`) reste une approximation : à `speed=60x` avec lecture REST toutes les 5s réelles, chaque tick du buffer représente 300 secondes simulées — impossible de reconstituer `temp_delta_5s` (qui nécessite 5 ticks consécutifs à 1 Hz simulé). L'Option C ne résout le problème qu'à `speed=1x`.

L'Option E reçoit les snapshots **au rythme de la simulation** via MQTT (`events_per_sec=1.0` en temps simulé). Le buffer se remplit à la bonne cadence quelle que soit la vitesse : à `speed=60x`, il reçoit 60 snapshots par seconde réelle et accumule 60 secondes simulées en 1 seconde réelle. Les features glissantes sont toujours calculées sur les bonnes fenêtres temporelles simulées.

**Correctif Option C nécessaire en complément**

L'Option E résout l'alimentation du buffer. Mais la *décision* (envoyer une commande fan) doit rester cadencée en temps simulé, pas en temps réel. Sans correction, le superviseur déciderait 60×/seconde à `speed=60x`, surchargeant l'API. Il faut donc ajouter un mécanisme de **sous-échantillonnage des décisions** : le buffer reçoit tous les ticks MQTT, mais une décision n'est prise que tous les `decision_interval_ticks` ticks simulés (ex: toutes les 5 secondes simulées = 5 ticks). Ce compteur est l'unique emprunt à l'esprit de l'Option C.

**Architecture cible**

```
jumeaux-chauds MQTT :1883
  topic: dt/cluster_alpha/+/telemetry  (1 msg/s simulé par machine)
       │
       ▼
supervisor/mqtt_telemetry.py          ← NOUVEAU
  MqttTelemetryConsumer
  - s'abonne à dt/{cluster}/+/telemetry
  - normalise le payload (même logique que ingest/normalizer.py)
  - appelle feat_buffer.update(machine_id, snapshot) à chaque message
  - thread/tâche asyncio séparée du loop de décision
       │
       ▼
supervisor/online_features.py
  OnlineFeatureBuffer (déjà livré)
  - accumule les ticks à la cadence MQTT (= cadence simulée)
       │
       ▼ (tous les decision_interval_ticks ticks simulés)
supervisor/supervisor.py
  Supervisor._decision_loop()
  - lit feat_buffer.get_features() pour chaque machine
  - prédit risk, décide RPM, envoie commande REST
  - logue la décision
```

**Points de conception :**
- Le consumer MQTT tourne en `asyncio` dans le même process (pas de thread séparé)
- La commande fan continue à passer par REST (`PUT /machines/{id}/fan_speed`) — MQTT est uniquement lecture
- Si MQTT est indisponible (pas de broker), fallback sur lecture REST (comportement Phase 6)
- `decision_interval_ticks` configurable (défaut 5 = toutes les 5s simulées)
- Le `speed_multiplier` courant est lu depuis l'API au démarrage pour dimensionner les logs (`[t=120s speed=60x]`)

**Tâches de développement :**
- [ ] `supervisor/mqtt_telemetry.py` : `MqttTelemetryConsumer` — subscriber asyncio, normalisation payload, alimentation buffer
- [ ] `supervisor/supervisor.py` : refactoring boucle principale — séparer tick MQTT (alimentation buffer) et cycle de décision (sous-échantillonnage)
- [ ] `supervisor/supervisor.py` : fallback REST si MQTT indisponible
- [ ] `.env.example` : ajouter `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `DECISION_INTERVAL_TICKS`
- [ ] `docker-compose.yml` : ajouter variables MQTT au service supervisor
- [ ] `tests/test_phase7_supervisor.py` : tests unitaires MqttTelemetryConsumer + boucle de décision sous-échantillonnée
- [ ] Mettre à jour `documents/rapport_analyse.md` avec résultats live post-Phase 7

**Livrables attendus :**
- `supervisor/mqtt_telemetry.py`
- `supervisor/supervisor.py` refactorisé
- `tests/test_phase7_supervisor.py`
- Mise à jour `docker-compose.yml`, `.env.example`

---

## Récapitulatif des livrables

| Livrable | Phase | Priorité |
|----------|-------|----------|
| `ingest/` : subscriber MQTT + normaliser | 2 | Essentielle |
| `data/` : datasets reproductibles | 2-3 | Essentielle |
| `features/` : pipeline de features | 3 | Essentielle |
| `models/failure_prediction/` : 3 modèles + baseline | 4 | Essentielle |
| `models/fan_control/` : 3+ contrôleurs | 5 | Essentielle |
| `supervisor/` : boucle fermée temps réel | 6 | Essentielle |
| `supervisor/online_features.py` : buffer features glissantes | 7 | Essentielle |
| `supervisor/mqtt_telemetry.py` : consumer MQTT télémétrie | 7 | Essentielle |
| `evaluation/` : protocole + résultats | 4-6 | Essentielle |
| `documents/rapport_analyse.md` | 6-7 | Essentielle |
| `docker-compose.yml` | 2 | Recommandée |
| Bandit contextuel | 5 | Optionnelle |

---

## Couples prédicteur / contrôleur à comparer

| # | Prédicteur | Contrôleur | Objectif |
|---|-----------|------------|----------|
| 0 | Aucun (natif) | Auto jumeaux-chauds | Baseline de référence |
| 1 | Heuristique seuil | Seuils fixes | Baseline règles simples |
| 2 | Régression Logistique | PID | Approche classique |
| 3 | Random Forest | Score multi-objectif | Recommandé |
| 4 | Gradient Boosting | Score multi-objectif | Meilleure performance attendue |
| 5 | Gradient Boosting | Bandit contextuel | Option avancée |
