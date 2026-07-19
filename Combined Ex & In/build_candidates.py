"""
build_candidates.py
===================
Stage 1 of the ChainLab internal route optimiser.

Turns the five normalised sheets into ONE flat "candidate" table where
    one row = one feasible route a shipment could take.

Pipeline (matches the agreed design):
    internal_shipments
      -> material_families   (handling requirements)
      -> route_options       (the route menu, filtered to the active scenario)
      -> hub_constraints x2  (origin + destination capability & capacity)

Key rules baked in here (all verified against the workbook):
  * Scenario filter is applied to route_options ONLY (route network layer).
  * Hubs are joined on hub_id ONLY - the two DisruptionScenario columns
    describe different things and must never be matched to each other.
  * Hub-side disruption (Hub_Constraints.DisruptionScenario +
    CapacityReductionPct) is a SEPARATE axis controlled by the
    `hub_disruption` argument of load_sheets():
        None (default)      -> clean network, no capacity cuts (true baseline)
        "Port congestion"   -> cut only hubs tagged Port congestion (guide S1)
        "Labor shortage"    -> cut only hubs tagged Labor shortage
        "Weather disruption"-> cut only hubs tagged Weather disruption
        "all"               -> cut every tagged hub at once (legacy behaviour
                               of runs before 18 Jul 2026, kept for
                               reproducibility of earlier outputs)
  * Handling capability uses the dedicated boolean columns, not text search.
  * Capability assumption: BOTH origin and destination hubs must satisfy the
    requirement.  (Swap ASSUME_BOTH_HUBS to False to test destination-only.)
  * `*_pct` fields are decimal fractions (0.35, 0.90 ...), NOT 0-100.  We
    rename them to `*_ratio` on load so nobody divides by 100 downstream.
"""

from pathlib import Path
import pandas as pd

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
import os
_DEFAULT_WB = Path(__file__).resolve().parents[1] / (
    "Infineon Dataset/Dataset-anonymised/Dataset-anonymised/"
    "IFX_LOG_Master_Data-anonymised_StudentVersion.xlsx"
)
WORKBOOK = Path(os.environ.get("IFX_WORKBOOK", _DEFAULT_WB))

ASSUME_BOTH_HUBS = True   # both origin+dest must meet handling reqs (stated assumption)

# Map each hazard class to the hub boolean that authorises it.
HAZARD_TO_HUBFLAG = {
    "ESD Sensitive":       "ESDHandlingAvailable",
    "Moisture Sensitive":  "MoistureControlAvailable",
    "Lithium Handling":    "LithiumHandlingAvailable",
}
COLD_VALUES = {"cold chain", "coldchain", "cold-chain"}


def _yes(v) -> bool:
    """Excel stores these flags as the strings 'Yes'/'No'."""
    return str(v).strip().lower() in {"yes", "true", "1"}


# --------------------------------------------------------------------------
# Load + light clean
# --------------------------------------------------------------------------
HUB_DISRUPTION_LEGACY_ALL = "all"   # pre-18-Jul behaviour: cut every tagged hub


def load_sheets(workbook: Path = WORKBOOK,
                hub_disruption: str | None = None) -> dict[str, pd.DataFrame]:
    """Load the five sheets and pre-compute hub capacity.

    hub_disruption selects the HUB-SIDE disruption axis (guide Scenario 1):
      None                  -> no CapacityReductionPct applied anywhere.
      "Port congestion" /
      "Labor shortage" /
      "Weather disruption"  -> apply each hub's CapacityReductionPct only
                               where Hub_Constraints.DisruptionScenario
                               matches that value.
      "all"                 -> legacy: apply the cut to every tagged hub
                               (what all runs before 18 Jul 2026 did).

    This is independent of the ROUTE-side scenario (Normal / PrimaryHubDown /
    AirCapacityReduced) passed to build_candidates() - the two DisruptionScenario
    columns are different axes and are never joined to each other.
    """
    xl = pd.ExcelFile(workbook)
    I = xl.parse("Internal_Shipments")
    M = xl.parse("Material_Families")
    R = xl.parse("Route_Options")
    H = xl.parse("Hub_Constraints")

    # 1) Missing hazard means "no special handling" -> explicit "None".
    M["HazardClass"] = M["HazardClass"].fillna("None")

    # 2) Rename the fraction fields so the 0-100 trap can't recur.
    H = H.rename(columns={
        "CurrentUtilizationPct": "current_utilization_ratio",
        "MaxUtilizationPct":     "max_utilization_ratio",
        "CapacityReductionPct":  "capacity_reduction_ratio",
    })
    H["capacity_reduction_ratio"] = H["capacity_reduction_ratio"].fillna(0.0)

    # 3) Decide which hubs actually take their capacity cut this run.
    if hub_disruption is None:
        applied = pd.Series(0.0, index=H.index)
    elif hub_disruption == HUB_DISRUPTION_LEGACY_ALL:
        applied = H["capacity_reduction_ratio"]
    else:
        valid = set(H["DisruptionScenario"].dropna().unique()) - {"None"}
        if hub_disruption not in valid:
            raise ValueError(
                f"hub_disruption={hub_disruption!r} not in Hub_Constraints."
                f"DisruptionScenario; valid values: {sorted(valid)} or "
                f"'{HUB_DISRUPTION_LEGACY_ALL}' or None")
        applied = H["capacity_reduction_ratio"].where(
            H["DisruptionScenario"] == hub_disruption, 0.0)
    H["applied_reduction_ratio"] = applied

    # 4) Pre-compute each hub's remaining weekly capacity ONCE.
    #    ceiling = weekly * (max_util * (1 - applied_reduction))
    #    remaining = max(0, ceiling - current_load)
    ceiling = (
        H["WeeklyCapacityUnits"]
        * (H["max_utilization_ratio"] * (1.0 - H["applied_reduction_ratio"]))
    )
    current_load = H["WeeklyCapacityUnits"] * H["current_utilization_ratio"]
    H["remaining_capacity_units"] = (ceiling - current_load).clip(lower=0)

    sheets = {"internal": I, "material": M, "route": R, "hub": H}
    sheets["hub_disruption"] = hub_disruption   # metadata for reporting
    return sheets


# --------------------------------------------------------------------------
# Candidate build
# --------------------------------------------------------------------------
def build_candidates(sheets: dict[str, pd.DataFrame],
                     scenario: str = "Normal") -> pd.DataFrame:
    I, M, R, H = sheets["internal"], sheets["material"], sheets["route"], sheets["hub"]

    # internal -> material (bring handling requirements onto every shipment)
    im = I.merge(
        M[["MaterialNo_Anon", "HazardClass", "TempRequirement", "PriorityClass"]],
        on="MaterialNo_Anon", how="left",
    )

    # route menu for the ACTIVE scenario only, and only routes that are usable
    routes = R[(R["DisruptionScenario"] == scenario) & (R["AvailableFlag"] == "Yes")]

    # internal x route on the lane key (family + stage-from + stage-to)
    cand = im.merge(
        routes, on=["MaterialFamily", "StageFrom", "StageTo"],
        how="inner", suffixes=("", "_route"),
    )
    # This is the pre-capability count the guide expects (1,137 for Normal).
    precapability_rows = len(cand)

    # attach origin + destination hub attributes (hub_id join only)
    hub_cols = ["HubID", "remaining_capacity_units", "ColdChainAvailable",
                "ESDHandlingAvailable", "MoistureControlAvailable",
                "LithiumHandlingAvailable"]
    cand = cand.merge(
        H[hub_cols].add_prefix("orig_"),
        left_on="FromHub", right_on="orig_HubID", how="left",
    ).merge(
        H[hub_cols].add_prefix("dest_"),
        left_on="ToHub", right_on="dest_HubID", how="left",
    )
    # .attrs is set LAST because pandas merges do not carry attrs forward.
    cand.attrs["precapability_rows"] = precapability_rows
    return cand


def _route_ok(row, require_cold_chain: bool = True, require_hazard: bool = True,
              require_both_hubs: bool = True) -> bool:
    """Handling compatibility for a single candidate row.

    The three switches let a scenario isolate ONE capability rule without
    disabling the others (guide Scenario 2 counterfactual): turning off
    require_cold_chain must NOT let hazardous materials through hubs that
    lack ESD / moisture / lithium handling, so require_hazard stays True.
    """
    hubs = ["orig_", "dest_"] if require_both_hubs else ["dest_"]

    cold_needed = str(row["TempRequirement"]).strip().lower() in COLD_VALUES
    hz = str(row["HazardClass"]).strip()
    hz_flag = HAZARD_TO_HUBFLAG.get(hz)  # None for "None"

    for p in hubs:
        if require_cold_chain and cold_needed and not _yes(row[f"{p}ColdChainAvailable"]):
            return False
        if require_hazard and hz_flag and not _yes(row[f"{p}{hz_flag}"]):
            return False
    return True


def apply_capability(cand: pd.DataFrame, require_cold_chain: bool = True,
                     require_hazard: bool = True,
                     require_both_hubs: bool | None = None) -> pd.DataFrame:
    """Filter candidates to handling-compatible routes.

    Defaults reproduce the original behaviour exactly (all rules on, both
    hubs must qualify per ASSUME_BOTH_HUBS).
    """
    if require_both_hubs is None:
        require_both_hubs = ASSUME_BOTH_HUBS
    return cand[cand.apply(
        lambda r: _route_ok(r, require_cold_chain, require_hazard, require_both_hubs),
        axis=1)].copy()


def feasibility_summary(feas: pd.DataFrame, all_ids) -> dict:
    per = feas.groupby("ShipmentID").size()
    ids = set(all_ids)
    return {
        "zero":      len(ids - set(per.index)),
        "exactly_1": int((per == 1).sum()),
        "multiple":  int((per > 1).sum()),
    }


# --------------------------------------------------------------------------
# CLI / self-test
# --------------------------------------------------------------------------
if __name__ == "__main__":
    sheets = load_sheets()
    cand = build_candidates(sheets, scenario="Normal")

    pre = cand.attrs["precapability_rows"]
    print(f"Normal candidate combinations (pre-capability): {pre}")
    assert pre == 1137, f"expected 1137, got {pre}"   # hard checkpoint
    print("  OK - matches the guide's 1,137 benchmark")

    feas = apply_capability(cand)
    s = feasibility_summary(feas, sheets["internal"]["ShipmentID"])
    print(f"After both-hubs capability filter: "
          f"zero={s['zero']}  exactly-1={s['exactly_1']}  multiple={s['multiple']}")
