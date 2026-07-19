"""
scenario_hub_adoption.py
=========================
Hub Capability Adoption Recommender -- v1 (Case B only; confirmed build order in
ChainLab_Hub_Capability_Adoption_Design.md Section 0, 18 Jul 2026).

WHAT THIS FINDS
---------------
Internal shipments unassigned under Reason == "handling_infeasible" specifically because
Case B applies: a Route_Options row already exists for the shipment's exact
(MaterialFamily, StageFrom, StageTo, DisruptionScenario) -- i.e. some hub pair already
ships this family on this lane -- but every such hub pair is missing ONE handling
capability (ESD / Moisture / Lithium / ColdChain) that THIS shipment's specific material
needs, at the origin and/or destination hub.

This is deliberately narrower than Case A (family completely absent from the lane, which
is rare: 0-3/240 shipments) and does not yet cover capacity-reason rescue, pre-emptive
resilience, or score-improvement triggers -- those are next in the confirmed build order,
added as extensions once this is verified.

WHAT IT RECOMMENDS
-------------------
For each Case B shipment, every existing candidate hub pair (already correct family+stage)
is scored by how many capability flags are missing (fewer is a cheaper ask) and by how much
free ROUTE capacity it has (Section 3b: CapacityUnitsPerWeek minus what the current solve
already assigns to that route+week -- NOT a "dedicated per family" assumption, which was
rejected as likely dataset noise).

Recommendations are aggregated to (HubID, MissingFlag) pairs and ranked by fan-out (how
many distinct shipments would be unblocked). The top candidates are then RE-SOLVED against
the full internal optimizer (all 240 shipments, same scenario) with that one capability
hypothetically flipped on, to verify:
  (a) the target shipment(s) actually get picked up (not just technically feasible), and
  (b) no previously-solved shipment gets bumped (hub capacity is a single shared pool per
      Section 3a -- adding new demand to a hub can, in principle, cannibalise it).

Handling-cost assumption (Section 3c): Case B always recommends adopting a capability at a
hub that ALREADY processes the shipment's exact MaterialFamily. Verified in this dataset
that MaterialFamily -> SubstitutionGroup is many-to-one (every material in a family shares
one substitution group; 99/99 families have a single group). So "same family already
processed" mechanically guarantees "same substitution group" -- the Section 3c rule for
"same SubstitutionGroup + same flags already met elsewhere" therefore ALWAYS applies to
Case B, and every Case B recommendation is stamped "handling cost assumption: unchanged."
(The "might increase" branch of that rule only applies to Case A donors from a different
family/substitution group -- not built yet, see module docstring above.)

OUTPUT
------
optimizer_hub_adoption_recommendations.xlsx with sheets:
  Recommendations  - ranked (scenario, HubID, MissingFlag) with fan-out and cost assumption
  ReSolveImpact     - before/after solved-count and avg score for the full 240-shipment
                      re-solve of each top-K recommendation, incl. any cannibalised shipment
  DraftEmails       - ready-to-send outreach email text per recommendation, plain language
  Assumptions       - stated assumptions, all decided 18 Jul 2026 per the design doc
"""
from pathlib import Path
import os
import pandas as pd

import build_candidates as bc
import baseline_v7 as b7

SCENARIOS = b7.SCENARIOS
HORIZON = b7.HORIZON
TOP_K = 5  # how many (HubID, MissingFlag) recommendations to re-solve and verify

FLAG_PLAIN = {
    "LithiumHandlingAvailable": "handle lithium-containing materials",
    "ESDHandlingAvailable": "meet ESD (anti-static / electrostatic-discharge-sensitive) handling requirements",
    "MoistureControlAvailable": "handle moisture-sensitive materials (humidity-controlled storage/handling)",
    "ColdChain": "support cold-chain (temperature-controlled) shipping",
}
# ColdChain check in build_candidates keys off row["...ColdChainAvailable"]; make the flag
# name consistent with the Hub_Constraints column for the recommendation output.
FLAG_HUB_COLUMN = {
    "LithiumHandlingAvailable": "LithiumHandlingAvailable",
    "ESDHandlingAvailable": "ESDHandlingAvailable",
    "MoistureControlAvailable": "MoistureControlAvailable",
    "ColdChain": "ColdChainAvailable",
}


# --------------------------------------------------------------------------
# Step 1: find Case B shipments + their missing-flag detail per candidate hub pair
# --------------------------------------------------------------------------
def missing_flags_for_row(row) -> list[tuple[str, str]]:
    """Returns [(side, flag), ...] for a single pre-capability candidate row, side in
    {'orig','dest'}, flag in FLAG_PLAIN keys. Mirrors build_candidates._route_ok exactly."""
    out = []
    cold_needed = str(row["TempRequirement"]).strip().lower() in bc.COLD_VALUES
    hz = str(row["HazardClass"]).strip()
    hz_flag = bc.HAZARD_TO_HUBFLAG.get(hz)
    for side, prefix in [("orig", "orig_"), ("dest", "dest_")]:
        if cold_needed and not bc._yes(row[f"{prefix}ColdChainAvailable"]):
            out.append((side, "ColdChain"))
        if hz_flag and not bc._yes(row[f"{prefix}{hz_flag}"]):
            out.append((side, hz_flag))
    return out


def case_b_detail(sheets: dict, scenario: str) -> pd.DataFrame:
    """One row per (ShipmentID, candidate RouteOptionID) for Case B shipments only, with
    the specific missing flag(s) and which physical hub (FromHub/ToHub) is short."""
    cand = bc.build_candidates(sheets, scenario=scenario)
    feas = bc.apply_capability(cand)
    all_ids = set(cand["ShipmentID"].unique())
    feas_ids = set(feas["ShipmentID"].unique())
    case_b_ids = all_ids - feas_ids

    sub = cand[cand["ShipmentID"].isin(case_b_ids)].copy()
    if sub.empty:
        return sub.assign(MissingFlag=[], MissingHubID=[], MissingSide=[])

    rows = []
    for _, r in sub.iterrows():
        for side, flag in missing_flags_for_row(r):
            hub_id = r["FromHub"] if side == "orig" else r["ToHub"]
            rows.append({
                "ShipmentID": r["ShipmentID"], "RouteOptionID": r["RouteOptionID"],
                "FromHub": r["FromHub"], "ToHub": r["ToHub"], "TransportMode": r["TransportMode"],
                "MaterialFamily": r["MaterialFamily"], "MaterialNo_Anon": r["MaterialNo_Anon"],
                "MissingSide": side, "MissingHubID": hub_id, "MissingFlag": flag,
                "CapacityUnitsPerWeek": r["CapacityUnitsPerWeek"],
            })
    detail = pd.DataFrame(rows)
    detail["scenario"] = scenario
    return detail


# --------------------------------------------------------------------------
# Step 2: free route capacity (Section 3b) from the CURRENT solve
# --------------------------------------------------------------------------
def current_solve(sheets: dict, scenario: str, scaler: dict, qty_map: dict, wk_map: dict):
    """Runs the actual internal optimizer for one scenario exactly as baseline_v7 does,
    and returns (chosen_df, universe, hub_remaining) so we can read off real per-route
    weekly usage instead of guessing at capacity."""
    ann = b7.capacity_fields(b7.internal_candidates(sheets, scenario), qty_map, "ShipmentID", HORIZON)
    ann = ann.assign(CostForScore=ann["BaseCostEUR"])
    ann["week"] = ann["ShipmentID"].map(wk_map)
    feas = b7.model_feasible(ann)
    feas = b7.score(feas, scaler)
    universe = sheets["internal"]["ShipmentID"].tolist()
    hub_remaining = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()
    res = b7.solve(feas, "ShipmentID", universe, hub_remaining)
    return res, ann


def route_free_capacity(chosen: pd.DataFrame, route_option_id, week, cap_per_week) -> float:
    used = chosen[(chosen["RouteOptionID"] == route_option_id) & (chosen["week"] == week)]["WeeklyFootprint"].sum()
    return float(cap_per_week) - float(used)


# --------------------------------------------------------------------------
# Step 3: rank (HubID, MissingFlag) recommendations
# --------------------------------------------------------------------------
def rank_recommendations(detail: pd.DataFrame, chosen: pd.DataFrame, wk_map: dict) -> pd.DataFrame:
    if detail.empty:
        return detail

    detail = detail.copy()
    detail["week"] = detail["ShipmentID"].map(wk_map)
    detail["FreeRouteCapacity"] = detail.apply(
        lambda r: route_free_capacity(chosen, r["RouteOptionID"], r["week"], r["CapacityUnitsPerWeek"]),
        axis=1)
    # how many DISTINCT flags does this shipment need fixed across ITS candidate rows, at minimum
    n_missing_per_ship_route = detail.groupby(["ShipmentID", "RouteOptionID"])["MissingFlag"].transform("count")
    detail["NMissingOnThisRoute"] = n_missing_per_ship_route

    agg = (detail.groupby(["scenario", "MissingHubID", "MissingFlag"])
           .agg(ShipmentsUnblocked=("ShipmentID", "nunique"),
                ShipmentIDs=("ShipmentID", lambda s: sorted(set(s))),
                AvgFreeRouteCapacity=("FreeRouteCapacity", "mean"),
                MinFreeRouteCapacity=("FreeRouteCapacity", "min"),
                AvgOtherMissingOnRoute=("NMissingOnThisRoute", lambda s: (s - 1).mean()),
                MaterialFamilies=("MaterialFamily", lambda s: sorted(set(s))))
           .reset_index())

    agg = agg.sort_values(
        by=["scenario", "ShipmentsUnblocked", "AvgFreeRouteCapacity"],
        ascending=[True, False, False]).reset_index(drop=True)
    agg["HandlingCostAssumption"] = (
        "Unchanged — hub already processes this exact MaterialFamily today; verified "
        "MaterialFamily->SubstitutionGroup is many-to-one in this dataset, so the material "
        "needing the new capability is guaranteed to share the SubstitutionGroup already "
        "running through this hub (design doc Section 3c).")
    agg["PlainCapabilityAsk"] = agg["MissingFlag"].map(FLAG_PLAIN)
    return agg


# --------------------------------------------------------------------------
# Step 4: re-solve verification for the top-K recommendations
# --------------------------------------------------------------------------
def resolve_with_capability_added(sheets: dict, scenario: str, hub_id: str, flag: str,
                                  scaler: dict, qty_map: dict, wk_map: dict) -> dict:
    """Copies the hub table, flips ONE capability flag to Yes at ONE hub, re-runs the FULL
    240-shipment solve, and returns before/after so we can see real impact (incl. any
    previously-solved shipment that gets bumped by the new demand on shared hub capacity)."""
    hub_col = FLAG_HUB_COLUMN[flag]
    sheets_mod = dict(sheets)
    H2 = sheets["hub"].copy()
    H2.loc[H2["HubID"] == hub_id, hub_col] = "Yes"
    sheets_mod["hub"] = H2

    before, ann_before = current_solve(sheets, scenario, scaler, qty_map, wk_map)
    after, ann_after = current_solve(sheets_mod, scenario, scaler, qty_map, wk_map)

    solved_before = set(before["chosen"]["ShipmentID"])
    solved_after = set(after["chosen"]["ShipmentID"])
    newly_solved = solved_after - solved_before
    bumped = solved_before - solved_after   # previously solved, now NOT solved -> cannibalised

    avg_before = round(before["chosen"]["MinScore"].mean(), 4) if len(before["chosen"]) else None
    avg_after = round(after["chosen"]["MinScore"].mean(), 4) if len(after["chosen"]) else None

    return {
        "scenario": scenario, "HubID": hub_id, "MissingFlag": flag,
        "SolvedBefore": len(solved_before), "SolvedAfter": len(solved_after),
        "AvgScoreBefore": avg_before, "AvgScoreAfter": avg_after,
        "NewlySolvedCount": len(newly_solved), "NewlySolvedShipments": sorted(newly_solved),
        "CannibalisedCount": len(bumped), "CannibalisedShipments": sorted(bumped),
        "NetSolvedChange": len(solved_after) - len(solved_before),
    }


# --------------------------------------------------------------------------
# Step 5: draft outreach emails (design doc Section 7, plain language)
# --------------------------------------------------------------------------
def draft_email(hub_id: str, hub_city: str, hub_country: str, material_families: list,
                flag: str, n_unblocked: int, planner_name: str = "Hon Lam") -> str:
    plain_ask = FLAG_PLAIN[flag]
    fam_str = ", ".join(material_families[:3]) + (", ..." if len(material_families) > 3 else "")
    subject = f"[Action Requested] Can {hub_id} be modified to {plain_ask.split('(')[0].strip()}? — {fam_str}"
    body = f"""Subject: {subject}

Hi {{Hub Engineering Contact}},

We're reviewing shipping routes for {fam_str} through {hub_id} ({hub_city}, {hub_country}) —
you already handle {'this material family' if len(material_families) == 1 else 'these material families'}
at your hub today. Could your team check a few things and get back to us by {{target date}}?

  1. Is your facility able to be modified to {plain_ask}? This wouldn't be a brand-new material
     family for you — you already process {fam_str} here — it would just mean adding this
     as an extra capability.
  2. If you make this change, would it use up any of the capacity you currently have for what you
     already process here?
  3. Roughly, how much would this cost and how long would it take to set up?

For context: this one gap is currently blocking {n_unblocked} shipment(s) that have nowhere else to go.

Happy to jump on a call if any of this needs facility detail we don't have visibility into from our side.

Thanks,
{planner_name}
ChainLab / Supply Chain Planning
"""
    return body


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
EXPECTED_CASE_B_COUNTS = {"Normal": 42, "PrimaryHubDown": 50, "AirCapacityReduced": 42}


def main(out_path: str = "optimizer_hub_adoption_recommendations.xlsx"):
    sheets = bc.load_sheets(hub_disruption=None)
    I = sheets["internal"]
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    hub_meta = sheets["hub"].set_index("HubID")[["City", "Country", "Stage"]].to_dict("index")

    qty_map = I.set_index("ShipmentID")["Qty"].to_dict()
    wk_map = dict(zip(I["ShipmentID"], pd.to_datetime(I["ShipDate"]).dt.isocalendar().week.astype(str)
                      + "-" + pd.to_datetime(I["ShipDate"]).dt.isocalendar().year.astype(str)))

    scaler = b7.canonical_scaler("internal", sheets, E, HORIZON)

    all_detail, all_reco, all_resolve, all_emails = [], [], [], []

    for scenario in SCENARIOS:
        print(f"\n=== {scenario} ===")
        detail = case_b_detail(sheets, scenario)
        n_case_b = detail["ShipmentID"].nunique() if len(detail) else 0
        print(f"  Case B shipments: {n_case_b}")
        expected = EXPECTED_CASE_B_COUNTS.get(scenario)
        assert n_case_b == expected, (
            f"Case B count drifted for {scenario}: expected {expected}, got {n_case_b} "
            f"-- dataset or capability logic changed since this checkpoint was set (18 Jul 2026); "
            f"re-verify against ChainLab_Hub_Capability_Adoption_Design.md before trusting output.")
        if detail.empty:
            continue
        all_detail.append(detail)

        res, ann = current_solve(sheets, scenario, scaler, qty_map, wk_map)
        chosen = res["chosen"]

        reco = rank_recommendations(detail, chosen, wk_map)
        reco.insert(0, "rank", range(1, len(reco) + 1))
        all_reco.append(reco)

        print(f"  Top recommendation: {reco.iloc[0]['MissingHubID']} needs "
              f"{reco.iloc[0]['MissingFlag']} -> unblocks {reco.iloc[0]['ShipmentsUnblocked']} shipment(s)")

        top = reco.head(TOP_K)
        for _, r in top.iterrows():
            impact = resolve_with_capability_added(
                sheets, scenario, r["MissingHubID"], r["MissingFlag"], scaler, qty_map, wk_map)
            all_resolve.append(impact)
            print(f"    re-solve {r['MissingHubID']}/{r['MissingFlag']}: "
                  f"solved {impact['SolvedBefore']}->{impact['SolvedAfter']} "
                  f"(net {impact['NetSolvedChange']:+d}, cannibalised {impact['CannibalisedCount']})")

            meta = hub_meta.get(r["MissingHubID"], {})
            email = draft_email(
                r["MissingHubID"], meta.get("City", "?"), meta.get("Country", "?"),
                r["MaterialFamilies"], r["MissingFlag"], r["ShipmentsUnblocked"])
            all_emails.append({
                "scenario": scenario, "HubID": r["MissingHubID"], "MissingFlag": r["MissingFlag"],
                "ShipmentsUnblocked": r["ShipmentsUnblocked"], "EmailDraft": email})

    detail_all = pd.concat(all_detail, ignore_index=True) if all_detail else pd.DataFrame()
    reco_all = pd.concat(all_reco, ignore_index=True) if all_reco else pd.DataFrame()
    resolve_all = pd.DataFrame(all_resolve)
    emails_all = pd.DataFrame(all_emails)

    if len(reco_all):
        reco_out = reco_all.copy()
        reco_out["ShipmentIDs"] = reco_out["ShipmentIDs"].apply(lambda l: ", ".join(l))
        reco_out["MaterialFamilies"] = reco_out["MaterialFamilies"].apply(lambda l: ", ".join(l))
    else:
        reco_out = reco_all

    if len(resolve_all):
        resolve_out = resolve_all.copy()
        resolve_out["NewlySolvedShipments"] = resolve_out["NewlySolvedShipments"].apply(lambda l: ", ".join(l))
        resolve_out["CannibalisedShipments"] = resolve_out["CannibalisedShipments"].apply(lambda l: ", ".join(l))
    else:
        resolve_out = resolve_all

    assumptions = pd.DataFrame([
        ("Scope (v1, 18 Jul 2026)", "Case B ONLY: shipments where a route already exists for their exact "
         "MaterialFamily+StageFrom+StageTo+DisruptionScenario, but every such hub pair is missing one "
         "capability flag for this shipment's specific material. Case A (family totally absent), "
         "capacity-reason rescue, pre-emptive resilience, and score-improvement triggers are NOT yet "
         "included -- confirmed next in the build order per the design doc Section 0."),
        ("Donor search", "For Case B, the hub pair(s) to fix ARE the existing candidate routes for this "
         "shipment's family+stage+scenario (no cross-family donor search needed, unlike Case A)."),
        ("No IsPrimary filter", "Recommendations are not filtered or ranked by Route_Options.IsPrimary, "
         "per the 18 Jul decision -- ALT hubs are treated on equal footing."),
        ("Route capacity ranking", "Free route capacity = CapacityUnitsPerWeek minus WeeklyFootprint "
         "actually assigned to that RouteOptionID+week in the CURRENT solve (not a per-family-dedicated "
         "capacity assumption, which was rejected as likely dataset noise -- design doc Section 3b)."),
        ("Handling-cost assumption", "Always 'unchanged' for Case B: verified MaterialFamily->SubstitutionGroup "
         "is many-to-one in this dataset (99/99 families map to exactly one SubstitutionGroup), so a hub "
         "already running the shipment's MaterialFamily is guaranteed to already run its SubstitutionGroup "
         "-- design doc Section 3c's 'same group, same cost' rule always applies here."),
        ("Re-solve verification", "Every top-5-per-scenario recommendation is verified by flipping the one "
         "capability flag at that hub and re-solving the FULL 240-shipment internal optimizer for that "
         "scenario, comparing solved-count and avg MinScore before/after, and checking for any previously-"
         "solved shipment that gets bumped (hub capacity is a single shared pool, design doc Section 3a)."),
        ("Scaler", "Uses the canonical fixed scaler (Normal+PrimaryHubDown+AirCapacityReduced union), "
         "identical to the official baseline -- scores are directly comparable to optimizer_internal_baseline.xlsx."),
    ], columns=["Assumption", "Detail"])

    with pd.ExcelWriter(out_path) as xw:
        reco_out.to_excel(xw, sheet_name="Recommendations", index=False)
        resolve_out.to_excel(xw, sheet_name="ReSolveImpact", index=False)
        emails_all.to_excel(xw, sheet_name="DraftEmails", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
        if len(detail_all):
            detail_all.drop(columns=["week"], errors="ignore").to_excel(
                xw, sheet_name="CaseBDetail", index=False)

    print(f"\nWrote {out_path}")
    return {"recommendations": reco_all, "resolve_impact": resolve_all, "emails": emails_all}


if __name__ == "__main__":
    main()
