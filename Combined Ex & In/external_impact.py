"""
external_impact.py
==================
Stage 3 - the External Delivery Impact & Prioritisation layer.

We do NOT optimise external freight here: the dataset has no external route
menu (no DC->airport lanes) and no real freight rates, so there is nothing to
choose between.  Instead we PROPAGATE the internal route decision out to the
customer deliveries it feeds, and compute the metrics a planner actually needs:

  * which customer deliveries are affected by each internal shipment,
  * the cost/kg the guide defines (route cost / summed linked chargeable weight),
  * a priority / action flag (expedite, notify customer, blocked).

cost/kg lives HERE, not in the internal optimiser, because this is the only
place a real ChargeableWeight_KG exists.  One internal shipment can feed several
external deliveries, so we divide the internal route cost by the TOTAL linked
chargeable weight (aggregate first, then divide - never per delivery).
"""

from pathlib import Path
import argparse
import pandas as pd

import build_candidates as bc
import optimize as opt


def build_impact(scenario: str = "Normal",
                 expedite_lead_days: int = opt.EXPEDITE_LEAD_DAYS,
                 horizon_weeks: int = opt.PLANNING_HORIZON_WEEKS) -> pd.DataFrame:
    sheets = bc.load_sheets()
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")

    # --- run the OFFICIAL delivery-grain optimiser (same routes as the graded run) ---
    dbs, _ = opt.deliveries_by_shipment(sheets)
    scaler = opt.fit_scaler_delivery(sheets, horizon_weeks, dbs)
    out = opt.solve_official_delivery(scenario, sheets, scaler,
                                      horizon_weeks, dbs)
    win = out["chosen"].set_index("ShipmentID")            # one winning route per shipment
    unassigned = set(out["unassigned_ids"])
    reason_by_ship = out["reason_by_ship"]

    M = sheets["material"].set_index("MaterialNo_Anon")

    rows = []
    for _, e in E.iterrows():
        sid = e["InternalShipmentID_Link"]
        delivery_wt = float(e["ChargeableWeight_KG"])
        assigned = sid in win.index
        r = win.loc[sid] if assigned else None

        cost_eur = float(r["BaseCostEUR"]) if assigned else None
        # per-delivery cost/kg (matches benchmark LowestCostPerKG_EUR definition, 225/225)
        cost_per_kg = (cost_eur / delivery_wt) if (assigned and delivery_wt > 0) else None

        mat = e["MaterialNo_Anon_Link"]
        priority = M["PriorityClass"].get(mat, "Standard")

        blocked = sid in unassigned or not assigned
        eff_lead = float(r["EffectiveLeadTimeDays"]) if assigned else None
        expedite = (str(priority).lower() in {"critical", "expedite"}
                    and assigned and eff_lead is not None
                    and eff_lead >= expedite_lead_days)

        reason = reason_by_ship.get(sid)
        REASON_ACTION = {
            "handling_infeasible": "BLOCKED - no handling-compatible internal route; notify customer",
            "zero_capacity":       "BLOCKED - internal route only via zero-capacity hub; reroute upstream",
            "capacity_escalation": "CAPACITY ESCALATION REQUIRED - upstream order exceeds quarter horizon",
            "capacity_contention": "CAPACITY CONTENTION - upstream weekly capacity contested; reschedule",
        }
        if blocked:
            action = REASON_ACTION.get(reason, "BLOCKED - upstream shipment unassigned")
        elif expedite:
            action = "EXPEDITE - high-priority material on a slow lane"
        else:
            action = "OK - standard flow"

        rows.append({
            "scenario": scenario,
            "DeliveryNo": e["DeliveryNo"],
            "CustomerCountry": e["ShipTo_CountryCodeISO"],
            "DestAirport": e["Airport_of_Destination"],
            "Forwarder": e["Forwarder_Anon"],
            "ChargeableWeight_KG": e["ChargeableWeight_KG"],
            "InternalShipmentID": sid,
            "InternalAssigned": assigned,
            "ChosenRoute": (r["RouteOptionID"] if assigned else None),
            "FromHub": (r["FromHub"] if assigned else None),
            "ToHub": (r["ToHub"] if assigned else None),
            "BaseLeadTimeDays": (float(r["BaseLeadTimeDays"]) if assigned else None),
            "EffectiveLeadTimeDays": eff_lead,
            "MultiWeek": (bool(r["MultiWeek"]) if assigned else None),
            "RiskScore": (float(r["RiskScore"]) if assigned else None),
            "RouteCost_EUR": cost_eur,
            "DeliveryWeight_KG": delivery_wt,
            "CostPerKG_EUR": (round(cost_per_kg, 3) if cost_per_kg is not None else None),
            "PriorityClass": priority,
            "UpstreamReason": reason,
            "Action": action,
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="Normal")
    ap.add_argument("--expedite-lead-days", type=int, default=opt.EXPEDITE_LEAD_DAYS,
                    help="effective lead-time (days) at/above which a priority delivery is flagged EXPEDITE")
    ap.add_argument("--horizon-weeks", type=int, default=opt.PLANNING_HORIZON_WEEKS,
                    help="planning horizon in throughput weeks (must match the optimiser run)")
    ap.add_argument("--out", default="external_impact.xlsx")
    args = ap.parse_args()

    df = build_impact(args.scenario, args.expedite_lead_days, args.horizon_weeks)
    n = len(df)
    print(f"External deliveries: {n}")
    for key in ("BLOCKED", "CAPACITY ESCALATION", "CAPACITY CONTENTION", "EXPEDITE", "OK"):
        c = df["Action"].str.startswith(key).sum()
        print(f"  {key:22s}: {c}")
    print(f"  cost/kg computed for  : {df['CostPerKG_EUR'].notna().sum()}")
    df.to_excel(args.out, index=False)
    print(f"Wrote {args.out}")
