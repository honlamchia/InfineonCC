# Hub Capability / Material-Family Adoption Recommender — Design Review

**Written:** 18 Jul 2026, hackathon Day 1. Reviews and extends Hon Lam's proposed feature: after the internal optimizer runs, recommend specific hubs to request facility changes so that shipments currently stranded (or at risk) can get a feasible, competitive route.

All numbers below are computed live against the current authoritative pipeline (`pulp_optimizer/Baseline/build_candidates.py` + `baseline_v7.py`) and the current dataset — not estimated.

---

## 0. Consolidated v1 scope — CONFIRM BEFORE BUILD

Everything below §0 is the accumulated reasoning/rationale from the review; this section is the flattened, final answer to "what are we actually building, in what order." Nothing has been implemented yet — this is the checkpoint before writing `scenario_hub_adoption.py`.

**Build order (highest priority first):**

1. **Case B — PRIORITY 1.** Route already exists for the shipment's (MaterialFamily, StageFrom, StageTo, DisruptionScenario); origin and/or destination hub is missing one specific capability flag (ESD/Moisture/Lithium/ColdChain) for this shipment's specific material. Dominant real-data failure mode (42–50/240 shipments per scenario), cheapest ask (add one flag to a hub already running the family). This is the core of the v1 build — get this working and verified end-to-end first.
2. **Case A.** Zero `Route_Options` row exists at all for (MaterialFamily, StageFrom, StageTo, DisruptionScenario). Rare (0–3/240) but real, and reuses the same donor-hub-search mechanism as Case B — build second, as an extension once Case B's pipeline (donor search → filter → rank → re-solve → email) is working.
3. **Capacity-reason rescue.** Shipments with `Reason ∈ {zero_capacity, capacity_contention, capacity_escalation}` — capability-feasible but losing the capacity fight. Same donor-hub-search mechanism, triggered by a different `Reason` code and scored by added capacity headroom rather than a missing flag.
4. **Pre-emptive resilience.** Lanes with exactly one capability-and-capacity-compatible hub pair today (single point of failure) — nothing is broken yet, but flag it before a disruption scenario strands the lane.
5. **Score-improvement opportunities.** Already-assigned shipments whose `MinScore` is a clear outlier vs. peers on a comparable lane — lower priority, exploratory/informational only.

**Explicitly OUT of scope for now (deferred, not building):**

- **Concentration / route-diversity risk** (was §2 item 4 — flagging low route diversity even with zero current infeasibility). Cut per Hon Lam's 18 Jul call. Left in §2 below for the record, but not part of the v1 build; revisit only if there's time after 1–5 above are done and verified.

**Confirmed mechanics carried into the build (from the sections below — not re-litigating, just listing what's locked in):**

- Target reason source: `baseline_v7.classify()` output on the `Unassigned` sheet, extended with the capacity-reason and at-risk/weak-score categories above.
- Donor search: same `StageFrom`/`StageTo` hub-type pair, same `DisruptionScenario`, different current `MaterialFamily` (Case A) or same family but missing flag (Case B).
- No `IsPrimary` filter or preference (§4).
- Rank donors by free route capacity = `CapacityUnitsPerWeek − Σ WeeklyFootprint` actually assigned in the current solve, not a raw dedicated-capacity assumption (§3b).
- Handling-cost assumption: same `SubstitutionGroup` + same flags → cost unchanged; different `SubstitutionGroup` → cost flagged as "might increase, needs hub confirmation" (§3c).
- `SubstitutionGroup`-based rerouting is a separate, parallel recommendation type, always shown as low-confidence/conditional pending Infineon confirmation — not folded into the hub-adoption logic (§4).
- Every top-K recommendation gets re-solved against the full 240-shipment problem before being presented, to confirm it actually helps and doesn't bump a previously-solved shipment (§6).
- Output includes the §7 outreach email (template + reason-specific checklist), addressed to hub engineering for Cases A/B/capacity-rescue/resilience/score-improvement, and to Materials/Quality Engineering for substitution-based recommendations.

**Status (18 Jul, updated):** Case B is built and verified — `pulp_optimizer/Baseline/scenario_hub_adoption.py` → `optimizer_hub_adoption_recommendations.xlsx`, plus a dashboard card and a companion apply script (§8 below covers all three). Case A, capacity-rescue, pre-emptive resilience, and score-improvement are still not built — next in the confirmed order above.

---

## 1. Your logic, checked

The core mechanism is sound: find an existing route option with the same `StageFrom`→`StageTo` hub-type pair (confirmed below that hub `Stage` matches `Route_Options.StageFrom/StageTo` with **zero mismatches across all 3,819 rows** — this is a safe, reliable join key), currently carrying a *different* `MaterialFamily`, and ask that hub pair to additionally support the stranded material family. Checking the candidate hub's existing handling flags (ESD/Moisture/Lithium/ColdChain) against the stranded shipment's actual requirement, and checking capacity before recommending, are both the right instincts.

**One correction, though, and it's the important one:** in the actual optimizer, "no feasible route option" is not one failure mode — the code (`baseline_v7.classify()`) already splits unassigned shipments into four reasons: `handling_infeasible`, `zero_capacity`, `capacity_escalation`, `capacity_contention`. What you described — "optimiser cannot allocate a route option that fulfills the correct material family, correct stage from and correct stage to" — is specifically **`handling_infeasible`**. The other three reasons are shipments that DO have a valid material+stage match but lose out purely on weekly throughput; those need a capacity conversation, not a material-family-adoption conversation. Your feature should filter the `Unassigned` sheet to `Reason == "handling_infeasible"` before doing anything else (with one caveat in §2 — capacity-classified shipments can *also* benefit from this feature, just via a different mechanism).

**And within `handling_infeasible` itself, there are two genuinely different sub-cases**, and your description only covers one of them:

| | Case A — family absent from the lane | Case B — family present, one flag missing |
|---|---|---|
| What it means | Zero `Route_Options` rows exist for (MaterialFamily, StageFrom, StageTo) in that scenario at all | A matching row exists (hub pair already ships this exact family on this lane), but the origin and/or destination hub is missing one specific capability (ESD / Moisture / Lithium / ColdChain) for *this shipment's specific material* |
| Your proposed fix | Correct — find a different-family hub pair on the same stage-to-stage lane, propose it adopt the family | Wrong target — the family is already there; the fix is a single boolean flag at a hub that's *already serving this family*, which is a smaller, cheaper ask |
| Actual count (Normal scenario, clean network) | **0 of 240** shipments | **42 of 240** shipments |
| Actual count (PrimaryHubDown) | **3** (`SIM-00188`, `SIM-00186`, `SIM-00173`) | **50** |
| Actual count (AirCapacityReduced) | **0** | **42** |

**This matters a lot for prioritization: Case A — the scenario your write-up describes — barely happens in this dataset (0–3 shipments, only under a disruption scenario). Case B is the dominant failure mode (42–50 shipments every scenario).** Your recommender needs both branches, but Case B is where most of the value is, and I'd build it first.

Digging into Case B specifically (Normal scenario): the blocking flag is **`LithiumHandlingAvailable` for 36 of 42 shipments**, `ColdChain` for 18, `MoistureControlAvailable` for 4 (some shipments are blocked by more than one flag across different candidate hub pairs). The single highest-value fix is lithium handling at FE-stage hubs — `FE_LOC_031`, `FE_LOC_051`, `FE_LOC_071`, `FE_LOC_011` each show up as the blocker on 8–9 candidate routes. That's a genuinely strong, concrete headline for the deck: *"adding lithium-handling capability at 4 existing front-end hubs would touch X of the 42 currently-stranded shipments."* Worth computing the exact resolved count once you build this (see §7).

---

## 2. Other reasons/scenarios to recommend adoption (you asked for this explicitly)

Beyond "this shipment is unassigned right now," five more triggers are worth building in, roughly in order of how much they'd strengthen the story:

1. **Pre-emptive resilience, not just reactive rescue.** For a (MaterialFamily, StageFrom, StageTo) lane that's *currently* fine under Normal but has only **one** capability-and-capacity-compatible hub pair, that's a single point of failure — it takes only one disruption scenario (or one hub going into `CapacityReductionPct`) to strand every shipment on that lane. Scan for lanes with candidate-hub-pair count == 1 under Normal and recommend adopting a *second* hub pair before it breaks, using the same PrimaryHubDown/AirCapacityReduced/PortCongestion scenarios you've already built as the stress test. This connects directly to the Monte Carlo resilience work already done (frozen-vs-reoptimized) — same "the plan is fragile, the network is not" narrative, applied one level down at the lane level instead of the whole-network level.
2. **Rescuing `zero_capacity` / `capacity_contention` / `capacity_escalation` shipments, not just `handling_infeasible` ones.** If a shipment IS capability-feasible but is losing the capacity fight because every compatible hub pair for its family is saturated, adding the family to an *additional*, currently under-utilized hub pair increases the real pool of usable capacity for that family — same mechanism as your feature, triggered by a different reason code. Don't hard-restrict the recommender to `handling_infeasible` only.
3. **Score improvement for shipments that already got assigned, not just feasibility for the ones that didn't.** If a shipment's assigned route has a materially worse `MinScore` than what it could get on a nearby, currently-different-family hub pair, recommending adoption there is a WeightedScore-reduction play, not a rescue play — worth a separate "opportunity" list distinct from the "stranded shipment" list, since judges will likely ask "does this only help the unassigned, or does it help the average too?"
4. **~~Concentration / route-diversity risk even when nothing is broken.~~ DEFERRED (18 Jul) — not in v1 scope.** Original idea: if a large share of a material family's volume already funnels through one or two hub pairs, that's an operational risk worth flagging even with zero infeasibility today. Cut for now per Hon Lam's call (§0) — kept here only so the idea isn't lost if there's time to revisit later.
5. **Exploiting genuinely idle hubs and forwarders.** The EDA already found 5 of 40 forwarders (`FWD-036`–`FWD-040`) never appear in any actual shipment, i.e. fully idle. The equivalent question for hubs: cross-reference `Hub_Constraints.CurrentUtilizationPct` (a low value) against whether the hub actually appears as `FromHub`/`ToHub` on any *chosen* route in the current solve — a hub that's technically in the network but essentially unused is your best-case adoption candidate (headroom is closer to guaranteed, not just "currently reads as available this week").

**One more path that isn't facility adoption at all, but should be checked first because it's cheaper:** `Material_Families.SubstitutionGroup` is populated for all 240 materials (real substitutability groups, e.g. `SUB-PSS-55` has 45 members). Before recommending a hub facility change for a stranded material, check whether a substitute material in the same group is already routable on an existing feasible lane — if so, that's a manufacturing/BOM-level fix, not a logistics one, and it's almost certainly faster than requesting a facility modification. Worth surfacing as an alternate recommendation type alongside the hub-adoption one, so planners can pick whichever is actually cheaper.

---

## 3. Does adopting a new material family affect the currently-processible family's numbers at that hub? Yes — three distinct ways

This is the sharpest gap in the current plan, so it's worth being precise about mechanism, not just saying "maybe."

**(a) Hub capacity is a single shared pool — confirmed in code, not assumed.** `Hub_Constraints.WeeklyCapacityUnits` (and the derived `remaining_capacity_units`) has no material-family dimension at all — one number per hub, full stop. In the PuLP model, the hub-capacity constraint (`hc_{hub}_{wk}` in `baseline_v7.solve()`) sums `WeeklyFootprint` across **every** selected route through that hub regardless of family. So the moment you add a new route option at a hub and the optimizer starts using it, it is directly competing with the hub's existing material family(ies) for the same weekly budget in the very next solve. Your "historically available capacity" check is necessary but not sufficient — you also need to re-solve (or at least re-check) the *existing* family's shipments after adding the new candidate route, to make sure you haven't just moved the infeasibility from the stranded shipment onto a previously-fine one. Don't just check "is there headroom today" — check "is there still headroom for everyone after this new demand is added."

**(b) Route-level (`CapacityUnitsPerWeek`) — decided 18 Jul: don't model this as dedicated-per-family, simplify to "prefer underutilized routes."** I'd originally flagged that 967 of 980 multi-family corridors show a *different* `CapacityUnitsPerWeek` per material family on the identical hub pair/mode, and read that as evidence of per-family dedicated allocation. **Hon Lam's call: don't treat that as real dedication — it's more likely dataset noise/generation artifact than an actual contracted-per-family capacity split**, and modeling it as genuinely dedicated adds complexity without a reliable basis. Simplified rule instead: when ranking candidate donor hub pairs, **prefer routes that are currently underutilized**, i.e. have the most free capacity, regardless of *why* that capacity reads as free. Concretely, since `Route_Options` has no utilization column of its own (unlike `Hub_Constraints.CurrentUtilizationPct`), derive it from the solve itself: `free_capacity(RouteOptionID, week) = CapacityUnitsPerWeek - Σ WeeklyFootprint of shipments actually assigned to it in the current solve` (0 if the route wasn't selected at all → full `CapacityUnitsPerWeek` counts as free). Rank donors by this descending. This sidesteps the shared-vs-dedicated question entirely: a route with a large capacity number that's barely used today is a safer bet for absorbing a new material family than a route with a small number or one that's already heavily drawn down, independent of whatever mechanism set that number in the first place.

**(c) Hub handling cost fields could genuinely change for existing flows, not just the new one.** `Hub_Constraints.FixedHandlingCost_EUR` (mean ~€1,456) and `VariableHandlingCostPerUnit_EUR` (mean ~€0.049/unit) exist per hub and are **not currently wired into `BaseCostEUR`** anywhere in the scoring pipeline (confirmed — `baseline_v7`'s cost term is raw `BaseCostEUR`, no reference to either handling-cost field). This is the literal case of "another row's values change" you asked about, and it needs a stated assumption to move forward. **Stated assumption (18 Jul):**

- **If the donor hub already handles a material in the *same* `SubstitutionGroup` as the material being adopted, and already satisfies the same capability flags it needs (ESD / Lithium / Moisture / ColdChain) → assume handling cost is unchanged.**
- **If the donor hub is adopting a material from a *different* `SubstitutionGroup` (even if the capability flags happen to already match) → assume handling cost might increase, and treat it as a real unknown to be checked, not zero.**

*Rationale:* `SubstitutionGroup` isn't just a labeling convenience — it's Infineon's own engineering judgment that two materials are functionally interchangeable (same package/die family, same qualification, same test program in most cases). If a hub already runs material A from group S and picks up material B from the *same* group S, B is by construction "the same kind of thing" A already is — same conveyor, same fixtures, same trained operators, same hazard-control equipment. There's no new physical process to build, so the marginal handling cost is effectively zero beyond the trivial admin of adding a route-option row.

The capability flags (ESD/Lithium/Moisture/ColdChain), by contrast, only capture the *hazard-control* dimension — they say nothing about package geometry, test program, calibration, quality documentation, or operator familiarity. Two materials from *different* substitution groups can share an identical hazard profile (both ESD-sensitive, say) while still being genuinely different products to actually handle: different fixtures, a new test/inspection procedure, possibly new customer-specific documentation (PPAP-style first-article approval), and real ramp-up/training time. So "flags match" is necessary but not sufficient to assume zero cost across a substitution-group boundary — the honest position is "might increase, needs the hub's own engineering team to confirm" rather than assuming it away. This is exactly the kind of check that belongs with the hub, not with a central dataset (see §7).

---

## 4. Additional filtering conditions for candidate hub selection

Combining what's already in your plan with the above:

- **Stage-type match** (`hub.Stage == route.StageFrom` / `StageTo`) — already a clean, reliable join key (verified zero mismatches).
- **Same `DisruptionScenario` as the failure.** Route rows are scenario-specific sets, not just an `AvailableFlag` toggle on one master list (1,419 Normal / 1,200 PrimaryHubDown / 1,200 AirCapacityReduced are separate rows). A shipment stranded under `PrimaryHubDown` must be matched against `PrimaryHubDown` route rows, not Normal ones — this is exactly why Case A only shows up under that scenario. You already called this out; just confirming the data backs it up.
- **Candidate hub must not itself be under an active `CapacityReductionPct` for that same scenario** — don't recommend a hub that's part of the disruption you're trying to route around.
- **Capacity headroom with a buffer, not zero-slack** — use `CurrentUtilizationPct` vs `MaxUtilizationPct` (0.90 cap) with a margin (e.g. don't recommend a hub already above ~75–80% even if technically under the cap), since this is a single point-in-time snapshot, not a real trend (§5).
- **No `IsPrimary` preference (decided 18 Jul — do not filter or rank by this).** `*_ALT_*`/non-primary hubs are fair game on equal footing with primary ones; excluding or down-ranking them would work against the "latent network capacity" story, not for it.
- **Rank by underutilized route capacity** (§3b) rather than raw `CapacityUnitsPerWeek` — free capacity computed against what the current solve actually assigns to that route, not the sticker number.
- **Rank by fan-out impact**: how many currently-stranded (or at-risk, per §2) shipments would this one hub fix actually unblock — a single ask that helps 5 shipments beats 5 asks that each help 1.
- **Rank by adoption cost, cheapest first**: Case B (add one existing-elsewhere capability flag to a hub already shipping this family) before Case A (import an entirely new family to a hub that's never touched it).
- **Geographic sanity check** using `City`/`Country`/`Latitude`/`Longitude`/`GeoCluster` — don't recommend a hub pair that's dramatically further from the shipment's real origin/destination than its currently-attempted routes, or the resulting lead time/cost/risk will make the "feasible" route uncompetitive even after the flag is added.
- **Post-hoc re-solve, not just local feasibility.** After hypothetically adding the candidate route option, re-run the actual optimizer (not just a standalone feasibility check) to confirm (i) the stranded shipment's new option actually beats the unassigned penalty and gets chosen, and (ii) total solved-count / average `MinScore` across all 240 shipments improves or holds — this is what catches the hub-capacity cannibalization risk from §3(a) before you present the recommendation.
- **Cap the number of simultaneous asks.** Rank and present a top-K shortlist — Infineon's operations team can't realistically evaluate and modify dozens of hubs in parallel; this is a real business constraint worth stating even without hard data on it.
- **Check `SubstitutionGroup` first** (§2) as a cheaper alternative before falling back to a facility-change recommendation — **but flag it as a low-confidence recommendation, not an equal-confidence one.** We don't actually know whether `SubstitutionGroup` membership implies *logistics/shipping* interchangeability (same routing rules, same customs/compliance treatment, same customer acceptance) as opposed to just BOM/manufacturing interchangeability. Present substitution-based reroutes as conditional: *"recommended IF Infineon confirms materials in the same SubstitutionGroup are also substitutable for shipping/routing purposes — needs sign-off, not assumed."* This is a question for the judges/instructor as much as it is for the hub, and it should be logged as an open assumption in the submission, not quietly acted on.

---

## 5. Data to request from the company for the complete version

You're right that the dataset can't really support a genuine "historically available" capacity calculation — I checked directly:

- `Hub_Constraints.CurrentUtilizationPct` is a **single flat number per hub** — there is no week/date column on that sheet at all, so there's no way to compute a true historical distribution or percentile from what's provided; it's a single snapshot, not a time series.
- The only real time signal in the whole dataset is `Internal_Shipments.ShipDate`, and it only covers **35 distinct ISO weeks with an average of ~7 shipments per week** — even if you tried to reconstruct a pseudo-time-series of hub load from actual shipment assignments, the sample is too thin per week to say anything statistically meaningful about "usual" availability.

What would actually make this complete, in priority order:

1. **Multi-period actual utilization logs per hub** (ideally 12+ months, weekly), so "historically available capacity" is a real computed percentile (e.g. P50/P90 free capacity) instead of a single current-week snapshot linearly extended.
2. **Whether *route* capacity (`CapacityUnitsPerWeek`) is genuinely shared across material families on the same corridor, or is allocated per family/contract.** *(Corrected 18 Jul — this is about route-level capacity, not hub-level; hub-level sharing is already confirmed in code, no ambiguity there. Per Hon Lam's 18 Jul call in §3(b), we're not trying to resolve this centrally for the hackathon build — we simplify to "prefer underutilized routes" instead — but it's still worth Infineon clarifying for a production version.)*
3–5. **Facility-modification lead time/cost, capacity-additive-vs-reallocating status, and operational bandwidth for concurrent requests** — decided 18 Jul that these shouldn't be requested as bulk data from Infineon at all. A hub's own engineering team knows its equipment and certification realities far better than any central spreadsheet could capture, and the question only needs answering for the small number of hubs actually recommended, not all 488 up front. **Moved to §7** — these become a standardized, per-recommendation outreach ask instead of a data request.
6. **Confirmation on the hub-handling-cost question in §3(c)** — is `FixedHandlingCost_EUR`/`VariableHandlingCostPerUnit_EUR` already embedded in `Route_Options.BaseCostEUR`, or genuinely separate and unapplied? (Current evidence says separate and unapplied, but this should come from Infineon, not be inferred.) The same-substitution-group-implies-same-cost assumption in §3(c) is the practical stand-in until this is confirmed.

For the hackathon submission, the honest framing is: *"we identify and rank candidate hubs using current-snapshot capacity as a stated proxy for 'currently has headroom'; a production version would replace this with a rolling historical percentile once multi-week utilization data is available — the dataset as supplied does not contain enough week-over-week granularity (35 weeks, ~7 shipments/week) to derive one ourselves."* That's a stronger, more credible answer than pretending the snapshot is good enough, and it directly anticipates the "how did you handle historical capacity" QA question already logged in the project's QA prep.

---

## 6. Revised recommendation algorithm (pseudocode)

```
for scenario in [Normal, PrimaryHubDown, AirCapacityReduced, PortCongestion(hub_disruption)]:
    candidates = build_candidates(sheets, scenario)          # existing pipeline
    unassigned = solve(candidates) -> unassigned list + Reason
    at_risk    = lanes with exactly 1 capability+capacity-compatible hub pair (single point of failure, §2.1)
    contended  = shipments with Reason in {zero_capacity, capacity_contention, capacity_escalation} (§2.2)
    weak_score = assigned shipments whose MinScore is an outlier vs peers on comparable lanes (§2.3)

    targets = unassigned[Reason == handling_infeasible] + at_risk + contended + weak_score

    for shipment in targets:
        # 0. cheaper-than-facility-change check
        if a SubstitutionGroup sibling material is already feasible on an existing route: recommend substitution, skip hub search

        # 1. find donor hub pairs
        donors = Route_Options[
            StageFrom == shipment.StageFrom, StageTo == shipment.StageTo,
            DisruptionScenario == scenario, AvailableFlag == Yes,
            MaterialFamily != shipment.MaterialFamily
        ]

        for donor in donors:
            missing_flags = required_flags(shipment.MaterialNo) - donor.hub_pair.supported_flags   # per-material, not per-family
            if missing_flags is empty:
                continue   # this is really a Case-A "just add the row" fix - zero facility change needed, flag as cheapest possible action

            if donor.hub_pair under CapacityReductionPct for this scenario: skip
            if donor.hub_pair current_utilization > buffer_threshold: skip
            if donor.hub_pair geographically unreasonable vs shipment's real lane: skip

            record candidate(donor, missing_flags, case = A|B, est_cost = hub.FixedHandlingCost_EUR delta, ...)

    dedupe candidates by (hub, missing_flag) -> count how many shipments/lanes each would unblock
    rank by: fan_out_count desc, adoption_cost (B before A) asc, geographic fit desc

    for top_K candidates:
        simulate: add hypothetical route option row(s), re-solve the FULL 240-shipment problem
        report: shipments newly solved, net change in solved-count and avg MinScore, any previously-solved
                shipment that got bumped (cannibalization check, §3a)
```

The re-solve step at the end is the part most worth building even under hackathon time pressure — it's what separates "here's a plausible-sounding hub to fix" from "we verified this actually helps and doesn't quietly break something else," which is the kind of rigor the insider interviews said judges want (simple, explainable, but *checked*).

---

## 7. Engineering outreach — cutting the planner's workload

Decided 18 Jul: facility-mod lead time/cost, capacity-additive-vs-reallocating status, and how many concurrent asks a hub can absorb (§5 items 3–5) shouldn't be gathered as bulk data up front — they get asked **per recommendation, directly to the specific hub's engineering team**, since they know their own equipment and certifications far better than a central spreadsheet ever could. To keep this from becoming a writing chore every time the model produces a candidate, use one fixed email skeleton and drop in a short, reason-specific checklist generated automatically from the recommendation's trigger.

### 7.1 Generic template

```
Subject: [Action Requested] Facility capability check — {HubID} ({City}, {Country}) — {MaterialFamily}

Hi {Hub Engineering Contact},

We're reviewing route options for {MaterialFamily} on the {StageFrom} → {StageTo} lane, and {HubID}
came up as a strong candidate. Could your team check the following and get back to us by {target date}?

{REASON-SPECIFIC CHECKLIST — swap in from §7.2}

For context: this affects {N} shipment(s) — {one-line reason, e.g. "currently unassigned, no other
feasible route exists" / "at risk if the primary lane is disrupted" / "running above-target cost/lead
time on their current route"}.

Happy to jump on a call if any of this needs facility detail we don't have visibility into from our side.

Thanks,
{Planner name}
ChainLab / Supply Chain Planning
```

**Worked example (Case B — the highest-value, most common trigger):**

```
Subject: [Action Requested] Can FE_LOC_031 be modified to handle lithium materials? — PSS-32-SenseLink

Hi Wei-Ling,

We're reviewing shipping routes for PSS-32-SenseLink on the front-end-to-OSAT leg, and FE_LOC_031 looks
like a strong option — you already handle this exact material family at your hub today. Could your team
check a few things and get back to us by 25 Jul?

  1. Is your facility able to be modified to handle lithium-containing materials? This wouldn't be a
     brand-new material family for you — you already process PSS-32-SenseLink here — it would just mean
     adding lithium handling as an extra capability.
  2. If you make this change, would it use up any of the capacity you currently have for what you
     already process here?
  3. Roughly, how much would this cost and how long would it take to set up?

For context: this one gap — lithium handling — is currently the single biggest reason shipments are
getting stuck with nowhere to go. Fixing it at FE_LOC_031 would likely help more than just this one
shipment.

Happy to jump on a call if any of this needs facility detail we don't have visibility into from our side.

Thanks,
Hon Lam
ChainLab / Supply Chain Planning
```

### 7.2 Reason → checklist (swap into §7.1's placeholder)

Rewritten in plainer language (18 Jul) — the goal is that a hub engineering contact with no context on our optimizer can read the checklist and immediately understand what's being asked, without needing to know what a "flag" or "handling_infeasible" means.

| Trigger | Recipient | What to ask, in plain terms | Urgency |
|---|---|---|---|
| **Case A** — this material family has never shipped through any hub on this route before (rare) | Hub engineering | (1) Could your facility physically handle and process {MaterialFamily} — including any special handling it needs, such as anti-static protection, moisture control, or lithium battery handling? (2) If yes, roughly how much would it cost and how long would it take to set up? (3) Once set up, would this be genuinely new capacity, or would it come out of the capacity you already use for the materials you handle today? | Low–medium (rare, but leaves a shipment with no alternative at all) |
| **Case B** — hub already handles this material family, just missing one specific capability (most common case, build this first) | Hub engineering | (1) Is your facility able to be modified to [handle lithium-containing materials / meet ESD anti-static handling requirements / handle moisture-sensitive materials / support cold-chain temperature-controlled shipping] — whichever specific capability is actually missing? (2) Would making this change use up any of the capacity you currently have for what you already process here? (3) Roughly, how much would this cost and how long would it take? | High — the smallest, fastest fix available |
| **Pre-emptive resilience** — right now only one hub can handle this material on this route; nothing's broken yet | Hub engineering | If the one hub currently handling this material on this route ever became unavailable, every shipment on this route would be stuck with no backup option. We're not asking you to take on volume today — just: could your facility realistically be set up as a backup for this material, and roughly what would that cost and take? | Low — fine to batch several of these together |
| **Capacity rescue** — shipments could be handled here in principle, but there isn't enough weekly capacity to fit them in | Hub engineering | (1) Is the weekly capacity figure we have on file for your hub still accurate? (2) Is there any way to free up more capacity for this material soon — either by processing more, or shifting some lower-priority work elsewhere? | Medium–high — a recurring bottleneck |
| **Score improvement** — shipments are getting routed a costlier or slower way than ideal, but they're not stuck | Hub engineering | Not urgent, just exploratory: could you take on some volume of this material, and if so, roughly what would the cost and delivery time look like? | Low — exploratory, fine for a quarterly check-in |
| **Substitution-group reroute** | Materials/Quality Engineering (not hub logistics) | We think material {A} might be able to substitute for material {B} — Infineon's own system groups them together — but we're not fully confident this holds for shipping and routing purposes, as opposed to manufacturing. Can you confirm this before we actually reroute using the substitute material? | Must be confirmed before we act on it |

*(The concentration/route-diversity trigger from §2 is out of scope for v1 per §0, so it's dropped from this table for now — reinstate a row here if it gets picked back up later.)*

### 7.3 One more lever: reuse the existing n8n drafting infrastructure

The project already has a working GPT-drafting + approve/reject/edit flow built for customer disruption emails (Era-1 n8n workflows, `n8n_live_patch.js` / `ChainLab_Dashboard_Live.html` — see `ChainLab_Context_Handover.md` §6). The exact same pattern applies here: feed the trigger reason + hub/shipment specifics into the same drafting agent, have it fill the §7.1 template, and the planner's job shrinks to reviewing and hitting send rather than writing anything from scratch. Worth wiring up if there's time — it's a natural extension of infrastructure that already exists rather than a new build.

---

## 8. What's actually built (18 Jul) — script, dashboard card, and the confirm-then-apply loop

**`pulp_optimizer/Baseline/scenario_hub_adoption.py`** — Case B only, run against the real dataset. Confirms the Case B counts (42/50/42) as a hard checkpoint, ranks (HubID, MissingFlag) by fan-out then by free route capacity (§3b), and re-solves the full 240-shipment problem for the top 5 per scenario before recommending anything. Real result: the top Normal recommendation (`FE_LOC_031`, lithium handling) shows up as a candidate for 9 shipments but the re-solve shows only 3 actually get picked up (170→173 solved) — the gap between "could help" and "verified helps" is exactly why the re-solve step exists. Zero cannibalisation across all 15 verified recommendations. Output: `optimizer_hub_adoption_recommendations.xlsx` (Recommendations / ReSolveImpact / DraftEmails / Assumptions / CaseBDetail).

**Dashboard card (`chainlab_dashboard.html`, Actions tab, "🔧 Facility Capability Adoption")** — the interactive front-end for the workflow you described:

1. Scenario picker (Normal / PrimaryHubDown / AirCapacityReduced), each showing its top 3 recommendations with the verified re-solve impact line up front (solved count, avg score, shipments bumped).
2. Each recommendation expands into two tabs: **Email** (the plain-language draft from §7, with a copy button) and **Checklist** — four tick items matching the email 1:1: (1) facility modifiable, (2) confirmed no reduction to existing capacity, (3) cost/lead-time received (with two text fields), (4) modification actually *finished* (not just agreed to).
3. **State lives in an in-page JS object only** — per the artifact policy against `localStorage`/`sessionStorage`, nothing persists across a page reload. Ticking any box re-renders live; reopening a recommendation you'd already started shows your previous answers, editable, exactly the "can be reopened" behaviour you asked for. The tradeoff is explicit in the UI: it survives moving between tabs within the session, not a refresh — export the changelog to keep it.
4. **The exact dataset change only appears once item 4 is ticked** — a "Proposed change to Hub_Constraints" panel shows the real column(s) affected: the capability boolean (e.g. `LithiumHandlingAvailable: No → Yes`) and, where relevant, the human-readable `SupportedHazardClasses` audit column getting the new capability appended (e.g. `"None; ESD Sensitive" → "None; ESD Sensitive; Lithium Handling"`) — both need updating for the row to stay internally consistent, which is a detail a planner wouldn't necessarily think to check.
5. **"Apply to changelog"** logs the confirmed change (hub, column, old/new value, cost, lead time, timestamp) into a session changelog table at the bottom of the card, with a **"↺ Reopen"** button to undo/re-edit before it's exported. **"Export changelog (JSON/CSV)"** downloads the log to the planner's machine — this is the actual hand-off point out of the browser.
6. The card is explicit that this does **not** write to a live database by itself — that's a deliberate scope decision (see below), not an oversight.

**`pulp_optimizer/Baseline/apply_hub_adoption_changes.py`** — the other half of the loop, and the actual answer to "push to the database." Takes the exported changelog (JSON or CSV) and the master dataset, patches only the confirmed `Hub_Constraints` cells in a **copy** of the workbook (`..._HubAdoptionApplied.xlsx`, every other sheet passes through unchanged), and writes an audit-trail CSV of exactly what changed and when. Tested end-to-end: applied a two-row changelog (the `FE_LOC_031` lithium example above) and re-ran `build_candidates`/`apply_capability` against the patched copy — Case B count for Normal dropped from 42 to 37, confirming the loop genuinely closes, not just that the UI looks right. Run the optimizer scripts against the `_HubAdoptionApplied.xlsx` copy (`IFX_WORKBOOK=...`) any time to see the effect ripple through the real scoring.

**Why "push to database" is a two-step script hand-off rather than a live button, stated plainly:** there's no live database backing `Hub_Constraints` in this project (it's a shared Excel dataset), and the dashboard is a static, self-contained HTML artifact with no backend of its own — per the artifact policy it cannot write to `localStorage` and has nothing else to write to persistently. Rather than fake a "success" toast that doesn't actually do anything, the honest version is: the dashboard stages and exports a reviewed, human-confirmed change; a script applies it for real. If you want an actual live "Push" button, the project already has the wiring for it — the Era-1 n8n instance + Supabase `chainlab` schema used for the approve/reject/feedback flow (`ChainLab_Build_Tracker.md`) could take a `POST /chainlab/hub-adoption` webhook the same way `/chainlab/feedback` does, and the dashboard could call it directly instead of (or in addition to) the local export. Not built — flagging it as the natural next step if you want the loop to close without the manual script hand-off, since that's a materially bigger piece of work (new n8n workflow + Supabase table) that shouldn't happen without you asking for it first.

---

*Companion to `ChainLab_Context_Handover.md`. Next in the confirmed build order: Case A, capacity-rescue, pre-emptive resilience, score-improvement (§0).*
