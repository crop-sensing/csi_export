"""End-to-end smoke test on one real BRO_002 card pair (123 half-hours)."""
import subprocess, sys
from pathlib import Path

import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent


def _write_cfg(tmp_path, **extra):
    cfg = {"site": "test_site", "raw_data": str(HERE / "data"),
           "output": str(tmp_path / "out"), "csi_export": extra}
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def _run(cfg_path, *args):
    r = subprocess.run([sys.executable, "-m", "csi_export.cli", "--config", str(cfg_path), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_annual_export(tmp_path):
    cfg = _write_cfg(tmp_path)
    out = _run(cfg)
    files = sorted((tmp_path / "out").glob("test_site_csi_*.csv"))
    names = [f.name for f in files]
    assert "test_site_csi_2026.csv" in names
    assert "test_site_csi_variables.csv" in names
    assert "test_site_csi_unmapped.csv" not in names          # everything maps
    d = pd.read_csv(tmp_path / "out" / "test_site_csi_2026.csv", parse_dates=["timestamp"])
    assert len(d) == 123
    assert d.timestamp.is_monotonic_increasing and d.timestamp.is_unique
    # both tables merged, canonical names, provenance
    for c in ("H", "LE", "NETRAD", "TA_1_1_1", "SWC_1_1_1", "alpha", "MO_LENGTH",
              "latitude", "_src_cs", "_src_notes"):
        assert c in d.columns, c
    assert d.H.notna().mean() > 0.8
    # timestamps are period START on the 30-min grid
    assert (d.timestamp.dt.minute.isin([0, 30])).all()
    v = pd.read_csv(tmp_path / "out" / "test_site_csi_variables.csv")
    assert (v.mapped == "canonical").all()
    assert v.loc[v.variable == "H", "units"].item() == "W m-2"


def test_site_rename_and_scale(tmp_path):
    # Pretend this logger called sensible heat "Hc" is not possible on real
    # data, so exercise the hooks the other way: rename a canonical column to
    # a private name and scale it.
    cfg = _write_cfg(tmp_path, rename={"T_SI111_body": "IRT_body_C"},
                     scale={"RH_1_1_1": 0.01}, drop=["Bowen_ratio"])
    _run(cfg)
    d = pd.read_csv(tmp_path / "out" / "test_site_csi_2026.csv")
    assert "IRT_body_C" in d.columns and "T_SI111_body" not in d.columns
    assert "Bowen_ratio" not in d.columns
    assert d.RH_1_1_1.max() <= 1.0                    # scaled to a fraction
    # a deliberate site rename to an off-schema name is not "unmapped" …
    assert not (tmp_path / "out" / "test_site_csi_unmapped.csv").exists()
    # … but the variables report says it is not canonical and where it came from
    v = pd.read_csv(tmp_path / "out" / "test_site_csi_variables.csv").set_index("variable")
    assert v.loc["IRT_body_C", "mapped"].startswith("UNMAPPED")
    assert "T_SI111_body" in v.loc["IRT_body_C", "source_names"]


def test_dry_run_writes_nothing(tmp_path):
    cfg = _write_cfg(tmp_path)
    out = _run(cfg, "--dry-run")
    assert "nothing written" in out
    assert not (tmp_path / "out").exists()
