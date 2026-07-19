"""
apply_hub_adoption_changes.py
==============================
Companion to the dashboard's "Facility Capability Adoption" card (chainlab_dashboard.html,
Actions tab -- see ChainLab_Hub_Capability_Adoption_Design.md Section 0/7 for the full flow).

The dashboard's checklist runs entirely in the browser and never writes to a live database --
per artifact policy it only holds state in memory for the session and lets the planner export
a changelog (JSON or CSV) once a hub's facility change is confirmed COMPLETE (checklist item 4).
This script is the other half of that loop: it takes that exported changelog and actually
patches the corresponding Hub_Constraints rows in a COPY of the master dataset, so the change
becomes real and the optimizer picks it up on the next run.

This is intentionally a two-step, human-gated process, not an automatic write:
  1. Planner ticks the dashboard checklist as engineering responses come in.
  2. Only once "modification completed" is checked does the dashboard reveal the exact
     proposed row/column change and let the planner "Apply" it to a session changelog.
  3. Planner exports that changelog (stays reviewable/reopenable in the browser until then).
  4. Planner runs THIS script against the exported changelog to actually patch the dataset.

Usage:
    IFX_WORKBOOK=/path/to/IFX_LOG_Master_Data-anonymised_StudentVersion.xlsx \\
        python3 apply_hub_adoption_changes.py hub_adoption_changelog.json

Produces:
    <dataset name>_HubAdoptionApplied.xlsx  -- full workbook, every sheet preserved unchanged
                                                except Hub_Constraints, which gets the patched
                                                cells. All other sheets (Internal_Shipments,
                                                External Shipments, Route_Options,
                                                Material_Families, Hackathon_Guide) pass through
                                                byte-for-byte via pandas read/write.
    <dataset name>_HubAdoptionApplied_log.csv -- audit trail: every cell actually changed, the
                                                old and new value, and when this script ran.

Does NOT touch Route_Options (no new-route-row logic yet -- that's Case A, not built in v1).
"""
from pathlib import Path
import argparse
import json
import sys
from datetime import datetime

import pandas as pd

DEFAULT_WB = Path(__file__).resolve().parents[1] / (
    "Infineon Dataset/Dataset-anonymised/Dataset-anonymised/"
    "IFX_LOG_Master_Data-anonymised_StudentVersion.xlsx")


def load_changelog(path: str) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    if p.suffix.lower() == ".csv":
        df = pd.read_csv(p)
        df = df.rename(columns={
            "Hub": "hub", "Column": "col", "OldValue": "old", "NewValue": "neu",
            "Cost": "cost", "LeadTime": "lead", "AppliedAt": "when"})
        return df.to_dict("records")
    raise ValueError(f"Unrecognised changelog format: {p.suffix} (expected .json or .csv)")


def apply_changes(workbook: Path, changelog: list[dict]) -> tuple[Path, Path]:
    xl = pd.ExcelFile(workbook)
    sheets = {name: xl.parse(name) for name in xl.sheet_names}
    H = sheets["Hub_Constraints"]

    applied_log = []
    for change in changelog:
        hub, col, old, neu = change["hub"], change["col"], change.get("old"), change["neu"]
        mask = H["HubID"] == hub
        if not mask.any():
            print(f"  SKIPPED — HubID {hub!r} not found in Hub_Constraints", file=sys.stderr)
            continue
        if col not in H.columns:
            print(f"  SKIPPED — column {col!r} not found in Hub_Constraints", file=sys.stderr)
            continue
        current = H.loc[mask, col].iloc[0]
        H.loc[mask, col] = neu
        applied_log.append({
            "HubID": hub, "Column": col, "ValueBeforeThisScript": current,
            "ExpectedOldFromChangelog": old, "NewValue": neu,
            "Cost": change.get("cost"), "LeadTime": change.get("lead"),
            "ConfirmedInDashboardAt": change.get("when"),
            "AppliedByScriptAt": datetime.now().isoformat(timespec="seconds"),
        })
        print(f"  Applied: {hub}.{col} -> {neu!r} (was {current!r})")

    sheets["Hub_Constraints"] = H

    stem = workbook.stem
    out_wb = workbook.with_name(f"{stem}_HubAdoptionApplied.xlsx")
    with pd.ExcelWriter(out_wb) as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)

    out_log = workbook.with_name(f"{stem}_HubAdoptionApplied_log.csv")
    pd.DataFrame(applied_log).to_csv(out_log, index=False)

    return out_wb, out_log


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("changelog", help="Path to hub_adoption_changelog.json or .csv exported from the dashboard")
    ap.add_argument("--workbook", default=None, help="Override the master dataset path (default: IFX_WORKBOOK env var or project default)")
    args = ap.parse_args()

    import os
    workbook = Path(args.workbook or os.environ.get("IFX_WORKBOOK", DEFAULT_WB))
    if not workbook.exists():
        sys.exit(f"Dataset not found: {workbook}")

    changelog = load_changelog(args.changelog)
    print(f"Loaded {len(changelog)} change(s) from {args.changelog}")
    if not changelog:
        sys.exit("Nothing to apply — changelog is empty.")

    out_wb, out_log = apply_changes(workbook, changelog)
    print(f"\nWrote {out_wb}")
    print(f"Wrote {out_log}")
    print("\nOriginal dataset untouched. Re-run the optimizer scripts against the "
          "*_HubAdoptionApplied.xlsx copy (IFX_WORKBOOK=... env var) to see the change reflected.")
