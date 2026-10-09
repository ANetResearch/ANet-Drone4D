# Paper artefacts

This directory holds the manuscript, the measured data behind every number and figure in it, and the scripts that turn
the data into figures. The experiment drivers live in `tools/paper/`.

| Path | Content |
|---|---|
| `main.tex`, `sections/`, `refs.bib` | Manuscript (Springer Nature `sn-jnl` class, `sn-mathphys-num` style) |
| `data/` | One Parquet file per experiment run plus its `.meta.json` (git commit, host, options, task count); two CSV files |
| `figures/` | Figure scripts (`fig_*.py`), shared style (`style.py`), TikZ diagrams, `gen_numbers.py` |
| `figures/pdf/` | Rendered figures |
| `sections/numbers.tex` | Every number quoted in the text, generated from `data/` by `gen_numbers.py` |
| `submission/` | Flattened source, bibliography and figures as uploaded to the journal |

## Rebuild figures and the PDF from the shipped data

Needs Python 3.12 with `pandas`, `pyarrow`, `matplotlib` and `numpy`, and a TeX engine (we use
[Tectonic](https://tectonic-typesetting.github.io)).

```bash
make -C paper/figures all            # all figures and sections/numbers.tex
cd paper && tectonic -X compile main.tex
```

## Rerun the experiments

Install the repository and the city data first (see the top-level `README.md`):

```bash
make setup && make fetch-data && make worlds
export PYTHONPATH=python:tools/paper
```

`tools/paper/sweep.py <exp> --procs N --run-id ID key=value ...` runs the grid of `tools/paper/exp_<exp>.py` in parallel
worker processes and writes `runs/paper/<exp>/<ID>/rows.jsonl`; rerunning with the same ID resumes.
`tools/paper/collect.py <exp>/<ID>` converts a run into `paper/data/<exp>_<ID>.parquet`. The runs used in the paper were
made on a 64-core Xeon Gold 6426Y server; the options below are copied from their `.meta.json` files.

| Data set | Used for | Command |
|---|---|---|
| `m1geo_full` | Fig. 2 (return geometry) | `sweep.py m1geo --run-id full n=2000 chunks=5` |
| `energy_wind_shared` | Fig. 3 (wind feasibility) | `sweep.py energy --run-id wind_shared seeds=0 shared=0,1` |
| `m3perc_full` | Figs. 4, 11, 17c (perception, scan planning) | `sweep.py m3perc --run-id full seeds=3` |
| `rtl_overall2` | Figs. 8, 9, 13 (overall and ablation) | `sweep.py rtl --run-id overall2 seeds=10 policies=coupled,px4,px4_time,no_geometry,no_detour,no_wind,no_shared,no_energy,no_precheck` |
| `rtl_front2` | Fig. 10 (weather fronts) | `sweep.py rtl --run-id front2 seeds=5 presets=clear front=rain,heavyRain,thunderstorm policies=px4,coupled,coupled_4d` |
| `deleg_full` | Fig. 12 (delegation) | `sweep.py deleg --run-id full seeds=5` |
| `rtl_sens` | Fig. 17a,b (sensitivity) | `sweep.py rtl --run-id sens worlds=shenzhen,newyork,synthcity presets=clear,rain,thunderstorm seeds=3 policies=coupled rtl_margin=1.0,1.1,1.3,1.5,1.8 top_margin=0,2,5,10,20` |
| `micro_full` | Figs. 5, 14 (query costs) | `taskset -c 8 python tools/paper/sweep.py micro --procs 1 --run-id full reps=3` |
| `micro_stages2` | Fig. 15 (tick stages) | `taskset -c 8 python tools/paper/sweep.py micro --procs 1 --run-id stages2 kinds=stages reps=3` |
| `determinism_full` | Table 4 (replay determinism) | `sweep.py determinism --run-id full` |

The remaining files in `data/` (`rtl_overall`, `rtl_front`, `rtl_fronttest`, `rtl_proto5`, `rtl_proto9`,
`energy_full`, `energy_pe2`) are pilot and superseded runs kept for reference; no number in the paper uses them.

Micro-benchmarks run on one pinned core of an otherwise idle host. `tools/paper/web/chain_bmax.sh` and
`chain_front.sh` are the exact command chains used on the server.

The two CSV files come from the browser side:

- `stream_full.csv` (Fig. 16): `tools/paper/web/stream_bench.mjs` drives a scripted 60 s camera flight in Chromium via
  Playwright against a running server, for each device and point-budget controller; the per-device outputs are
  concatenated.
- `consistency_env_parity.csv` (Table 4): run `PAPER_DUMP=/tmp/env_parity_ts.json npx vitest run
  tests/environment/parity.dump.test.ts` in `apps/web`, then group the dumped per-output differences by case group
  (count, maximum absolute difference, bit-exact share).

Every result is a relative comparison between policies on the reference energy and wind models of a 6 kg hexacopter;
none of it is flight-test data.

## Literature and review helpers

`tools/paper/review_llm.py` sends the compiled text to an OpenAI-compatible endpoint for a simulated reviewer critique.
The key is read from `REVIEW_API_KEY` (or `DEEPSEEK_API_KEY`) and never stored. Its output in `review/` was used only
to find unclear passages; every number in the paper comes from `data/`.
