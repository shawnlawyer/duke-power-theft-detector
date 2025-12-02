#!/usr/bin/env python3
"""
Duke Energy XML usage analyzer / theft detector.

Usage:
    python app.py --input path/to/duke.xml --output report.csv
"""

import argparse
import sys
from datetime import datetime, time as dtime

from dateutil import tz
import pandas as pd
from lxml import etree


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze Duke Energy XML interval data for abnormal usage.")
    parser.add_argument("--input", "-i", required=True, help="Path to Duke interval XML file")
    parser.add_argument("--output", "-o", default="usage_report.csv", help="Path to save daily summary CSV")
    parser.add_argument("--tz", default="America/New_York", help="Timezone for interpreting timestamps")
    parser.add_argument("--night-start", type=str, default="02:00", help="Night window start (HH:MM)")
    parser.add_argument("--night-end", type=str, default="04:00", help="Night window end (HH:MM)")
    parser.add_argument("--min-night-kw", type=float, default=1.0,
                        help="Minimum kW at night to be considered suspicious")
    parser.add_argument("--night-multiplier", type=float, default=2.0,
                        help="Night kW > (global_baseline * this) → suspicious")
    return parser.parse_args()


def parse_duke_xml(path, tz_name="America/New_York"):
    """
    Parse Duke / Green Button style XML interval data.

    Expects elements like:
      <IntervalReading>
        <timePeriod>
          <start>1700788800</start>
          <duration>900</duration>  (seconds)
        </timePeriod>
        <value>123</value>  (watt-hours for that interval)
      </IntervalReading>
    """
    with open(path, "rb") as f:
        tree = etree.parse(f)

    nsmap = tree.getroot().nsmap
    # Handle namespaces or lack thereof
    interval_xpath_candidates = [
        "//IntervalReading",
        "//espi:IntervalReading",
        "//*[local-name()='IntervalReading']",
    ]

    intervals = []
    for xp in interval_xpath_candidates:
        elems = tree.xpath(xp, namespaces={k: v for k, v in nsmap.items() if k})
        if elems:
            for ir in elems:
                start_elem = ir.xpath(".//*[local-name()='start']")
                dur_elem = ir.xpath(".//*[local-name()='duration']")
                val_elem = ir.xpath(".//*[local-name()='value']")
                if not (start_elem and dur_elem and val_elem):
                    continue
                try:
                    start_epoch = int(start_elem[0].text.strip())
                    duration_s = int(dur_elem[0].text.strip())
                    wh = float(val_elem[0].text.strip())
                except (ValueError, TypeError):
                    continue

                # Convert to datetime in given timezone
                dt_utc = datetime.utcfromtimestamp(start_epoch).replace(tzinfo=tz.UTC)
                local_tz = tz.gettz(tz_name)
                dt_local = dt_utc.astimezone(local_tz)

                kw = (wh * 3600.0) / (duration_s * 1000.0)  # Wh over duration → kW

                intervals.append(
                    {
                        "start": dt_local,
                        "duration_s": duration_s,
                        "wh": wh,
                        "kw": kw,
                    }
                )
            break  # stop after first xpath that worked

    if not intervals:
        raise ValueError("No IntervalReading elements found. Check XML format or tags.")

    df = pd.DataFrame(intervals)
    df = df.sort_values("start").reset_index(drop=True)
    df["date"] = df["start"].dt.date
    df["time"] = df["start"].dt.time
    return df


def in_time_window(t: dtime, start: dtime, end: dtime) -> bool:
    """
    True if time t is within [start, end) considering no wrap-around.
    """
    return start <= t < end


def compute_daily_summary(df, night_start_str="02:00", night_end_str="04:00"):
    night_start = dtime.fromisoformat(night_start_str)
    night_end = dtime.fromisoformat(night_end_str)

    def classify(row):
        t = row["time"]
        if in_time_window(t, night_start, night_end):
            return "night"
        else:
            return "other"

    df["bucket"] = df.apply(classify, axis=1)

    daily = df.groupby("date").agg(
        total_kwh=("wh", lambda x: x.sum() / 1000.0),
        avg_kw=("kw", "mean"),
        min_kw=("kw", "min"),
        max_kw=("kw", "max"),
    )

    # Night stats
    night = df[df["bucket"] == "night"].groupby("date").agg(
        night_avg_kw=("kw", "mean"),
        night_min_kw=("kw", "min"),
        night_max_kw=("kw", "max"),
    )

    summary = daily.join(night, how="left")
    return summary


def flag_suspicious_days(summary, min_night_kw=1.0, night_multiplier=2.0):
    # Global night baseline: median of night_avg_kw across days where it exists
    valid_nights = summary["night_avg_kw"].dropna()
    if len(valid_nights) == 0:
        baseline = None
    else:
        baseline = valid_nights.median()

    flags = []
    for date, row in summary.iterrows():
        night_avg = row.get("night_avg_kw", float("nan"))
        suspicious = False
        reasons = []

        if pd.notna(night_avg):
            if night_avg >= min_night_kw:
                suspicious = True
                reasons.append(f"night_avg_kw >= {min_night_kw:.2f} kW")
            if baseline is not None and night_avg >= baseline * night_multiplier:
                suspicious = True
                reasons.append(
                    f"night_avg_kw >= {night_multiplier:.1f} × baseline ({baseline:.2f} kW)"
                )

        flags.append(
            {
                "date": date,
                "suspicious": suspicious,
                "reasons": "; ".join(reasons),
            }
        )

    flags_df = pd.DataFrame(flags).set_index("date")
    summary_with_flags = summary.join(flags_df[["suspicious", "reasons"]], how="left")
    summary_with_flags["suspicious"] = summary_with_flags["suspicious"].fillna(False)
    summary_with_flags["reasons"] = summary_with_flags["reasons"].fillna("")
    return summary_with_flags, baseline


def print_human_report(summary, baseline):
    print("")
    print("=== POWER USAGE / POSSIBLE THEFT REPORT ===")
    print("")
    if baseline is not None:
        print(f"Estimated global night baseline: {baseline:.2f} kW (median 2–4 AM)")
    else:
        print("No valid 2–4 AM intervals found, cannot compute night baseline.")
    print("")

    suspicious_days = summary[summary["suspicious"]]
    if suspicious_days.empty:
        print("No days flagged as suspicious with current thresholds.")
        return

    print("Suspicious days:")
    for date, row in suspicious_days.iterrows():
        print(
            f"  {date}  | total_kWh={row['total_kwh']:.1f} | "
            f"night_avg_kw={row['night_avg_kw']:.2f} | "
            f"min_kw={row['min_kw']:.2f} | max_kw={row['max_kw']:.2f}"
        )
        if row["reasons"]:
            print(f"     → reasons: {row['reasons']}")
    print("")


def main():
    args = parse_args()

    try:
        df = parse_duke_xml(args.input, tz_name=args.tz)
    except Exception as e:
        print(f"Error parsing XML: {e}", file=sys.stderr)
        sys.exit(1)

    summary = compute_daily_summary(
        df,
        night_start_str=args.night_start,
        night_end_str=args.night_end,
    )

    summary_with_flags, baseline = flag_suspicious_days(
        summary,
        min_night_kw=args.min_night_kw,
        night_multiplier=args.night_multiplier,
    )

    # Save CSV
    summary_with_flags.to_csv(args.output, index=True)
    print(f"Daily summary + flags saved to: {args.output}")

    # Human-readable stdout report
    print_human_report(summary_with_flags, baseline)


if __name__ == "__main__":
    main()
