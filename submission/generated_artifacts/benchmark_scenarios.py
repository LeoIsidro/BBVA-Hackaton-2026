"""Script de evaluación sintética para los 4 arquetipos de escenario (BASE, PICO, RURAL_FEST, CAT_VAR)."""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(REPO_DIR / "engine"))

from log8092_engine.config import load_config
from log8092_engine.runner import EngineRunner


def build_synthetic_scenarios(scen_root: Path) -> dict[str, Path]:
    base_dir = REPO_DIR / "data"
    with open(base_dir / "shipments.csv", encoding="utf-8") as f:
        base_rows = list(csv.DictReader(f))
        fieldnames = list(base_rows[0].keys())

    # 1. PICO: incremento brusco de demanda entre el día 10 y el día 45
    pico_dir = scen_root / "pico"
    shutil.copytree(base_dir, pico_dir)
    (pico_dir / "generation_manifest.json").unlink(missing_ok=True)
    pico_rows = list(base_rows)
    extra_id = 900000
    for r in base_rows:
        dt = datetime.fromisoformat(r["available_at"])
        if date(2026, 1, 15) <= dt.date() <= date(2026, 2, 20):
            extra_id += 1
            r2 = dict(r)
            r2["shipment_id"] = f"S-PICO-{extra_id:06d}"
            r2["delivery_cluster_id"] = f"{r['delivery_cluster_id']}-P"
            pico_rows.append(r2)
    pico_rows.sort(key=lambda x: (x["available_at"], x["shipment_id"]))
    with open(pico_dir / "shipments.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(pico_rows)

    # 2. RURAL_FEST: desplazamiento del 33% de envíos urbanos/suburbanos a RURAL/REMOTA + 16 festivos
    rf_dir = scen_root / "rural_festivos"
    shutil.copytree(base_dir, rf_dir)
    (rf_dir / "generation_manifest.json").unlink(missing_ok=True)
    rf_rows = []
    for idx, r in enumerate(base_rows):
        r2 = dict(r)
        if r2["zone"] in ("URBANA", "SUBURBANA") and idx % 3 == 0:
            r2["zone"] = "RURAL" if idx % 6 == 0 else "REMOTA"
            r2["delivery_cluster_id"] = f"{r['delivery_cluster_id']}-{r2['zone'][:3]}"
        rf_rows.append(r2)
    with open(rf_dir / "shipments.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rf_rows)
    with open(rf_dir / "holidays.csv", "w", encoding="utf-8", newline="") as f:
        f.write("date,name\n")
        start_d = date(2026, 1, 8)
        for i in range(16):
            hd = start_d + timedelta(days=i * 5)
            f.write(f"{hd.isoformat()},Festivo {i+1}\n")

    # 3. CAT_VAR: catálogo completamente distinto con 6 transportistas, tarifas y velocidades nuevas
    cv_dir = scen_root / "catalogo_variante"
    shutil.copytree(base_dir, cv_dir)
    (cv_dir / "generation_manifest.json").unlink(missing_ok=True)
    carriers_data = {
        "schema_version": "0.2.0",
        "carriers": [
            {
                "carrier_id": "C_ALPHA_CITY",
                "display_name": "Alpha City",
                "allowed_zones": ["URBANA"],
                "capabilities": [],
                "fleet_units": 5,
                "batch_capacity": 25,
                "pickup_minutes": 35,
                "speed_kmh": 35,
                "base_fee_cents": 240,
                "zone_surcharge_cents": {"URBANA": 20},
                "night_multiplier_bps": 14000,
                "holiday_multiplier_bps": 15000,
                "operates_sundays": False,
                "operates_holidays": False,
                "operational_failure_bps": 220,
                "supported_upgrades": ["EXTRA_UNIT", "EXPANDED_CAPACITY"],
            },
            {
                "carrier_id": "C_BETA_METRO",
                "display_name": "Beta Metro",
                "allowed_zones": ["URBANA", "SUBURBANA"],
                "capabilities": [],
                "fleet_units": 5,
                "batch_capacity": 15,
                "pickup_minutes": 25,
                "speed_kmh": 50,
                "base_fee_cents": 440,
                "zone_surcharge_cents": {"URBANA": 0, "SUBURBANA": 150},
                "night_multiplier_bps": 15000,
                "holiday_multiplier_bps": 17000,
                "operates_sundays": True,
                "operates_holidays": True,
                "operational_failure_bps": 140,
                "supported_upgrades": ["EXTRA_UNIT", "SPEED_SHIFT"],
            },
            {
                "carrier_id": "C_GAMMA_REGIONAL",
                "display_name": "Gamma Regional",
                "allowed_zones": ["SUBURBANA", "RURAL"],
                "capabilities": ["HARD_ACCESS"],
                "fleet_units": 3,
                "batch_capacity": 20,
                "pickup_minutes": 30,
                "speed_kmh": 45,
                "base_fee_cents": 580,
                "zone_surcharge_cents": {"SUBURBANA": 120, "RURAL": 320},
                "night_multiplier_bps": 15000,
                "holiday_multiplier_bps": 16000,
                "operates_sundays": True,
                "operates_holidays": False,
                "operational_failure_bps": 130,
                "supported_upgrades": ["EXTRA_UNIT", "SPEED_SHIFT"],
            },
            {
                "carrier_id": "C_DELTA_OFFROAD",
                "display_name": "Delta Offroad",
                "allowed_zones": ["RURAL", "REMOTA"],
                "capabilities": ["HARD_ACCESS"],
                "fleet_units": 3,
                "batch_capacity": 25,
                "pickup_minutes": 35,
                "speed_kmh": 55,
                "base_fee_cents": 720,
                "zone_surcharge_cents": {"RURAL": 280, "REMOTA": 620},
                "night_multiplier_bps": 14000,
                "holiday_multiplier_bps": 16000,
                "operates_sundays": True,
                "operates_holidays": True,
                "operational_failure_bps": 100,
                "supported_upgrades": [
                    "EXTRA_UNIT",
                    "SPEED_SHIFT",
                    "EXPANDED_CAPACITY",
                ],
            },
            {
                "carrier_id": "C_EPSILON_PRIME",
                "display_name": "Epsilon Prime",
                "allowed_zones": ["URBANA", "SUBURBANA", "RURAL", "REMOTA"],
                "capabilities": ["HARD_ACCESS"],
                "fleet_units": 2,
                "batch_capacity": 10,
                "pickup_minutes": 12,
                "speed_kmh": 75,
                "base_fee_cents": 1150,
                "zone_surcharge_cents": {
                    "URBANA": 0,
                    "SUBURBANA": 220,
                    "RURAL": 450,
                    "REMOTA": 850,
                },
                "night_multiplier_bps": 10000,
                "holiday_multiplier_bps": 10000,
                "operates_sundays": True,
                "operates_holidays": True,
                "operational_failure_bps": 60,
                "supported_upgrades": ["EXTRA_UNIT"],
            },
            {
                "carrier_id": "C_ZETA_SUB",
                "display_name": "Zeta Sub",
                "allowed_zones": ["SUBURBANA"],
                "capabilities": [],
                "fleet_units": 4,
                "batch_capacity": 20,
                "pickup_minutes": 40,
                "speed_kmh": 32,
                "base_fee_cents": 290,
                "zone_surcharge_cents": {"SUBURBANA": 110},
                "night_multiplier_bps": 15000,
                "holiday_multiplier_bps": 18000,
                "operates_sundays": False,
                "operates_holidays": False,
                "operational_failure_bps": 210,
                "supported_upgrades": ["EXTRA_UNIT"],
            },
        ],
    }
    (cv_dir / "carriers.json").write_text(
        json.dumps(carriers_data, indent=2), encoding="utf-8"
    )

    return {
        "BASE": base_dir,
        "PICO": pico_dir,
        "RURAL_FEST": rf_dir,
        "CAT_VAR": cv_dir,
    }


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "BASE"
    strat_path = REPO_DIR / "candidate_strategy.py"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        scens = build_synthetic_scenarios(tmp_path / "scenarios")
        targets = [which] if which in scens else list(scens.keys())
        for name in targets:
            cfg = load_config(scens[name])
            res = EngineRunner(
                config=cfg,
                candidate_path=strat_path,
                interfaces_path=REPO_DIR / "interfaces.py",
                output_path=tmp_path / f"report_{name}.json",
            ).run()
            r = res.report
            print(
                f"{name:<12} | status={r['status']} unres={r['counts']['unresolved']} "
                f"hold_br={r['counts']['voluntary_hold_breaches']} rej={r['counts']['rejected_dispatches']} "
                f"SLA={r['sla']['breach_rate']:.4f} RCP={r['rcp']['weighted_score']:.3f} "
                f"Cost={r['costs']['total_cents']} (upg={r['costs']['upgrade_cents']}) "
                f"Mean={r['lead_time_minutes']['mean']:.2f} P95={r['lead_time_minutes']['p95']} "
                f"Batch={r['batching']['average_batch_size']:.3f}"
            )


if __name__ == "__main__":
    main()
