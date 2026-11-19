# EM Update Data Workflow

This project produces the market-data tables and charts used to support the EM Updates slide pack. The final operational workflow is the single notebook `monthly_update.ipynb`, with shared logic kept in `helper_functions.py` and regression coverage in `test_helper_functions.py`.

The purpose of the tool is to:

- retrieve price, generation, and demand data from NemPoint
- validate data quality before charting
- calculate daily and period spreads
- generate generation profiles and deltas by fuel
- calculate gas-weighted average prices
- compare unit availability by fuel (`MMS.DISPATCHLOAD.AVAILABILITY`, intervention runs excluded) against the previous month and year, with a specific-day view against the month average
- export the reporting-month spread table to CSV for manual slide use

## Repository layout

- `monthly_update.ipynb` — the main analyst runbook
- `helper_functions.py` — query construction, validation, and market transforms
- `test_helper_functions.py` — offline tests for shared logic
- `August 2026 Spreads.csv` — example output artifact for the reporting month
- `Akaysha Powerpoint Template.potx` — slide template used in manual output assembly
- `EM Updates - Slide Development.pptx` — the worked slide deck for manual review

## How the workflow is used

1. Set the required environment variable before running the notebook:

```powershell
$env:NEMPOINT_API_KEY = "your-api-key"
```

2. Optionally override the endpoint if needed:

```powershell
$env:NEMPOINT_URL = "https://www.nempoint.com/data/query"
```

3. In `monthly_update.ipynb`, update only the configuration block variables `year` and `month`.
4. Run the notebook from the first cell in a clean kernel.
5. Review the data-quality diagnostics before using outputs in slides.
6. Exported CSVs and plotted profiles are generated from the shared helper functions rather than notebook-local logic.

## Key conventions and business rules

The pipeline applies a few important rules that are built into the shared functions:

- Price queries use an inclusive end boundary.
- Generation and demand queries use an exclusive end boundary.
- NEM settlement timestamps are normalized to operating-day windows using the standard `(timestamp - 1 minute).normalize()` convention.
- Incomplete days are excluded by default and reported in the diagnostics output.
- Duplicate mapping keys fail explicitly rather than being silently accepted.
- Unmapped fuel rows are retained and surfaced for review instead of being dropped silently.
- Gas-weighted price is a generation-weighted average, not a simple arithmetic mean.

## Shared functions

The main reusable functions are:

- `qt_post_request(...)` — posts a NemPoint query, validates the response schema, and raises `NemPointQueryError` for transport, HTTP, parsing, or missing-column failures.
- `build_price_query(...)`, `build_generation_query(...)`, `build_demand_query(...)` — return SQL text for the supported market extracts.
- `get_price_data(...)`, `get_fueltype_generation(...)`, `get_operational_demand(...)` — fetch validated market tables.
- `add_time_columns(...)` — adds calendar and settlement-bucket metadata.
- `aggregate_to_half_hour(...)` — converts five-minute values into right-closed half-hour periods.
- `build_generation_profile(...)` — creates a representative generation profile by fuel for a given state.
- `apply_fuel_mapping(...)` — maps historical fuel names while retaining unmapped values.
- `calculate_gas_weighted_price(...)` — calculates the generation-weighted gas price.
- `calculate_spreads(...)` — calculates daily and period spreads and exposes data-quality diagnostics.
- `build_time_series_quality_report(...)` — reports duplicate timestamps, gap detection, and incomplete-day issues without filling missing data.

## Gas-weighted average price

The notebook calculates gas-weighted price as:

$$
\text{GasWeightedPrice} = \frac{\sum (\text{Price} \times \text{GasGeneration})}{\sum \text{GasGeneration}}
$$

This means periods with more gas generation influence the average more strongly than periods with little or no gas generation. It is a volume-weighted price for gas generation, not a simple average across all prices.

## Validation

Run the project’s offline regression suite from the repo root:

```powershell
& "C:\Anaconda3\python.exe" -m unittest test_helper_functions.py
```

This suite does not call NemPoint; it validates the SQL builders, transformation logic, data-quality reporting, and spread calculations without network access.

## Documentation and maintenance notes

- Keep query construction separate from data transformation and plotting.
- Use descriptive `snake_case` names for functions and variables.
- Use `PascalCase` only for classes and `UPPER_SNAKE_CASE` for constants.
- Include units in names where ambiguity could matter (for example, `generation_mw` or `price_dollars_per_mwh`).
- Do not silently drop rows, impute missing market inputs, or hide API failures.
- Treat diagnostics as part of the final analytical output rather than a side note.
