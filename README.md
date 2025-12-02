# Duke Power Theft Detector (Interval XML Analyzer)

This tool analyzes Duke Energy / Green Button style interval XML data and flags days with suspiciously high **nighttime kW load**, which can indicate possible power theft (e.g., someone tapped your service and is running heaters or other 240V loads).

## Features

- Parses Duke / Green Button interval XML (`IntervalReading` elements)
- Converts readings to local time (default: `America/New_York`)
- Computes per-day:
  - total kWh
  - average / min / max kW
  - night-time average / min / max kW (default window: 02:00–04:00)
- Flags days as "suspicious" when:
  - night average kW exceeds a configurable minimum (default 1.0 kW), and/or
  - night average kW is more than a configurable multiple of the global baseline (default: 2× the median night-time kW across all days).

Outputs:

- A CSV file with one row per day and flag columns
- A human-readable summary in the terminal

## Install

```bash
git clone https://github.com/<your-user>/duke-power-theft-detector.git
cd duke-power-theft-detector

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Usage

```bash
python app.py --input path/to/duke_interval.xml --output report.csv
```

Optional flags:

- `--tz America/New_York` – timezone name
- `--night-start 02:00` – night window start (HH:MM)
- `--night-end 04:00` – night window end (HH:MM)
- `--min-night-kw 1.0` – minimum kW at night to be suspicious
- `--night-multiplier 2.0` – night kW > baseline × this factor → suspicious

Example:

```bash
python app.py \
  --input data/duke_2024.xml \
  --output duke_report.csv \
  --night-start 02:00 \
  --night-end 04:00 \
  --min-night-kw 1.0 \
  --night-multiplier 2.0
```

This will:

- Parse the XML
- Compute daily usage stats
- Flag suspicious nights
- Save `duke_report.csv`
- Print a short suspicious-day summary

## Using with Codex / GPT-style code tools

You can:

1. Upload this whole repo as a ZIP to your code assistant.
2. Provide one of your Duke XML interval files.
3. Ask it to:
   - add plots (Matplotlib/Plotly)
   - build a web UI (Streamlit/Flask)
   - or extend the detection logic (more advanced anomaly detection).
