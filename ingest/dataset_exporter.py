"""Dataset Exporter — Juste des Ventilateurs.

Exporte les enregistrements collectés en fichiers Parquet partitionnés
par épisode et machine, et écrit les métadonnées associées.

Structure de sortie :
    data/raw/
    └── episode=001/
        ├── machine=srv-master-01/
        │   └── part-0.parquet
        ├── machine=srv-worker-01/
        │   └── part-0.parquet
        └── metadata.json

Usage :
    exporter = DatasetExporter(output_dir="./data", episode_id="001")
    path = exporter.export_parquet(records)
    exporter.write_metadata(scenario="stress", duration_s=600, n_records=3600)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Import optionnel de pandas/pyarrow (non disponibles au stade structure)
try:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    _PARQUET_AVAILABLE = True
except ImportError:
    _PARQUET_AVAILABLE = False
    logger.warning(
        "pandas/pyarrow non disponibles — export Parquet désactivé. "
        "Installer avec : pip install pandas pyarrow"
    )


class DatasetExporter:
    """Exporte des enregistrements normalisés vers Parquet.

    Parameters
    ----------
    output_dir : répertoire racine de stockage (ex: "./data")
    episode_id : identifiant de l'épisode (ex: "001")
    """

    def __init__(self, output_dir: str, episode_id: str) -> None:
        self.output_dir = Path(output_dir)
        self.episode_id = episode_id
        self.raw_dir = self.output_dir / "raw" / f"episode={episode_id}"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def export_parquet(self, records: list[dict]) -> str:
        """Exporte les enregistrements en Parquet, partitionné par machine.

        Parameters
        ----------
        records : liste de dicts normalisés (sortie de Normalizer)

        Returns
        -------
        str : chemin du répertoire de sortie
        """
        if not _PARQUET_AVAILABLE:
            # Fallback CSV
            return self._export_csv_fallback(records)

        if not records:
            logger.warning("Aucun enregistrement à exporter.")
            return str(self.raw_dir)

        df = pd.DataFrame(records)

        # Conversion explicite des types
        df = self._cast_types(df)

        # Partition par machine_id
        machine_ids = df["machine_id"].dropna().unique()
        for machine_id in machine_ids:
            machine_df = df[df["machine_id"] == machine_id].copy()
            machine_dir = self.raw_dir / f"machine={machine_id}"
            machine_dir.mkdir(parents=True, exist_ok=True)
            out_path = machine_dir / "part-0.parquet"

            table = pa.Table.from_pandas(machine_df, preserve_index=False)
            pq.write_table(table, out_path, compression="snappy")
            logger.info(
                "Exporté %d lignes → %s", len(machine_df), out_path
            )

        # Enregistrements sans machine_id (cluster_summary)
        cluster_df = df[df["machine_id"].isna()]
        if not cluster_df.empty:
            cluster_dir = self.raw_dir / "machine=_cluster"
            cluster_dir.mkdir(parents=True, exist_ok=True)
            out_path = cluster_dir / "part-0.parquet"
            table = pa.Table.from_pandas(cluster_df, preserve_index=False)
            pq.write_table(table, out_path, compression="snappy")
            logger.info(
                "Exporté %d lignes cluster → %s", len(cluster_df), out_path
            )

        return str(self.raw_dir)

    def write_metadata(
        self,
        scenario: str = "unknown",
        duration_s: float | None = None,
        n_records: int = 0,
        extra: dict | None = None,
    ) -> str:
        """Écrit le fichier metadata.json de l'épisode.

        Parameters
        ----------
        scenario   : nom du scénario jumeaux-chauds utilisé
        duration_s : durée de collecte en secondes
        n_records  : nombre total d'enregistrements collectés
        extra      : champs supplémentaires libres

        Returns
        -------
        str : chemin du fichier metadata.json
        """
        meta: dict[str, Any] = {
            "episode_id": self.episode_id,
            "scenario": scenario,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": duration_s,
            "n_records": n_records,
            "schema_version": "1.0",
            "storage_format": "parquet",
            "output_dir": str(self.raw_dir),
        }
        if extra:
            meta.update(extra)

        meta_path = self.raw_dir / "metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

        logger.info("Métadonnées écrites : %s", meta_path)
        return str(meta_path)

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    def _cast_types(self, df: "pd.DataFrame") -> "pd.DataFrame":
        """Applique les types corrects aux colonnes du schéma unifié."""
        float_cols = [
            "temperature_c", "sensor_temp_max", "sensor_temp_mean",
            "power_w", "energy_kwh", "fan_rpm_mean", "fan_rpm_std",
            "load_estimated", "fault_magnitude",
        ]
        int_cols = ["fan_count", "fault_count", "machines_total", "machines_on"]
        bool_cols = ["has_fault"]
        str_cols = [
            "cluster_id", "machine_id", "role", "status", "msg_type",
            "fan_modes", "fault_types", "status_cause",
            "fault_event", "fault_type_event",
        ]

        for col in float_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        for col in int_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

        for col in bool_cols:
            if col in df.columns:
                df[col] = df[col].astype("boolean")

        for col in str_cols:
            if col in df.columns:
                df[col] = df[col].astype(str).where(df[col].notna(), other=None)

        return df

    def _export_csv_fallback(self, records: list[dict]) -> str:
        """Fallback CSV si pandas/pyarrow ne sont pas installés."""
        import csv

        out_path = self.raw_dir / "data.csv"
        if not records:
            return str(out_path)

        fieldnames = list(records[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

        logger.info("Export CSV fallback : %s (%d lignes)", out_path, len(records))
        return str(out_path)
