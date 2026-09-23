# csi_export

One homogenised 30-min CSV per year, per site, from Campbell Scientific
EasyFlux-DL datalogger tables.

The CR6/CR1000X EasyFlux programs write two 30-min tables to the card:

* **Flux_CSIFormat / Flux_CSFormat** — fluxes (H, LE, FC), met, radiation,
  soil heat flux, soil temperature and moisture, wind, QC flags;
* **Flux_Notes** — rotation angles, stability (MO_LENGTH, ZL), spectral
  frequency factors, WPL terms, sensor diagnostic-flag totals and the
  site-geometry settings the logger was running with.

Across program generations the same quantity changes name (`SoilWater_1`
→ `SWC_1_1_1`, `batt_volt` → `V_batt`, `TA_2_1_1` → `TA_1_1_2`, …) and older
CR3000 programs use a different vocabulary altogether (`Hc`, `LE_wpl`,
`wnd_spd`, `T_hmp_Avg`, `Incoming_SW_Avg`, `SHF1_Avg` …).  `csi_export`
reads every card file, maps each file's columns onto one canonical schema,
merges the two tables, resolves overlapping card pulls, and writes one CSV per
calendar year with the same columns in the same order for every site.

Renaming is configuration, not code: the shared canonical vocabulary lives in
[`src/csi_export/schema.yaml`](src/csi_export/schema.yaml) and site-specific
overrides live in the site's config file.

## Install

```bash
git clone <this repo> csi_export
cd csi_export
pip install -e .            # or: pip install .
```

Needs Python ≥ 3.9, numpy, pandas, pyyaml (installed automatically).
With conda: `conda create -n csi python=3.11 && conda activate csi` first.

## Run

```bash
cp configs/example_site.yaml configs/bro_002.yaml     # edit site / raw_data / output
csi-export --config configs/bro_002.yaml --dry-run    # read, map, report, write nothing
csi-export --config configs/bro_002.yaml              # write the annual files
csi-export --config configs/bro_002.yaml --raw /Volumes/NAS/BRO_002/RAW --output ./exports
```

Without installing: `python -m csi_export.cli --config …` from `src/`, or
`PYTHONPATH=src python -m csi_export.cli …` from the repo root.

## Output

```
exports/bro_002/
  bro_002_csi_2025.csv          one file per calendar year
  bro_002_csi_2026.csv
  bro_002_csi_variables.csv     name, units, table, description, the raw names
                                that fed it, n_valid per year
  bro_002_csi_unmapped.csv      raw names nothing matched (only if any)
```

* `timestamp` is **period start**, tz-naive, on the logger's clock (the TOB3
  slot time is period *end*; the reader subtracts one interval).
* Columns follow the schema order; unmapped columns (if kept) follow under
  their raw names; `_src_cs` / `_src_notes` name the card file each row came
  from.
* When two card pulls hold the same half-hour, the row with more valid values
  wins.
* Where the two tables share a column the Flux_CS value is kept.
* Campbell sentinels (−9999, 6999, 7999) and float32 overflows are NaN.
* Records a CR6 wrote with its clock unset (stamped ≈ 2118-12, seconds `:28:16`)
  are dropped and the files listed.
* Programs that logged the four radiation components but no `NETRAD` get it
  derived (`derive_netrad`), flagged in `NETRAD_src`.
* `latitude` / `longitude` from Flux_Notes are exported **as the logger had
  them** — they are frequently wrong (sign dropped, stale from a cloned
  program).  Keep the true coordinates in the site config, not here.

## How renaming works

1. **Site overrides** (`csi_export.rename` in the site config) are applied
   first, per file: raw name → canonical name.
2. **Shared aliases** (`schema.yaml`, each canonical variable's `aliases:`)
   are applied next.
3. A raw name that is already canonical passes through.
4. Anything else is *unmapped*: kept under its raw name (or dropped with
   `keep_unmapped: false`) and written to `<site>_csi_unmapped.csv` so it can
   be added to one of the two places above.

Renaming happens **per file, before files are concatenated**, so a site whose
program changed mid-record gets one `SWC_1_1_1` column populated across both
eras, not two half-empty columns.  Two raw names that map to the same
canonical name *within a single file* (e.g. `T_nr` and `T_nr_Avg` both present)
keep the first and print a warning.

To extend the shared vocabulary, add a variable or an alias to `schema.yaml`:

```yaml
  SWC_1_1_1:
    units: "%"
    table: cs
    aliases: [SoilWater_1, H1_SM]
```

Every alias must be unique and must not itself be a canonical name; the
loader refuses the schema otherwise.

## Config reference

See [`configs/example_site.yaml`](configs/example_site.yaml) — every key is
documented there.  The ec_pipeline site config shape (`site: {name: …}`,
`paths: {raw_data: …, output: …}`, `csi_export: {…}`) is also accepted, so one
file can drive both tools.

## Data-source notes

* The AmeriFlux export table on the card is **not** used: it is a subset of
  Flux_CS with gaps between card pulls.
* Pre-CR6 years that exist only as converted TOA5 / AmeriFlux-format `.dat`
  files (older CR3000 towers) can be included through `csi_export.extra_dirs`;
  the CR3000 aliases are already in the schema.
* Files on a cloud drive (Box, iCloud) that are "online-only" are read
  anyway — the drive fetches them on open (they are ~1 MB each).  Pass
  `--skip-online-only` to leave them out instead.

## Tests

```bash
pip install -e ".[test]"
pytest
```

`tests/data/` holds one real Flux_CS + Flux_Notes card pair (BRO_002, June
2026, 123 half-hours) that the smoke test exports end-to-end.

## Layout

```
src/csi_export/
  reader.py     TOB3 / TOA5 decoders for the 30-min tables (mixed IEEE4B, FP2,
                BOOL4, INT4, ASCII(n) fields), file collectors
  schema.yaml   canonical variables, units, descriptions, aliases
  export.py     the exporter (rename → merge → dedup → annual CSVs + reports)
  cli.py        console entry point
configs/        site configs (example_site.yaml is the documented template)
tests/          pytest smoke test + sample data
```
