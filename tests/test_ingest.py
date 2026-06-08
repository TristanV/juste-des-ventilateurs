"""Tests unitaires — module ingest.

Teste le Normalizer et le DatasetExporter sans dépendance réseau.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.normalizer import Normalizer, _std
from ingest.dataset_exporter import DatasetExporter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLUSTER_ID = "cluster_alpha"

TELEMETRY_PAYLOAD = {
    "id": "srv-worker-01",
    "role": "worker",
    "status": "on",
    "ts": "2026-06-08T10:00:01.000Z",
    "temperature_c": 67.3,
    "power_w": 842.5,
    "energy_kwh_cumulated": 1.234,
    "fans": [
        {"idx": 0, "rpm": 2800, "mode": "auto"},
        {"idx": 1, "rpm": 2900, "mode": "auto"},
    ],
    "sensors": {
        "temp_cpu":     {"temp_c": 67.3, "bias_c": 0.0},
        "temp_inlet":   {"temp_c": 61.3, "bias_c": -6.0},
    },
    "faults": [],
}

STATUS_PAYLOAD = {
    "ts": "2026-06-08T10:01:00.000Z",
    "status": "degraded",
    "cause": "overheat_partial",
}

FAULT_PAYLOAD = {
    "ts": "2026-06-08T10:02:00.000Z",
    "event": "injected",
    "type": "fan_failure",
    "magnitude": 1.0,
}

SUMMARY_PAYLOAD = {
    "ts": "2026-06-08T10:00:05.000Z",
    "cluster_id": "cluster_alpha",
    "machines_total": 5,
    "machines_on": 4,
    "t_max_c": 72.1,
    "power_total_w": 3200.0,
}


# ---------------------------------------------------------------------------
# Tests Normalizer
# ---------------------------------------------------------------------------

class TestNormalizer:
    def setup_method(self):
        self.norm = Normalizer(cluster_id=CLUSTER_ID)

    def test_telemetry_basic_fields(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=TELEMETRY_PAYLOAD)

        assert rec is not None
        assert rec["machine_id"] == "srv-worker-01"
        assert rec["cluster_id"] == CLUSTER_ID
        assert rec["role"] == "worker"
        assert rec["status"] == "on"
        assert rec["msg_type"] == "telemetry"

    def test_telemetry_temperature(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=TELEMETRY_PAYLOAD)

        assert rec["temperature_c"] == pytest.approx(67.3)
        assert rec["sensor_temp_max"] == pytest.approx(67.3)
        assert rec["sensor_temp_mean"] == pytest.approx((67.3 + 61.3) / 2, abs=0.01)

    def test_telemetry_fans(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=TELEMETRY_PAYLOAD)

        assert rec["fan_count"] == 2
        assert rec["fan_rpm_mean"] == pytest.approx(2850.0)
        assert rec["fan_rpm_std"] > 0
        assert "auto" in rec["fan_modes"]

    def test_telemetry_no_fault(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=TELEMETRY_PAYLOAD)

        assert rec["has_fault"] is False
        assert rec["fault_count"] == 0
        assert rec["fault_types"] == ""

    def test_telemetry_with_fault(self):
        payload = {**TELEMETRY_PAYLOAD, "faults": [
            {"type": "fan_failure", "remaining_s": 10.0, "magnitude": 1.0}
        ]}
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=payload)

        assert rec["has_fault"] is True
        assert rec["fault_count"] == 1
        assert "fan_failure" in rec["fault_types"]

    def test_telemetry_load_estimation(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=TELEMETRY_PAYLOAD)

        # power_w=842.5, worker: idle=100, max=1450 → load ≈ 0.55
        assert 0.0 <= rec["load_estimated"] <= 1.0
        assert rec["load_estimated"] == pytest.approx((842.5 - 100) / (1450 - 100), abs=0.01)

    def test_status_event(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/status"
        rec = self.norm.normalize(topic=topic, msg_type="status", payload=STATUS_PAYLOAD)

        assert rec is not None
        assert rec["msg_type"] == "status_event"
        assert rec["status"] == "degraded"
        assert rec["status_cause"] == "overheat_partial"
        assert rec["machine_id"] == "srv-worker-01"

    def test_fault_event(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/fault"
        rec = self.norm.normalize(topic=topic, msg_type="fault", payload=FAULT_PAYLOAD)

        assert rec is not None
        assert rec["msg_type"] == "fault_event"
        assert rec["fault_event"] == "injected"
        assert rec["fault_type_event"] == "fan_failure"
        assert rec["fault_magnitude"] == pytest.approx(1.0)
        assert rec["has_fault"] is True

    def test_summary(self):
        topic = f"dt/{CLUSTER_ID}/summary"
        rec = self.norm.normalize(topic=topic, msg_type="summary", payload=SUMMARY_PAYLOAD)

        assert rec is not None
        assert rec["msg_type"] == "cluster_summary"
        assert rec["machine_id"] is None
        assert rec["machines_total"] == 5
        assert rec["machines_on"] == 4
        assert rec["sensor_temp_max"] == pytest.approx(72.1)

    def test_unknown_topic_ignored(self):
        rec = self.norm.normalize(
            topic=f"dt/{CLUSTER_ID}/srv-worker-01/power",
            msg_type="power",
            payload={"ts": "2026-06-08T10:00:00Z", "power_w": 800},
        )
        assert rec is None

    def test_timestamp_parsing(self):
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=TELEMETRY_PAYLOAD)
        assert isinstance(rec["timestamp"], datetime)

    def test_bad_timestamp_fallback(self):
        payload = {**TELEMETRY_PAYLOAD, "ts": "not-a-date"}
        topic = f"dt/{CLUSTER_ID}/srv-worker-01/telemetry"
        rec = self.norm.normalize(topic=topic, msg_type="telemetry", payload=payload)
        assert rec is not None
        assert isinstance(rec["timestamp"], datetime)


# ---------------------------------------------------------------------------
# Tests DatasetExporter
# ---------------------------------------------------------------------------

class TestDatasetExporter:
    def test_export_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = DatasetExporter(output_dir=tmpdir, episode_id="test_001")
            assert exporter.raw_dir.exists()

    def test_write_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = DatasetExporter(output_dir=tmpdir, episode_id="test_001")
            path = exporter.write_metadata(scenario="stress", duration_s=600, n_records=360)

            meta_file = Path(path)
            assert meta_file.exists()

            with open(meta_file) as f:
                meta = json.load(f)

            assert meta["episode_id"] == "test_001"
            assert meta["scenario"] == "stress"
            assert meta["duration_s"] == 600
            assert meta["n_records"] == 360
            assert meta["schema_version"] == "1.0"

    def test_export_empty_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = DatasetExporter(output_dir=tmpdir, episode_id="test_002")
            result = exporter.export_parquet([])
            assert result  # retourne quand même le chemin

    def test_csv_fallback_structure(self):
        """Teste le fallback CSV (sans pandas)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = DatasetExporter(output_dir=tmpdir, episode_id="test_003")
            records = [
                {"timestamp": "2026-01-01T00:00:00Z", "machine_id": "m01", "temperature_c": 65.0},
                {"timestamp": "2026-01-01T00:00:01Z", "machine_id": "m01", "temperature_c": 65.5},
            ]
            path = exporter._export_csv_fallback(records)
            assert Path(path).exists()


# ---------------------------------------------------------------------------
# Tests utilitaires
# ---------------------------------------------------------------------------

class TestUtils:
    def test_std_single_value(self):
        assert _std([42.0]) == 0.0

    def test_std_identical_values(self):
        assert _std([5.0, 5.0, 5.0]) == 0.0

    def test_std_known_values(self):
        # std([2,4,4,4,5,5,7,9]) = 2.0
        result = _std([2, 4, 4, 4, 5, 5, 7, 9])
        assert result == pytest.approx(2.0)
