#!/usr/bin/env python
"""
csi_export — one homogenised 30-min datalogger CSV per year, per site.

Reads every Flux_CS(I)Format and Flux_Notes TOB3 table under the site's RAW
tree (plus any `csi_export.extra_dirs`, TOB3 or TOA5), maps every column onto
the canonical vocabulary in csi_schema.yaml, merges the two tables on
timestamp, de-duplicates overlapping card pulls by completeness, and writes

    <output>/<prefix>_csi_<YYYY>.csv         one file per calendar year
    <output>/<prefix>_csi_variables.csv      name, units, table, description,
                                             source names seen, n_valid per year
    <output>/<prefix>_csi_unmapped.csv       raw names that matched nothing —
                                             add them to the schema or to the
                                             site config's csi_export.rename

Renaming is configuration, not code:
    schema.yaml (in the package) canonical names + units + aliases (shared)
    <config>.yaml: csi_export  per-site rename / scale / drop overrides,
                               applied BEFORE the shared aliases

Usage
-----
    csi-export --config configs/bro_002.yaml
    csi-export --config configs/bar_a12.yaml --start 2017-01-01
    csi-export --config configs/x.yaml --schema my_schema.yaml --dry-run
    csi-export --config configs/x.yaml --raw /Volumes/NAS/BRO_002/RAW --output ./exports

Config shapes accepted (see configs/example_site.yaml):
    site: bro_002                       # standalone
    raw_data: ../data/BRO_002/RAW       # relative paths resolve against the config file
    output: ./exports/bro_002
    csi_export: {...}
  or the full ec_pipeline site config (site.name / paths.raw_data / paths.output /
  csi_export), so one file serves both tools.

The AmeriFlux export table is deliberately NOT a source: it is a subset of
Flux_CS with gaps between card pulls (BRO_002 held 3,511 of 6,038 rows).
Use `extra_dirs` for the pre-CR6 years that only exist as converted TOA5 /
AmeriFlux .dat (BAR_A12 2017-2020, RIP_760 pre-2021).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .reader import (collect_csiformat_files, collect_notes_files,
                     read_flux_csiformat, read_flux_csiformat_toa5, _is_tob_binary)

HERE = Path(__file__).resolve().parent
DEFAULT_SCHEMA = HERE / "schema.yaml"

# Campbell / EasyFlux missing-value sentinels and the float32 overflow that a
# corrupt slot decodes to.  Anything beyond ±1e30 is never a measurement.
_SENTINELS = (-9999.0, -99999.0, 6999.0, 7999.0)
_ABS_MAX   = 1e30
_TS_LO, _TS_HI = pd.Timestamp("2010-01-01"), pd.Timestamp.now() + pd.Timedelta(days=365)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────
def load_schema(path: Path) -> tuple[dict, dict]:
    """Return (variables, alias→canonical) from csi_schema.yaml."""
    y = yaml.safe_load(open(path))
    variables = y["variables"]
    alias = {}
    for canon, spec in variables.items():
        for a in (spec or {}).get("aliases", []) or []:
            if a in variables and a != canon:
                raise SystemExit(f"[schema] alias {a!r} of {canon!r} is itself a "
                                 "canonical name — fix csi_schema.yaml")
            if a in alias and alias[a] != canon:
                raise SystemExit(f"[schema] alias {a!r} maps to both {alias[a]!r} "
                                 f"and {canon!r}")
            alias[a] = canon
    return variables, alias


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────
def _read_any(fp: str) -> pd.DataFrame:
    """TOB3 binary or TOA5 ASCII, both to a period-START `timestamp` frame."""
    if _is_tob_binary(fp):
        return read_flux_csiformat(fp, period_start=True)
    return read_flux_csiformat_toa5(fp)


_SF_DATALESS = 0x40000000   # macOS File-Provider placeholder (Box/iCloud "online-only")


def _is_local(fp: str) -> bool:
    """False for a Box online-only placeholder.  Opening one makes Box fetch
    it first — fine for these ~1 MB 30-min tables (the default), so this is
    only consulted when --skip-online-only is given."""
    try:
        st = os.stat(fp)
        return not (getattr(st, "st_flags", 0) & _SF_DATALESS) and st.st_blocks > 0
    except OSError:
        return False


def read_table(files: list[str], label: str, verbose: bool,
               renamer=None, skip_online_only: bool = False) -> tuple[pd.DataFrame, dict, set]:
    """Read every file, RENAME PER FILE (so `SoilWater_1` in 2023 cards and
    `SWC_1_1_1` in 2025 cards land in one column instead of two), then concat.
    Returns (frame, {raw: canonical} used, unmapped raw names)."""
    frames, skipped, used_all, unmapped_all, collisions = [], [], {}, set(), set()
    clock_unset = []
    n_remote = sum(not _is_local(fp) for fp in files)
    if n_remote and not skip_online_only:
        print(f"  [{label}] {n_remote} of {len(files)} file(s) are Box online-only — "
              f"Box fetches each on open (first run is slower)")
    for fp in files:
        if skip_online_only and not _is_local(fp):
            skipped.append(fp); continue
        try:
            df = _read_any(fp)
        except Exception as e:                       # one bad card ≠ no export
            print(f"  [{label}] {os.path.basename(fp)}: read failed ({e}) — skipped")
            continue
        if df is None or df.empty or "timestamp" not in df.columns:
            continue
        df = df.drop(columns=[c for c in ("RECORD", "TIMESTAMP") if c in df.columns]).copy()
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_convert(None)
        n_bad = int((~ts.between(_TS_LO, _TS_HI)).sum())
        if n_bad and n_bad >= 0.5 * len(df):
            # A CR6 that reboots with its clock unset stamps 1990 + 2^32 s ≈
            # 2118-12 on every record (seconds always :28:16).  Those rows are
            # genuinely un-timestampable; say which files they are.
            clock_unset.append((os.path.basename(fp), n_bad, len(df)))
        df = _clean_numeric(df)
        if renamer is not None:
            df, used, unmapped, coll = renamer(df)
            used_all.update(used); unmapped_all |= unmapped; collisions |= coll
        df["_src"] = os.path.basename(fp)
        frames.append(df)
    if clock_unset:
        tot = sum(n for _, n, _ in clock_unset)
        print(f"  [{label}] {len(clock_unset)} file(s) written with the logger clock "
              f"UNSET ({tot} rows stamped ~2118, dropped): "
              + ", ".join(f"{f} ({n}/{m})" for f, n, m in clock_unset[:6])
              + (" …" if len(clock_unset) > 6 else ""))
    for raw, kept, tgt in sorted(collisions):
        print(f"  [{label}] {raw!r} and {kept!r} occur in the SAME file and both map "
              f"to {tgt!r} — kept {kept!r}")
    if skipped:
        print(f"  [{label}] {len(skipped)} file(s) are Box online-only and were "
              f"SKIPPED (--skip-online-only) — first: {os.path.basename(skipped[0])}")
    if not frames:
        return pd.DataFrame(), used_all, unmapped_all
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    if getattr(out["timestamp"].dt, "tz", None) is not None:
        out["timestamp"] = out["timestamp"].dt.tz_convert(None)
    n0 = len(out)
    out = out[out["timestamp"].between(_TS_LO, _TS_HI)]
    if verbose:
        print(f"  [{label}] {len(frames)} file(s), {n0:,} rows, "
              f"{n0 - len(out)} outside {_TS_LO.year}–{_TS_HI.year} dropped")
    return out, used_all, unmapped_all


# ─────────────────────────────────────────────────────────────────────────────
# Homogenise
# ─────────────────────────────────────────────────────────────────────────────
def _clean_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if c in ("timestamp", "_src") or df[c].dtype == object:
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        s = s.mask(s.isin(_SENTINELS) | (s.abs() > _ABS_MAX))
        df[c] = s
    return df


def apply_renames(df: pd.DataFrame, site_rename: dict, alias: dict,
                  variables: dict) -> tuple[pd.DataFrame, dict, set, set]:
    """Site overrides first, then schema aliases.  Returns the renamed frame,
    the {raw: canonical} map actually used, and the set of raw names that
    matched nothing (kept under their raw name)."""
    used, unmapped = {}, set()
    new_cols = {}
    for c in df.columns:
        if c in ("timestamp", "_src"):
            continue
        if c in site_rename:
            tgt = site_rename[c]
        elif c in alias:
            tgt = alias[c]
        elif c in variables:
            tgt = c
        else:
            unmapped.add(c); tgt = c
        if tgt != c:
            used[c] = tgt
        new_cols[c] = tgt
    # two raw columns landing on one canonical name (e.g. T_nr and T_nr_Avg in
    # one file) → keep the first, warn about the rest
    seen, drop, collisions = {}, [], set()
    for raw, tgt in new_cols.items():
        if tgt in seen:
            collisions.add((raw, seen[tgt], tgt)); drop.append(raw)
        else:
            seen[tgt] = raw
    df = df.drop(columns=drop).rename(columns={r: t for r, t in new_cols.items()
                                               if r not in drop})
    return df, used, unmapped, collisions


def dedup_by_completeness(df: pd.DataFrame) -> pd.DataFrame:
    """Card pulls overlap; keep, per timestamp, the row with most valid values."""
    if df.empty:
        return df
    n_valid = df.drop(columns=["timestamp", "_src"], errors="ignore").notna().sum(axis=1)
    df = df.assign(_n=n_valid).sort_values(["timestamp", "_n"], ascending=[True, False])
    df = df.drop_duplicates("timestamp", keep="first").drop(columns="_n")
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
def _resolve(p, base: Path) -> str:
    """Expand ~ and make a relative path relative to the CONFIG FILE, not the
    shell's cwd — so a config checked in next to the data works anywhere."""
    q = Path(str(p)).expanduser()
    return str(q if q.is_absolute() else (base / q).resolve())


def normalise_config(cfg: dict, base: Path) -> tuple[str, dict, dict]:
    """Accept both the standalone shape and the ec_pipeline site-config shape.

        standalone:   site: bro_002 / raw_data: … / output: … / csi_export: {…}
        ec_pipeline:  site: {name: bro_002, …} / paths: {raw_data: …, output: …}
                      / csi_export: {…}
    Returns (site_name, paths, csi_export) with every path resolved.
    """
    xcfg = dict(cfg.get("csi_export", {}) or {})
    if isinstance(cfg.get("site"), dict):                 # ec_pipeline shape
        site = cfg["site"]["name"]
        paths = dict(cfg.get("paths", {}) or {})
    else:                                                 # standalone shape
        site = cfg.get("site") or xcfg.get("file_prefix")
        if not site:
            raise SystemExit("[config] needs `site: <name>`")
        paths = {k: cfg[k] for k in ("raw_data",) if k in cfg}
        # standalone `output:` is the export folder itself; the pipeline's
        # `paths.output` is the site's output tree, to which /csi is appended
        if cfg.get("output") and not xcfg.get("output_dir"):
            xcfg["output_dir"] = cfg["output"]
    for k in ("raw_data", "output"):
        if paths.get(k):
            paths[k] = _resolve(paths[k], base)
    if xcfg.get("output_dir"):
        xcfg["output_dir"] = _resolve(xcfg["output_dir"], base)
    xcfg["extra_dirs"] = [_resolve(d, base) for d in (xcfg.get("extra_dirs") or [])]
    return str(site), paths, xcfg


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA),
                    help="canonical schema YAML (default: the one bundled with the package)")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end",   default=None, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--raw", default=None,
                    help="override raw_data (the RAW tree holding the card folders)")
    ap.add_argument("--output", default=None,
                    help="override csi_export.output_dir / paths.output")
    ap.add_argument("--dry-run", action="store_true",
                    help="read + map + report, write nothing")
    ap.add_argument("--skip-online-only", action="store_true",
                    help="do not read Box placeholder files (default: let Box fetch them)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    verbose = not a.quiet

    cfg_path = Path(a.config).resolve()
    cfg   = yaml.safe_load(open(cfg_path)) or {}
    site, paths, xcfg = normalise_config(cfg, cfg_path.parent)
    if a.raw:
        paths["raw_data"] = str(Path(a.raw).expanduser().resolve())
    if xcfg.get("enabled", True) is False:
        print(f"[csi-export] {site}: csi_export.enabled is false — nothing to do")
        return

    prefix  = xcfg.get("file_prefix") or site
    out_dir = Path(a.output or xcfg.get("output_dir")
                   or (Path(paths.get("output", "output/" + site)) / "csi")).expanduser()
    variables, alias = load_schema(Path(a.schema))
    site_rename = dict(xcfg.get("rename", {}) or {})
    site_scale  = dict(xcfg.get("scale",  {}) or {})
    site_drop   = set(xcfg.get("drop",   []) or [])
    keep_unmapped = bool(xcfg.get("keep_unmapped", True))
    sources = [s.lower() for s in (xcfg.get("sources") or ["cs", "notes"])]

    # ── collect files ─────────────────────────────────────────────────────
    if not paths.get("raw_data"):
        raise SystemExit("  !! no raw_data in the config and no --raw given")
    roots = [paths["raw_data"]] + list(xcfg.get("extra_dirs", []) or [])
    cs_files, nt_files = [], []
    for r in roots:
        if not os.path.isdir(r):
            print(f"  !! source dir missing: {r}"); continue
        if "cs" in sources:
            cs_files += collect_csiformat_files(r)
        if "notes" in sources:
            nt_files += collect_notes_files(r)
    print("=" * 72)
    print(f"{site} — CSI annual export")
    print(f"  config : {a.config}")
    print(f"  schema : {a.schema}  ({len(variables)} canonical, {len(alias)} aliases)")
    print(f"  roots  : {roots}")
    print(f"  files  : {len(cs_files)} Flux_CS, {len(nt_files)} Flux_Notes")
    print(f"  output : {out_dir}")
    print("=" * 72)
    if not cs_files and not nt_files:
        raise SystemExit("  !! no Flux_CS / Flux_Notes files found — check paths.raw_data "
                         "and csi_export.extra_dirs")

    # ── read, clean, rename each table ────────────────────────────────────
    tables, used_all, unmapped_all = {}, {}, {}
    for label, files in (("cs", cs_files), ("notes", nt_files)):
        if not files:
            continue
        renamer = lambda d: apply_renames(d, site_rename, alias, variables)
        df, used, unmapped = read_table(files, label, verbose, renamer,
                                        skip_online_only=a.skip_online_only)
        if df.empty:
            continue
        df = df.drop(columns=[c for c in site_drop if c in df.columns])
        for c, f in site_scale.items():
            if c in df.columns:
                df[c] = df[c] * float(f)
        df = dedup_by_completeness(df)
        tables[label] = df
        used_all.update({k: (v, label) for k, v in used.items()})
        unmapped_all[label] = unmapped
        if verbose:
            print(f"  [{label}] {len(df):,} unique half-hours "
                  f"{df.timestamp.min()} → {df.timestamp.max()}, "
                  f"{len(used)} renamed, {len(unmapped)} unmapped")

    # ── merge CS + Notes ──────────────────────────────────────────────────
    if "cs" in tables and "notes" in tables:
        cs, nt = tables["cs"], tables["notes"]
        dup = [c for c in nt.columns if c in cs.columns and c not in ("timestamp",)]
        nt = nt.drop(columns=[c for c in dup if c != "_src"]).rename(columns={"_src": "_src_notes"})
        merged = cs.rename(columns={"_src": "_src_cs"}).merge(nt, on="timestamp", how="outer")
        if verbose:
            print(f"  [merge] CS {len(cs):,} + Notes {len(nt):,} → {len(merged):,} rows; "
                  f"{len(dup) - 1} shared columns taken from CS")
    else:
        label = next(iter(tables))
        merged = tables[label].rename(columns={"_src": f"_src_{label}"})
    merged = merged.sort_values("timestamp").reset_index(drop=True)

    # ── derived NETRAD where a program logged only the four components ────
    # (ART/COR/FLT 2023 EZ_v1/v2 programs have SW_IN..LW_OUT but no NETRAD).
    # Only fills rows where NETRAD is missing; flagged in NETRAD_src.
    if xcfg.get("derive_netrad", True) and all(c in merged.columns for c in
                                               ("SW_IN", "SW_OUT", "LW_IN", "LW_OUT")):
        rn4 = merged.SW_IN - merged.SW_OUT + merged.LW_IN - merged.LW_OUT
        if "NETRAD" not in merged.columns:
            merged["NETRAD"] = np.nan
        need = merged.NETRAD.isna() & rn4.notna()
        if need.any():
            merged["NETRAD_src"] = np.where(merged.NETRAD.notna(), "logger", "")
            merged.loc[need, "NETRAD"] = rn4[need]
            merged.loc[need, "NETRAD_src"] = "4-stream"
            print(f"  [derive] NETRAD filled from SW_IN-SW_OUT+LW_IN-LW_OUT on "
                  f"{int(need.sum()):,} rows (NETRAD_src column added)")

    if a.start: merged = merged[merged.timestamp >= pd.Timestamp(a.start)]
    if a.end:   merged = merged[merged.timestamp <= pd.Timestamp(a.end) + pd.Timedelta(hours=23, minutes=59)]

    # ── sparse-year clip: a corrupt slot with a wild-but-plausible timestamp
    #    (BRO_002 has one dated 2018-10-21) must not become its own annual file
    min_rows = int(xcfg.get("min_rows_per_year", 48))
    per_year = merged.timestamp.dt.year.value_counts()
    sparse = sorted(int(y) for y, n in per_year.items() if n < min_rows)
    if sparse:
        n_drop = int(merged.timestamp.dt.year.isin(sparse).sum())
        print(f"  [clip] dropped {n_drop} row(s) in sparse year(s) {sparse} "
              f"(< {min_rows} rows/year)")
        merged = merged[~merged.timestamp.dt.year.isin(sparse)]

    # ── column order: schema order, then unmapped (raw names), then provenance
    unmapped = set().union(*unmapped_all.values()) if unmapped_all else set()
    ordered = ["timestamp"] + [v for v in variables if v in merged.columns]
    raw_cols = sorted(c for c in merged.columns
                      if c not in ordered and not c.startswith("_src"))
    if not keep_unmapped:
        raw_cols = []
    prov = [c for c in merged.columns if c.startswith("_src")]
    merged = merged[ordered + raw_cols + prov]

    # ── reports ───────────────────────────────────────────────────────────
    years = sorted(merged.timestamp.dt.year.unique())
    var_rows = []
    for c in ordered[1:] + raw_cols:
        spec = variables.get(c, {}) or {}
        src_names = sorted({r for r, (t, _) in used_all.items() if t == c} | {c})
        row = dict(variable=c, units=spec.get("units", ""), table=spec.get("table", ""),
                   mapped=("canonical" if c in variables else "UNMAPPED (raw name kept)"),
                   description=spec.get("description", ""),
                   source_names=";".join(src_names))
        for y in years:
            row[f"n_valid_{y}"] = int(merged.loc[merged.timestamp.dt.year == y, c].notna().sum())
        var_rows.append(row)
    var_df = pd.DataFrame(var_rows)

    print("\nRenames applied (raw → canonical):")
    for raw, (tgt, tab) in sorted(used_all.items()):
        print(f"    {raw:28s} → {tgt:24s} [{tab}]")
    if unmapped:
        print(f"\n!! {len(unmapped)} column(s) matched neither csi_schema.yaml nor "
              f"csi_export.rename — kept under their raw names:")
        for c in sorted(unmapped):
            print(f"    {c}")
    print("\nPer-year rows:")
    for y in years:
        m = merged.timestamp.dt.year == y
        core = [c for c in ("H", "LE", "NETRAD", "TA_1_1_1", "SWC_1_1_1", "alpha") if c in merged.columns]
        cov = "  ".join(f"{c}={merged.loc[m, c].notna().mean():.2f}" for c in core)
        print(f"    {y}: {int(m.sum()):6,d} rows   {cov}")

    if a.dry_run:
        print("\n[dry-run] nothing written.")
        return

    # ── write ─────────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    for y in years:
        m = merged.timestamp.dt.year == y
        fp = out_dir / f"{prefix}_csi_{y}.csv"
        merged[m].to_csv(fp, index=False, float_format="%.6g",
                         date_format="%Y-%m-%d %H:%M:%S")
        print(f"  wrote {fp.name}  ({int(m.sum()):,} rows, {merged.shape[1]} cols)")
    var_df.to_csv(out_dir / f"{prefix}_csi_variables.csv", index=False)
    if unmapped:
        pd.DataFrame({"raw_name": sorted(unmapped),
                      "table": [";".join(t for t, s in unmapped_all.items() if c in s)
                                for c in sorted(unmapped)]}
                     ).to_csv(out_dir / f"{prefix}_csi_unmapped.csv", index=False)
    print(f"  wrote {prefix}_csi_variables.csv"
          + (f", {prefix}_csi_unmapped.csv" if unmapped else ""))


if __name__ == "__main__":
    main()
