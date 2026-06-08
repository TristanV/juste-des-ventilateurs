# Schéma des données — Juste des Ventilateurs

## Schéma unifié (après normalisation)

| Champ | Type | Description | Source |
|-------|------|-------------|--------|
| `timestamp` | datetime (UTC) | Horodatage de réception | MQTT |
| `cluster_id` | str | ID du cluster | Topic MQTT |
| `machine_id` | str | ID de la machine | Payload |
| `status` | str | `on` / `degraded` / `off` | Payload |
| `temperature_c` | float | Température en °C | Payload |
| `power_w` | float | Puissance électrique totale (W) | Payload |
| `energy_kwh` | float | Énergie cumulée (kWh) | Payload |
| `fan_rpm_mean` | float | RPM moyen des ventilateurs | Calculé |
| `fan_rpm_std` | float | Écart-type des RPM | Calculé |
| `fan_count` | int | Nombre de ventilateurs | Calculé |
| `fan_modes` | str | Modes des fans (CSV) | Payload |
| `sensor_temp_max` | float | Température max parmi les sondes | Calculé |
| `sensor_temp_mean` | float | Température moyenne des sondes | Calculé |
| `has_fault` | bool | Présence d'une panne active | Calculé |
| `fault_types` | str | Types de pannes actives (CSV) | Payload |
| `load_estimated` | float | Charge estimée depuis power_w | Calculé |

## Partitionnement des fichiers Parquet

```
data/raw/
├── episode=001/
│   ├── machine=m01/part-0.parquet
│   └── machine=m02/part-0.parquet
└── episode=002/...

data/processed/
├── episode=001/
│   ├── features.parquet    # features + labels
│   └── metadata.json       # seed, scénario, t_shutdown par machine
└── ...
```

## Métadonnées d'épisode (metadata.json)

```json
{
  "episode_id": "001",
  "scenario": "stress",
  "seed": 42,
  "start_ts": "2026-06-08T10:00:00Z",
  "end_ts": "2026-06-08T10:30:00Z",
  "duration_s": 1800,
  "machines": {
    "m01": {"t_shutdown_c": 95.0, "t_restart_c": 70.0, "fan_max_rpm": 5000}
  },
  "schema_version": "1.0"
}
```
