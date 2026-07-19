"""
scenario_external_official.py
=============================
OFFICIAL external-shipment MinScore per Infineon instructor clarification.

The official external score is a DIRECT score-and-rank calculation on three
delivery-level best-value columns already supplied in `External Shipments`:

    ExternalMinScore = 0.4*norm(BestLeadTimeDays)
                     + 0.4*norm(LowestCostPerKG_EUR)
                     + 0.2*norm(LowestRiskScore)

It is NOT a route-selection optimiser. Those three values are the dataset's own
best-per-delivery results, identical across every candidate route, so a PuLP
route model would have nothing to choose between. One score per DeliveryNo.

Rules (instructor):
  * Use ONLY External Shipments.{BestLeadTimeDays, LowestCostPerKG_EUR,
    LowestRiskScore}. Do NOT use route_options.{BaseLeadTimeDays, BaseCostEUR,
    RiskScore} and do NOT recompute BaseCostEUR/ChargeableWeight_KG here
    (LowestCostPerKG_EUR is already provided).
  * NO join to internal_shipments / route_options / hub_constraints /
    material_families.
  * ONE fixed min-max scaler across all 225 deliveries (no per-scenario refit;
    the supplied columns are not scenario-specific, so the official external
    MinScore is the SAME under Normal / PrimaryHubDown / AirCapacityReduced /
    Port congestion).
  * Route / capacity / cold-chain / expedite / sustainability / disruption
    analyses are kept SEPARATELY as extensions and their route-dependent score
    is called ScenarioRouteScore, never ExternalMinScore.
  * Never overwrites the source dataset; writes a new scored workbook.

Output: optimizer_external_official_scores.xlsx
    ExternalScores  - 225 delivery-level scores (+ normalised terms + rank)
    Summary         - mean / median / std / min / max
    ScalerBounds    - min/max used for each of the three features
    DataQuality     - missing / invalid counts
    Assumptions     - instructor-approved definition
"""
from pathlib import Path
import argparse
import numpy as np
import pandas as pd

W_LEAD, W_COST, W_RISK = 0.40, 0.40, 0.20
SCORE_COLS = ["BestLeadTimeDays", "LowestCostPerKG_EUR", "LowestRiskScore"]
_DEFAULT_WB = Path(__file__).resolve().parents[1] / (
    "Infineon Dataset/Dataset-anonymised/Dataset-anonymised/"
    "IFX_LOG_Master_Data-anonymised_StudentVersion.xlsx")
import os
WORKBOOK = Path(os.environ.get("IFX_WORKBOOK", _DEFAULT_WB))


def minmax(series, lower, upper):
    if upper == lower:
        return pd.Series(0.0, index=series.index)
    return (series - lower) / (upper - lower)


def score_external_official(external: pd.DataFrame):
    required = ["DeliveryNo"] + SCORE_COLS
    missing = set(required) - set(external.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    out = external[required].copy()
    if out["DeliveryNo"].duplicated().any():
        raise ValueError("DeliveryNo must be unique")
    if out[SCORE_COLS].isna().any().any():
        raise ValueError("Official external score fields contain missing values")

    bounds = {c: (float(out[c].min()), float(out[c].max())) for c in SCORE_COLS}
    out["n_BestLeadTimeDays"]    = minmax(out["BestLeadTimeDays"],    *bounds["BestLeadTimeDays"])
    out["n_LowestCostPerKG_EUR"] = minmax(out["LowestCostPerKG_EUR"], *bounds["LowestCostPerKG_EUR"])
    out["n_LowestRiskScore"]     = minmax(out["LowestRiskScore"],     *bounds["LowestRiskScore"])
    out["ExternalMinScore"] = (
        W_LEAD * out["n_BestLeadTimeDays"]
        + W_COST * out["n_LowestCostPerKG_EUR"]
        + W_RISK * out["n_LowestRiskScore"])
    out["ExternalRank"] = out["ExternalMinScore"].rank(method="dense", ascending=True).astype(int)
    return out, bounds


def _checks(scores: pd.DataFrame, source: pd.DataFrame):
    assert len(scores) == 225, f"expected 225 rows, got {len(scores)}"
    assert scores["DeliveryNo"].nunique() == 225, "DeliveryNo not unique 225"
    assert scores["ExternalMinScore"].between(0, 1).all(), "MinScore outside [0,1]"
    assert scores[SCORE_COLS].notna().all().all(), "null in score inputs"
    expected = (W_LEAD * scores["n_BestLeadTimeDays"]
                + W_COST * scores["n_LowestCostPerKG_EUR"]
                + W_RISK * scores["n_LowestRiskScore"])
    assert np.allclose(scores["ExternalMinScore"], expected), "score != 40/40/20 recombination"
    # independence: uses only the three supplied columns + DeliveryNo
    assert set(source.columns) >= set(SCORE_COLS + ["DeliveryNo"]), "source missing supplied cols"
    print("  [OK] all official-external checks pass (225 unique, in [0,1], reconstructs 40/40/20)")


def main(workbook: Path = WORKBOOK, out_path="optimizer_external_official_scores.xlsx"):
    E = pd.read_excel(workbook, sheet_name="External Shipments")
    scores, bounds = score_external_official(E)
    _checks(scores, E)

    s = scores["ExternalMinScore"]
    summary = pd.DataFrame([{
        "deliveries": len(scores),
        "average": round(s.mean(), 4), "median": round(s.median(), 4),
        "std": round(s.std(), 4), "min": round(s.min(), 4), "max": round(s.max(), 4),
        "best_delivery": scores.loc[s.idxmin(), "DeliveryNo"],
        "worst_delivery": scores.loc[s.idxmax(), "DeliveryNo"]}])

    scaler_bounds = pd.DataFrame(
        [{"feature": c, "weight": w, "min": bounds[c][0], "max": bounds[c][1]}
         for c, w in zip(SCORE_COLS, [W_LEAD, W_COST, W_RISK])])

    dq = pd.DataFrame([{
        "column": c,
        "missing": int(E[c].isna().sum()),
        "non_numeric": int(pd.to_numeric(E[c], errors="coerce").isna().sum() - E[c].isna().sum()),
        "min": float(E[c].min()), "max": float(E[c].max())} for c in SCORE_COLS]
        + [{"column": "DeliveryNo", "missing": int(E["DeliveryNo"].isna().sum()),
            "non_numeric": None, "min": None, "max": None,
            "duplicates": int(E["DeliveryNo"].duplicated().sum())}])

    assumptions = pd.DataFrame([
        ("Scoring definition (instructor-approved)",
         "Official external MinScore = 0.40*norm(BestLeadTimeDays) + 0.40*norm(LowestCostPerKG_EUR) "
         "+ 0.20*norm(LowestRiskScore). Direct score-and-rank on the three delivery-level best-value "
         "columns supplied in External Shipments."),
        ("Not a route optimiser",
         "The three supplied values are the dataset's own best-per-delivery results and are identical "
         "across candidate routes, so there is nothing for a route-selection model to choose. PuLP "
         "route selection is NOT used for the official external baseline."),
        ("Columns used",
         "ONLY External Shipments.BestLeadTimeDays, .LowestCostPerKG_EUR, .LowestRiskScore. "
         "route_options.{BaseLeadTimeDays,BaseCostEUR,RiskScore} are NOT used; BaseCostEUR/ChargeableWeight_KG "
         "is NOT recomputed (LowestCostPerKG_EUR is already provided)."),
        ("Independence",
         "No join to internal_shipments, route_options, hub_constraints or material_families. "
         "One score per DeliveryNo."),
        ("Normalisation",
         "ONE fixed min-max scaler across all 225 deliveries (bounds in ScalerBounds). Not refit by "
         "disruption scenario."),
        ("Scenario invariance",
         "The supplied columns are not scenario-specific, so the official external MinScore is the SAME "
         "under Normal / PrimaryHubDown / AirCapacityReduced / Port congestion. Route/disruption effects "
         "are separate ScenarioRouteScore extensions and do NOT change this official score."),
        ("Source integrity", "Source dataset is not modified; this is a new scored workbook."),
    ], columns=["Assumption", "Detail"])

    keep = ["DeliveryNo", "BestLeadTimeDays", "LowestCostPerKG_EUR", "LowestRiskScore",
            "n_BestLeadTimeDays", "n_LowestCostPerKG_EUR", "n_LowestRiskScore",
            "ExternalMinScore", "ExternalRank"]
    with pd.ExcelWriter(out_path) as xw:
        scores[keep].to_excel(xw, sheet_name="ExternalScores", index=False)
        summary.to_excel(xw, sheet_name="Summary", index=False)
        scaler_bounds.to_excel(xw, sheet_name="ScalerBounds", index=False)
        dq.to_excel(xw, sheet_name="DataQuality", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)

    print(f"  wrote {out_path}")
    print(summary.to_string(index=False))
    return scores, summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", default=str(WORKBOOK))
    args = ap.parse_args()
    main(Path(args.workbook))
