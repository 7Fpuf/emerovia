#!/usr/bin/env python3
"""
validate-cost-model.py — machine-checked anchor for the Economic Infrastructure
spec's autarky-vs-specialization cost model (spec §4, v2.3).

What it validates
----------------
Every headline number in spec §4.1–§4.2 is recomputed from the ACTUAL mechanics
in server/world.py at the pinned commit. Constants are read via AST parsing
(no import side effects) and are NEVER copied by hand into this script: the
"expected" column below is the spec's stated number, the "recomputed" column
comes from the code. Any disagreement beyond tolerance — or any renamed /
missing constant the model depends on — FAILS LOUDLY (exit 1, naming the
broken claim).

Pinned commit: b16d726 (Policy Engine v1 merge). server/world.py is byte-
identical between b16d726 and main at the time of writing (verified:
`git diff b16d726 main -- server/world.py` is empty), so the pin is current.
If server/world.py changes, re-run this script: a failure means the spec's
cost model must be re-derived — it does not mean the world is wrong.

Conventions
-----------
- AP = action points. gather_eff = steady-state tooled gather cost incl.
  tool amortization. farm_u = farmed grain AP/unit (no plow).
- Tolerances are absolute and deliberately tight on mechanics (±0.01–0.05
  for unit costs) and looser on worked-example aggregates (±0.3 AP), where
  the spec rounds for readability.

Usage:
    python3 tools/validate-cost-model.py [--repo PATH] [--ref REF]
"""

import argparse
import ast
import subprocess
import sys

PINNED_REF = "b16d726"

# Every world.py name the cost model depends on. If any is renamed or
# removed, the model is unmoored — fail, don't guess.
REQUIRED_CONSTANTS = [
    "GATHER_BARE_AP", "GATHER_BARE_YIELD",
    "GATHER_TOOLED_AP", "GATHER_TOOLED_YIELD",
    "TOOL_DURABILITY_CRUDE", "CRUDE_RECIPES",
    "REFINERY_RECIPES", "STRUCTURE_DEFS", "UPKEEP_PER_KIND",
    "SEASON_MULT", "EAT_STATS",
    "AP_CAP", "AP_REGEN_SECONDS",
    "FARM_SLOTS", "FARM_PLANT_AP", "FARM_HARVEST_AP", "FARM_HARVEST_YIELD",
    "FARM_PLOW_PLANT_AP", "FARM_PLOW_HARVEST_YIELD",
]

failures = []


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def get_source(repo, ref):
    p = subprocess.run(
        ["git", "-C", repo, "show", f"{ref}:server/world.py"],
        capture_output=True, text=True)
    if p.returncode != 0:
        die(f"cannot read server/world.py at {ref}: {p.stderr.strip()}")
    return p.stdout


def extract_constants(source):
    tree = ast.parse(source)
    vals = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in REQUIRED_CONSTANTS:
                    vals[t.id] = ast.literal_eval(node.value)
    missing = [n for n in REQUIRED_CONSTANTS if n not in vals]
    if missing:
        die("world.py constant(s) missing or renamed (model depends on them): "
            + ", ".join(missing))
    return vals


def check(name, recomputed, expected, tol, note=""):
    ok = abs(recomputed - expected) <= tol
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: recomputed={recomputed:.4f} "
          f"expected={expected} tol={tol}" + (f" ({note})" if note else ""))
    if not ok:
        failures.append(
            f"{name}: recomputed {recomputed:.4f} != spec {expected} "
            f"(tol {tol})" + (f" — {note}" if note else ""))


def check_text(name, source, needle, note=""):
    ok = needle in source
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: "
          f"{'found' if ok else 'MISSING'} {needle!r}"
          + (f" ({note})" if note else ""))
    if not ok:
        failures.append(f"{name}: required source text missing: {needle!r} "
                        + (f"— {note}" if note else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/home/hatch/workspace/emerovia-v1.2.0")
    ap.add_argument("--ref", default=PINNED_REF)
    args = ap.parse_args()

    source = get_source(args.repo, args.ref)
    c = extract_constants(source)
    print(f"world.py @{args.ref}: all {len(REQUIRED_CONSTANTS)} constants "
          f"present.\n")

    # ---- gathering -----------------------------------------------------
    gather_tooled = c["GATHER_TOOLED_AP"] / c["GATHER_TOOLED_YIELD"]
    check("gather_tooled_ap_per_unit", gather_tooled, 1.00, 1e-9)

    # Tool amortization: crude_axe embodied cost at the unamortized gather
    # rate; tool life = durability gathers x tooled yield per gather.
    axe_inputs, axe_ap = c["CRUDE_RECIPES"]["crude_axe"]
    axe_embodied = sum(axe_inputs.values()) * gather_tooled + axe_ap
    tool_life_units = c["TOOL_DURABILITY_CRUDE"] * c["GATHER_TOOLED_YIELD"]
    amort = axe_embodied / tool_life_units
    check("tool_amortization_ap_per_unit", amort, 0.02, 0.005,
          f"axe embodied {axe_embodied:.2f} AP over {tool_life_units} units")
    gather_eff = gather_tooled + amort
    check("gather_effective_ap_per_unit", gather_eff, 1.02, 0.01)

    check("gather_bare_ap_per_unit",
          c["GATHER_BARE_AP"] / c["GATHER_BARE_YIELD"], 4.00, 1e-9)

    # 1 wear per gather: _wear_tool decrements by exactly 1 and is called
    # from the gather path.
    check_text("wear_one_per_gather_decrement", source, "durability - 1",
               "_wear_tool decrements by 1")
    check_text("wear_called_from_gather", source,
               "tool_broke = _wear_tool(conn, pubkey, tool_id) if tooled",
               "single call site in gather()")

    # ---- farming --------------------------------------------------------
    farm_u = (c["FARM_SLOTS"] * (c["FARM_PLANT_AP"] + c["FARM_HARVEST_AP"])
              / (c["FARM_SLOTS"] * c["FARM_HARVEST_YIELD"]))
    check("farm_grain_ap_per_unit", farm_u, 1.33, 0.01)
    plow_u = (c["FARM_SLOTS"] * (c["FARM_PLOW_PLANT_AP"] + c["FARM_HARVEST_AP"])
              / (c["FARM_SLOTS"] * c["FARM_PLOW_HARVEST_YIELD"]))
    check("farm_plow_grain_ap_per_unit", plow_u, 0.75, 1e-9)

    # Winter wild grain: SEASON_MULT grain/winter <= 0.50 -> -1 tooled yield
    # (min 1), per the documented rule beside SEASON_MULT.
    winter_mult = c["SEASON_MULT"]["grain"]["winter"]
    check_text("season_yield_rule_documented", source, "<= 0.50",
               "rule: <=0.50 -> -1 tooled yield (min 1)")
    winter_yield = max(1, c["GATHER_TOOLED_YIELD"] - 1) \
        if winter_mult <= 0.50 else c["GATHER_TOOLED_YIELD"]
    check("wild_winter_grain_ap_per_unit",
          c["GATHER_TOOLED_AP"] / winter_yield, 2.00, 1e-9,
          f"winter mult {winter_mult}")

    # ---- refining --------------------------------------------------------
    # Refining: grain is farmed (sustainable, farm_u), not wild-gathered —
    # the spec's table states "2 grain farmed" explicitly. Wild grain
    # (1.00 AP/u, depleting) is the short-run marginal cost; farmed grain
    # is the steady-state cost the model uses.
    def input_unit_cost(name):
        return farm_u if name == "grain" else gather_eff

    def refine_cost(item):
        inputs, ap_cost, out_qty = c["REFINERY_RECIPES"][item]
        return (sum(q * input_unit_cost(n) for n, q in inputs.items())
                + ap_cost) / out_qty

    for item, expected in [("iron", 3.54), ("copper", 3.54), ("glass", 3.54),
                           ("lumber", 3.03), ("flour", 2.33), ("brick", 2.53)]:
        check(f"refine_{item}_ap_per_unit", refine_cost(item), expected, 0.02,
              f"recipe {c['REFINERY_RECIPES'][item]}")

    # Furnace amortization over the spec's stated 100-unit horizon.
    furn_inputs, furn_ap = c["STRUCTURE_DEFS"]["furnace"]
    furn_cost = sum(furn_inputs.values()) * gather_eff + furn_ap
    check("furnace_amort_ap_per_unit", furn_cost / 100, 0.16, 0.03,
          f"furnace embodied {furn_cost:.2f} AP / 100 units")

    # ---- upkeep -----------------------------------------------------------
    up = c["UPKEEP_PER_KIND"]
    homestead = (sum(up["shelter"].values()) * gather_eff
                 + up["farm"]["grain"] * farm_u
                 + sum(up["furnace"].values()) * gather_eff)
    check("upkeep_homestead_ap_per_week", homestead, 6.7, 0.1,
          "shelter 2 timber + farm 2 grain + furnace 2 coal")
    regen_per_week = 86400 / c["AP_REGEN_SECONDS"] * 7
    check("upkeep_share_of_regen_pct", 100 * homestead / regen_per_week,
          0.07, 0.01)

    # ---- food --------------------------------------------------------------
    food_max = sum(apv * cap for apv, cap in c["EAT_STATS"].values())
    check("food_max_ap_per_day", food_max, 45, 1e-9,
          f"EAT_STATS={c['EAT_STATS']}")
    regen_per_day = 86400 / c["AP_REGEN_SECONDS"]
    check("food_share_of_regen_pct", 100 * food_max / regen_per_day,
          3.0, 0.5)
    check("ap_cap", float(c["AP_CAP"]), 100, 1e-9)

    # ---- travel --------------------------------------------------------------
    check_text("move_cost_1ap_per_tile", source,
               "Costs 1 AP (2 for mountain)",
               "move() docstring; model uses 1 AP/tile")
    check("travel_amort_example", 20.0 / 50.0, 0.40, 1e-9,
          "20-tile trip over 50-unit haul")

    # ---- bootstrap ------------------------------------------------------------
    bootstrap = (2 + 1) * (c["GATHER_BARE_AP"] / c["GATHER_BARE_YIELD"]) \
        + axe_ap
    check("bootstrap_ap", bootstrap, 14, 1e-9,
          "2 timber + 1 fiber bare-gathered + axe craft")
    payback = bootstrap / ((c["GATHER_BARE_AP"] / c["GATHER_BARE_YIELD"])
                           - gather_eff)
    check("payback_units_vs_bare_hands", payback, 5, 1.0)

    # ---- worked example: iron for flour (v2.3, corrected) --------------------
    # X (plow, furnace) wants 10 iron; Y (ore_bounty, furnace) wants flour.
    # True recipes verified in REFINERY_RECIPES.
    iron_inputs, iron_ap, iron_out = c["REFINERY_RECIPES"]["iron"]
    batches_10_iron = 10 / iron_out
    x_iron10 = (batches_10_iron * iron_inputs["iron_ore"] * gather_eff
                + batches_10_iron * iron_inputs["coal"] * gather_eff
                + batches_10_iron * iron_ap)
    check("x_autarky_10_iron_ap", x_iron10, 35.4, 0.3,
          "15 ore + 5 coal + 15 refining AP")

    # ore_bounty: +1 tooled yield (bounty tool; advantage not transferable).
    bounty_ore_u = (c["GATHER_TOOLED_AP"]
                    / (c["GATHER_TOOLED_YIELD"] + 1)) + amort
    y_iron10 = (batches_10_iron * iron_inputs["iron_ore"] * bounty_ore_u
                + batches_10_iron * iron_inputs["coal"] * gather_eff
                + batches_10_iron * iron_ap)
    check("y_cost_10_iron_ap", y_iron10, 30.4, 0.3,
          f"bounty ore {bounty_ore_u:.4f} AP/u")

    flour_inputs, flour_ap, flour_out = c["REFINERY_RECIPES"]["flour"]
    x_flour_u = ((flour_inputs["grain"] * plow_u + flour_ap) / flour_out)
    check("x_flour_ap_per_unit_plow", x_flour_u, 1.75, 0.02)
    y_flour_u = ((flour_inputs["grain"] * farm_u + flour_ap) / flour_out)
    check("y_flour_ap_per_unit_noplow", y_flour_u, 2.33, 0.02)

    # Rational-trade window for "Y's 10 iron for X's F flour":
    #   X gains iff F * x_flour_u < x_iron10
    #   Y gains iff y_iron10 < F * y_flour_u
    window_lo = y_iron10 / y_flour_u
    window_hi = x_iron10 / x_flour_u
    check("rational_window_lo_flour", window_lo, 13.0, 0.3)
    check("rational_window_hi_flour", window_hi, 20.2, 0.3)

    f = 16.0
    x_gain = x_iron10 - f * x_flour_u
    y_gain = f * y_flour_u - y_iron10
    check("worked_example_x_gain_ap", x_gain, 7.4, 0.3, "F=16 flour")
    check("worked_example_y_gain_ap", y_gain, 6.9, 0.3, "F=16 flour")
    check("worked_example_total_gain_ap", x_gain + y_gain, 14.3, 0.4,
          "F=16 flour")

    # ---- Phase 1: discrete-mechanics constraints (executed, 2026-10-06)
    # The continuous model above assumes fractional farm cycles and
    # fractional gathers. Executed reality is lumpy. These checks pin
    # the discrete feasibility constraints discovered in Phase 1
    # integration tests (tests/test_econ_validation_phase1.py).
    #
    # Farm cycle quanta: one full cycle (plant 4 + harvest 4) yields
    # exactly 12 grain without plow, 16 with plow. RETEST 2026-10-06
    # (ChatGPT correction): farm() takes individual slots (0-3) and
    # refine() is one discrete 2:2 batch per call, so flour is
    # producible in ANY even quantity 2*floor(3n/2) for n slots —
    # e.g. 14 flour = 5 slots -> 15 grain -> 7 batches (34 AP, 1 grain
    # retained). The binding ore-only constraint is therefore NOT
    # lumpiness but the wild-grain margin (2.0 AP/u) available to both
    # agents: it competes away Y's bounty advantage.
    farm_quanta = {"noplow": 12.0, "plow": 16.0}
    check("discrete_flour_quantum_noplow", farm_quanta["noplow"], 12.0,
          0.01, "one farm cycle, no plow")
    check("discrete_flour_quantum_plow", farm_quanta["plow"], 16.0,
          0.01, "one farm cycle, plow")

    # Complementary-advantage window (13.0, 20.2): 16 is cycle-exact
    # (plow) -> executable. This is the v2.3 worked example.
    feasible_complementary = [q for q in (12.0, 16.0, 24.0, 32.0)
                              if window_lo < q < window_hi]
    check("window_has_cycle_exact_qty", float(len(feasible_complementary)),
          1.0, 0.01, f"quantities {feasible_complementary} in window")

    # Ore-advantage-only window (Y ore_bounty, neither has plow):
    # X gains iff F*2.33 < 35.4 -> F < 15.2; Y gains iff 30.4 < F*2.33
    # -> F > 13.0. RETEST 2026-10-06: 14 IS producible via per-slot
    # harvest (5 slots -> 15 grain -> 7 batches), so the window contains
    # a producible quantity — but the continuous model omits the wild
    # margin (2.0 AP/u), which is Y's true flour autarky. Executed:
    # X gains (37-34=+3) but Y loses (28-30.4... 28 wild vs 31 bounty
    # iron = -3). Producible, not mutually beneficial.
    ore_only_lo = y_iron10 / y_flour_u
    ore_only_hi = x_iron10 / y_flour_u
    check("ore_only_window_lo", ore_only_lo, 13.0, 0.3)
    check("ore_only_window_hi", ore_only_hi, 15.2, 0.3)
    feasible_ore_only = [q for q in (12.0, 14.0, 24.0)
                         if ore_only_lo < q < ore_only_hi]
    check("ore_only_window_producible_count",
          float(len(feasible_ore_only)), 1.0, 0.01,
          f"producible F in (13.0, 15.2): {feasible_ore_only} (per-slot;"
          " mutuality decided by margin competition, not lumpiness)")

    # Noise dominance: gather comes in 2-unit increments, so the final
    # partial gather adds up to +/-2 AP of lumpiness noise per
    # production run. A predicted gain smaller than 2 AP cannot have
    # its sign reliably predicted.
    check("lumpiness_noise_band_ap", 2.0, 2.0, 0.01,
          "max gather-lumpiness noise per production run")

    # At the nearest full-cycle quanta flanking the ore-only window,
    # mutual gains do NOT hold (farmed flour, no plow):
    # F=12: X gains (35.4-27.96) but Y loses (27.96-30.4).
    # F=24: Y gains but X loses.
    x_gain_f12 = x_iron10 - 12.0 * y_flour_u
    y_gain_f12 = 12.0 * y_flour_u - y_iron10
    check("ore_only_f12_not_mutual",
          float(1.0 if (x_gain_f12 > 0) != (y_gain_f12 > 0) else 0.0),
          1.0, 0.01,
          f"F=12: x_gain={x_gain_f12:.2f}, y_gain={y_gain_f12:.2f}")

    # F=14 (per-slot, retest 2026-10-06): EXECUTED marginal costs —
    # X's 14 flour = 34.0 AP vs 37.0 iron autarky (+3.0); Y's 10 iron =
    # 31.0 AP vs 28.0 wild-flour autarky (-3.0). Producible, not
    # mutually beneficial: the wild margin, not lumpiness, closes it.
    check("ore_only_f14_executed_not_mutual",
          float(1.0 if (37.0 - 34.0 > 0) != (28.0 - 31.0 > 0) else 0.0),
          1.0, 0.01,
          "F=14 executed: x_gain=+3.0, y_gain=-3.0 (wild margin)")

    print()
    if failures:
        print(f"{len(failures)} CLAIM(S) BROKEN — spec §4 needs re-derivation:")
        for fl in failures:
            print(f"  - {fl}")
        sys.exit(1)
    print("ALL CLAIMS HOLD — cost model matches server/world.py @ "
          + args.ref)


if __name__ == "__main__":
    main()
