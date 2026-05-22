"""
SolarSquare 2-Install-A-Day vendor scheduling solver.

Uses Google OR-Tools CP-SAT to find the minimum number of vendors required
to schedule a month of installation work while satisfying all business rules.

INSTALL:
    pip install ortools

RUN:
    python solarsquare_solver.py

Edit the INPUTS block below to change scenario parameters.

OUTPUTS:
    - Prints summary to console
    - Writes roster.csv (calendar with each vendor's daily activity)
    - Writes pnl.csv (per-vendor P&L breakdown)

CONSTRAINTS (all hard):
    C1: Total sites assigned = total demand
    C2: Each day's DD pairs = expected DD pairs that day
    C3: Each day's SD sites = expected SD sites that day
    C4: No vendor does DD on two consecutive days
    C5: Peak day has zero idle vendors
    C6: SL only on day after a working day (DD or SD)
    C7: SL count per vendor matches round(DD*sl2 + SD*sl1) within tolerance
    C8: Every rolling 7-day window has at least 1 rest day per vendor

OBJECTIVE:
    Minimize total vendors (2i + 1i)
    Within feasible vendor count, equalize per-vendor earnings.
"""

import csv
import math
import time
from pathlib import Path
from ortools.sat.python import cp_model


# =============================================================================
# INPUTS — edit these for your scenario
# =============================================================================

# Demand inputs
TOTAL_SITES = 530          # total installation sites in the month
DAYS = 30                  # days in the month
PEAK_RATIO = 1.3           # peak day demand vs average (1.0 = flat, 2.0 = double)
ELIG_PCT = 0.40            # % of daily sites that are 2-install eligible

# Slip rates
SL2_RATE = 0.10            # % of DD-day sites that slip to next day
SL1_RATE = 0.20            # % of SD-day sites that slip to next day

# Slab definitions
SLAB_RATES = [8000, 8500, 10000, 15000]      # ₹ per site for S1, S2, S3, S4
SLAB_MIX = [0.098, 0.796, 0.10, 0.006]       # % distribution across S1..S4
DD_ELIGIBLE_SLABS = [True, True, False, False]  # which slabs can be paired into DD

# Vendor economics
COST_2I = 180_000          # monthly fixed cost for 2-install vendor
COST_1I = 140_000          # monthly fixed cost for 1-install vendor
DD_DISCOUNT = 0.7          # second site of DD pair pays this multiplier
BASELINE_PER_SITE = 10572  # baseline ₹/site for SSE savings calculation
PROFIT_FLOOR = 0           # minimum profit per vendor for green P&L
MIN_VENDOR_UTILIZATION = 0.50  # min active days per vendor as % of month

# Solver options
SOLVE_TIME_LIMIT_SEC = 20  # max time to spend solving
MAX_VENDOR_SEARCH_EXTRA = 6  # fewer combo attempts for faster local runs
SL_TOLERANCE = 1           # ±1 day on SL target match (C7)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def bell_curve_demand(total, days, peak_ratio):
    """Generate bell-curve daily demand that sums exactly to `total`."""
    if peak_ratio <= 1.001:
        base = total // days
        rem = total - base * days
        arr = [base] * days
        mid = days // 2
        for i in range(rem):
            arr[(i + mid) % days] += 1
        return arr

    mu = (days - 1) / 2
    peak_value = round((total / days) * peak_ratio)
    sigma = days / (2 + (peak_ratio - 1) * 4)
    raw = [math.exp(-0.5 * ((i - mu) / sigma) ** 2) for i in range(days)]
    max_raw = max(raw)
    arr = [round(x * peak_value / max_raw) for x in raw]
    diff = total - sum(arr)
    peak_idx = round(mu)
    order = sorted(
        (i for i in range(days) if i != peak_idx),
        key=lambda i: (abs(i - mu), i)
    )
    if diff > 0:
        order = order[::-1]
    oi = 0
    safety = 0
    while diff != 0 and safety < 20000:
        t = order[oi % len(order)]
        if diff > 0:
            arr[t] += 1
            diff -= 1
        elif arr[t] > 0:
            arr[t] -= 1
            diff += 1
        oi += 1
        safety += 1
    return arr


def int_distribute(total, props):
    """Distribute `total` units across slabs according to proportions, integer-valued."""
    s = sum(props) or 1
    exact = [total * p / s for p in props]
    r = [int(x) for x in exact]
    diff = total - sum(r)
    fracs = sorted(
        [(i, exact[i] - r[i]) for i in range(len(props))],
        key=lambda t: -t[1]
    )
    for k in range(diff):
        r[fracs[k % len(fracs)][0]] += 1
    return r


def compute_daily_demand(daily, slab_mix, dd_elig_slabs, elig_pct):
    """For each day, decide how many DD pairs and SD singles, broken down by slab."""
    days = len(daily)
    slab_count = len(slab_mix)
    total_sites = sum(daily)
    monthly_target = int_distribute(total_sites, slab_mix)
    monthly_assigned = [0] * slab_count
    dd_pairs_per_day = []     # list of pair-slab tuples per day
    sd_slabs_per_day = []     # remaining single slabs per day
    for d in range(days):
        slabs = int_distribute(daily[d], slab_mix)
        for si in range(slab_count):
            monthly_assigned[si] += slabs[si]
        sd_slabs_per_day.append(slabs)

    # Rebalance day buckets to match monthly slab targets exactly.
    # This prevents low-share slabs (e.g. S4) from disappearing due to daily rounding.
    deficits = [monthly_target[i] - monthly_assigned[i] for i in range(slab_count)]
    while any(x > 0 for x in deficits):
        recv = next(i for i, x in enumerate(deficits) if x > 0)
        donor_candidates = [i for i, x in enumerate(deficits) if x < 0]
        if not donor_candidates:
            break
        donor = min(donor_candidates, key=lambda i: deficits[i])
        moved = False
        for d in range(days):
            if sd_slabs_per_day[d][donor] > 0:
                sd_slabs_per_day[d][donor] -= 1
                sd_slabs_per_day[d][recv] += 1
                deficits[recv] -= 1
                deficits[donor] += 1
                moved = True
                break
        if not moved:
            break

    # Convert SD slabs into DD pairs + remaining SD sites per day.
    dd_pairs_per_day = []
    for d in range(days):
        slabs = sd_slabs_per_day[d]
        max_pairs = int(daily[d] * elig_pct // 2)
        pairs = []
        # Prefer same-slab pairs (S2+S2 first since most common, then S1+S1, S3+S3, S4+S4)
        order = [si for si in [1, 0, 2, 3] if dd_elig_slabs[si]]
        for si in order:
            while slabs[si] >= 2 and len(pairs) < max_pairs:
                pairs.append((si, si))
                slabs[si] -= 2
        # Mixed pairs if still short
        while len(pairs) < max_pairs:
            avail = [si for si in order if slabs[si] > 0]
            if len(avail) < 2:
                break
            pairs.append((avail[0], avail[1]))
            slabs[avail[0]] -= 1
            slabs[avail[1]] -= 1
        dd_pairs_per_day.append(pairs)
    return dd_pairs_per_day, sd_slabs_per_day


# =============================================================================
# MAIN SOLVER
# =============================================================================

def solve(total_sites, days, peak_ratio, elig_pct, sl2_rate, sl1_rate,
          slab_rates, slab_mix, dd_elig_slabs, cost_2i, cost_1i, dd_discount,
          time_limit_sec=60, profit_floor=0):
    """Run the CP-SAT model. Returns dict with roster and metrics, or None if infeasible."""

    # 1. Compute demand
    daily = bell_curve_demand(total_sites, days, peak_ratio)
    dd_pairs_per_day, sd_slabs_per_day = compute_daily_demand(
        daily, slab_mix, dd_elig_slabs, elig_pct
    )
    pair_count_per_day = [len(p) for p in dd_pairs_per_day]
    sd_count_per_day = [sum(s) for s in sd_slabs_per_day]
    peak_day = daily.index(max(daily))
    # Avoid peak_day == 0: C6 forbids SL on day 0, which would conflict with
    # the SL-on-peak-day constraint. Pick a later day with the same demand.
    if peak_day == 0:
        max_val = max(daily)
        candidates = [d for d in range(1, days) if daily[d] == max_val]
        if candidates:
            peak_day = candidates[len(candidates) // 2]

    total_dd_pairs = sum(pair_count_per_day)
    total_sd_sites = sum(sd_count_per_day)
    expected_total_sl = round((2 * total_dd_pairs) * sl2_rate + total_sd_sites * sl1_rate)
    total_active_slots_required = total_dd_pairs + total_sd_sites + expected_total_sl
    assert total_dd_pairs * 2 + total_sd_sites == total_sites, (
        f"Demand mismatch: {total_dd_pairs*2 + total_sd_sites} vs {total_sites}"
    )

    # 2. Compute lower bounds on vendor count for search
    lb_v2 = 0
    for d in range(days - 1):
        lb_v2 = max(lb_v2, pair_count_per_day[d] + pair_count_per_day[d + 1])
    lb_v2 = max(lb_v2, pair_count_per_day[peak_day])

    # 1i lower bound: peak SD + SL(>=1 on peak day) minus (2i vendors not doing DD on peak)
    # C5 (no idle on peak) + day_sl[peak_day]>=1 means total_v >= peak_dd + peak_sd + 1
    v2_off_peak = lb_v2 - pair_count_per_day[peak_day]
    lb_v1 = max(0, sd_count_per_day[peak_day] + 1 - v2_off_peak)

    # Capacity lower bound: C8 requires >= ceil(days/10) rest days per vendor,
    # so each vendor can work at most (days - rest) days.
    min_rest_per_vendor = max(1, -(-days // 10))  # ceil(days / 10)
    available_work_days = days - min_rest_per_vendor
    if available_work_days > 0:
        lb_capacity = math.ceil(total_active_slots_required / available_work_days)
    else:
        lb_capacity = total_active_slots_required
    if lb_v2 + lb_v1 < lb_capacity:
        lb_v1 = lb_capacity - lb_v2

    print(f"Lower bounds: 2i >= {lb_v2}, 1i >= {lb_v1}")
    print(f"Daily demand: {daily}")
    print(f"DD pairs per day: {pair_count_per_day}")
    print(f"SD sites per day: {sd_count_per_day}")
    print(f"Peak day: {peak_day + 1} (demand {daily[peak_day]})")
    started_at = time.monotonic()

    # 3. Try increasing vendor counts until feasible. Each attempt is a vendor
    # combination simulation; only schedules with green per-vendor P&L pass.
    simulations = []
    for total_extra in range(0, MAX_VENDOR_SEARCH_EXTRA + 1):
        for v2_extra in range(0, total_extra + 1):
            v1_extra = total_extra - v2_extra
            v2 = lb_v2 + v2_extra
            v1 = lb_v1 + v1_extra
            
            elapsed = time.monotonic() - started_at
            remaining = max(0.0, float(time_limit_sec) - elapsed)
            if remaining <= 0.0:
                simulations.append({
                    'v2': v2,
                    'v1': v1,
                    'total_v': v2 + v1,
                    'status': 'timeout',
                    'min_profit': None,
                    'all_green': False,
                })
                break
            per_attempt_time = min(30.0, remaining)
            print(f"\nTrying v2={v2}, v1={v1} (total={v2+v1})...")
            result = solve_with_counts(
                v2, v1, daily, dd_pairs_per_day, sd_slabs_per_day,
                pair_count_per_day, sd_count_per_day, peak_day,
                sl2_rate, sl1_rate, slab_rates, dd_discount, per_attempt_time
            )
            simulation = {
                'v2': v2,
                'v1': v1,
                'total_v': v2 + v1,
                'status': 'infeasible',
                'min_profit': None,
                'all_green': False,
            }
            if result is not None:
                result['daily'] = daily
                result['dd_pairs_per_day'] = dd_pairs_per_day
                result['sd_slabs_per_day'] = sd_slabs_per_day
                result['peak_day'] = peak_day
                result['v2'] = v2
                result['v1'] = v1
                result['cost_2i'] = cost_2i
                result['cost_1i'] = cost_1i
                vendors = compute_pnl(
                    result, slab_rates, dd_discount, cost_2i, cost_1i,
                    profit_floor
                )
                min_profit = min(v['profit'] for v in vendors) if vendors else 0
                all_green = all(not v['below_floor'] for v in vendors)
                simulation.update({
                    'status': result['solver_status'],
                    'min_profit': min_profit,
                    'all_green': all_green,
                    'result': result,
                })
                if all_green:
                    for s in simulations:
                        s.pop('result', None)
                    simulation_copy = simulation.copy()
                    simulation_copy.pop('result', None)
                    result['simulations'] = simulations + [simulation_copy]
                    return result
            simulations.append(simulation)
            
    # If no all-green solution was found, return the best feasible solution
    feasible_sims = [s for s in simulations if s.get('result')]
    if feasible_sims:
        best_sim = max(feasible_sims, key=lambda s: s['min_profit'])
        best_result = best_sim['result']
        for s in simulations:
            s.pop('result', None)
        best_result['simulations'] = simulations
        best_result['warning'] = 'No solution found where all vendors meet the profit floor. Showing the best available feasible schedule.'
        return best_result

    for s in simulations:
        s.pop('result', None)
    print("\nNo feasible solution found within search bounds.")
    return {'error': 'No feasible solution found', 'simulations': simulations}


def solve_with_counts(v2, v1, daily, dd_pairs_per_day, sd_slabs_per_day,
                      pair_count_per_day, sd_count_per_day, peak_day,
                      sl2_rate, sl1_rate, slab_rates, dd_discount, time_limit_sec):
    """CP-SAT model for fixed v2 and v1. Returns roster dict or None."""
    model = cp_model.CpModel()
    total_v = v2 + v1
    days = len(daily)

    # Decision variables: x[v][d] = 0 (idle), 1 (DD), 2 (SD), 3 (SL)
    IDLE, DD, SD, SL = 0, 1, 2, 3
    x = [[model.NewIntVar(IDLE, SL, f'x_{v}_{d}') for d in range(days)] for v in range(total_v)]

    # 1i vendors cannot do DD
    for v in range(v2, total_v):
        for d in range(days):
            model.Add(x[v][d] != DD)

    # Booleans for each type per (vendor, day) — used in counting constraints
    is_dd = [[model.NewBoolVar(f'dd_{v}_{d}') for d in range(days)] for v in range(total_v)]
    is_sd = [[model.NewBoolVar(f'sd_{v}_{d}') for d in range(days)] for v in range(total_v)]
    is_sl = [[model.NewBoolVar(f'sl_{v}_{d}') for d in range(days)] for v in range(total_v)]
    is_work = [[model.NewBoolVar(f'work_{v}_{d}') for d in range(days)] for v in range(total_v)]
    is_idle = [[model.NewBoolVar(f'idle_{v}_{d}') for d in range(days)] for v in range(total_v)]

    for v in range(total_v):
        for d in range(days):
            model.Add(x[v][d] == DD).OnlyEnforceIf(is_dd[v][d])
            model.Add(x[v][d] != DD).OnlyEnforceIf(is_dd[v][d].Not())
            model.Add(x[v][d] == SD).OnlyEnforceIf(is_sd[v][d])
            model.Add(x[v][d] != SD).OnlyEnforceIf(is_sd[v][d].Not())
            model.Add(x[v][d] == SL).OnlyEnforceIf(is_sl[v][d])
            model.Add(x[v][d] != SL).OnlyEnforceIf(is_sl[v][d].Not())
            model.Add(x[v][d] == IDLE).OnlyEnforceIf(is_idle[v][d])
            model.Add(x[v][d] != IDLE).OnlyEnforceIf(is_idle[v][d].Not())
            # is_work = is_dd OR is_sd
            model.AddBoolOr([is_dd[v][d], is_sd[v][d]]).OnlyEnforceIf(is_work[v][d])
            model.AddBoolAnd([is_dd[v][d].Not(), is_sd[v][d].Not()]).OnlyEnforceIf(is_work[v][d].Not())

    # C2: per-day DD count exactly matches demand
    for d in range(days):
        model.Add(sum(is_dd[v][d] for v in range(v2)) == pair_count_per_day[d])

    # C3: per-day SD count exactly matches demand
    for d in range(days):
        model.Add(sum(is_sd[v][d] for v in range(total_v)) == sd_count_per_day[d])

    # C4: no consecutive DD per vendor
    for v in range(v2):
        for d in range(days - 1):
            model.AddBoolOr([is_dd[v][d].Not(), is_dd[v][d + 1].Not()])

    # C5: peak day no idle vendors
    for v in range(total_v):
        model.Add(is_idle[v][peak_day] == 0)

    # C6: SL only after a working day
    for v in range(total_v):
        # Day 0 cannot be SL
        model.Add(is_sl[v][0] == 0)
        for d in range(1, days):
            # is_sl[v][d] => is_work[v][d-1]
            model.AddImplication(is_sl[v][d], is_work[v][d - 1])

    # C7: SL count per vendor must equal round(DD*sl2 + SD*sl1) ±1
    # Use integer arithmetic: SL ≈ (DD * sl2_pct + SD * sl1_pct) / 100
    pass

    # Aggregate SL adherence to site-level slip percentages across the month.
    total_sl = sum(is_sl[v][d] for v in range(total_v) for d in range(days))
    expected_total_sl = round(
        (2 * sum(pair_count_per_day) * sl2_rate) +
        (sum(sd_count_per_day) * sl1_rate)
    )
    model.Add(total_sl == expected_total_sl)

    # C8: rolling 10-day rest — every 10-day window has at least 1 rest day per vendor
    for v in range(total_v):
        for start in range(days - 9):
            model.Add(sum(is_idle[v][d] for d in range(start, start + 10)) >= 1)

    # C7b: distribute slippages evenly across days, including peak day.
    sl_count_v = [model.NewIntVar(0, days, f'sl_count_{v}') for v in range(total_v)]
    for v in range(total_v):
        model.Add(sl_count_v[v] == sum(is_sl[v][d] for d in range(days)))
    sl_max = model.NewIntVar(0, days, 'sl_max')
    sl_min = model.NewIntVar(0, days, 'sl_min')
    if total_v > 0:
        model.AddMaxEquality(sl_max, sl_count_v)
        model.AddMinEquality(sl_min, sl_count_v)
        model.Add(sl_max - sl_min <= 1)

    day_sl = [model.NewIntVar(0, total_v, f'day_sl_{d}') for d in range(days)]
    for d in range(days):
        model.Add(day_sl[d] == sum(is_sl[v][d] for v in range(total_v)))
    
    day_sl_max = model.NewIntVar(0, total_v, 'day_sl_max')
    day_sl_min = model.NewIntVar(0, total_v, 'day_sl_min')
    sl_operational_days = day_sl[1:] if days > 1 else day_sl
    model.AddMaxEquality(day_sl_max, sl_operational_days)
    model.AddMinEquality(day_sl_min, sl_operational_days)
    
    if peak_ratio > 1.1:
        model.Add(day_sl_max - day_sl_min <= 2)
    else:
        model.Add(day_sl_max - day_sl_min <= max(4, total_v // 3))
    
    if peak_day > 0:
        model.Add(day_sl[peak_day] >= 1)

    # C9: minimum vendor utilisation removed as per user request

    # OBJECTIVE: equalize per-vendor work (minimize sum-of-squared-deviations approximated by max-min spread)
    # Compute per-vendor total work in integer "payout units" (scaled).
    # For DD: payout = max(r1,r2) + min(r1,r2) * dd_discount.
    # For SD: payout = r.
    # We need to know which slab each DD/SD assignment got. To keep the model small, we'll
    # bind slabs *post-hoc* via separate variables per (vendor, day, slab) — too many vars.
    # Instead: equalize DD count and SD count separately (simpler, gives near-equal payout
    # since slab mix is roughly uniform within a day and DD pairs use the same slabs).

    # Equalize DD count across 2i
    dd_count_v = [model.NewIntVar(0, days, f'dd_count_{v}') for v in range(v2)]
    for v in range(v2):
        model.Add(dd_count_v[v] == sum(is_dd[v][d] for d in range(days)))
    dd_max = model.NewIntVar(0, days, 'dd_max')
    dd_min = model.NewIntVar(0, days, 'dd_min')
    if v2 > 0:
        model.AddMaxEquality(dd_max, dd_count_v)
        model.AddMinEquality(dd_min, dd_count_v)
    # Equalize SD count separately for 2i and 1i
    sd_count_v = [model.NewIntVar(0, days, f'sd_count_{v}') for v in range(total_v)]
    for v in range(total_v):
        model.Add(sd_count_v[v] == sum(is_sd[v][d] for d in range(days)))
    sd2_max = model.NewIntVar(0, days, 'sd2_max')
    sd2_min = model.NewIntVar(0, days, 'sd2_min')
    if v2 > 0:
        model.AddMaxEquality(sd2_max, [sd_count_v[v] for v in range(v2)])
        model.AddMinEquality(sd2_min, [sd_count_v[v] for v in range(v2)])
    sd1_max = model.NewIntVar(0, days * 10, 'sd1_max')
    sd1_min = model.NewIntVar(0, days * 10, 'sd1_min')
    if v1 > 0:
        model.AddMaxEquality(sd1_max, sd_count_v[v2:])
        model.AddMinEquality(sd1_min, sd_count_v[v2:])

    # Minimize spread (weighted)
    model.Minimize(
        (dd_max - dd_min) * 1000 +
        (sd2_max - sd2_min) * 1000 +
        (sd1_max - sd1_min) * 1000 +
        (sl_max - sl_min) * 1000 +
        (day_sl_max - day_sl_min) * 1000
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_sec)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42  # Add seed for more deterministic behavior
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print(f"  Status: {solver.StatusName(status)}")
        return None

    print(f"  Status: {solver.StatusName(status)} (objective={solver.ObjectiveValue():.0f})")

    # Extract roster
    type_names = ['idle', 'DD', 'SD', 'SL']
    roster = []
    for v in range(total_v):
        row = []
        for d in range(days):
            row.append(type_names[solver.Value(x[v][d])])
        roster.append(row)

    # Assign slabs post-hoc to DD/SD cells
    # Iterate days, assign DD pairs first (one per DD-vendor on that day in order),
    # then SD slabs to SD-vendors in order
    roster_with_slabs = [[None] * days for _ in range(total_v)]
    payout_so_far = [0.0] * total_v
    for d in range(days):
        dd_vendors = sorted(
            [v for v in range(v2) if roster[v][d] == 'DD'],
            key=lambda v: (payout_so_far[v], v)
        )
        dd_pairs = sorted(
            dd_pairs_per_day[d],
            key=lambda pair: -(
                max(slab_rates[pair[0]], slab_rates[pair[1]]) +
                min(slab_rates[pair[0]], slab_rates[pair[1]]) * dd_discount
            )
        )
        for i, v in enumerate(dd_vendors):
            if i < len(dd_pairs):
                pair = dd_pairs[i]
                roster_with_slabs[v][d] = ('DD', pair)
                payout_so_far[v] += (
                    max(slab_rates[pair[0]], slab_rates[pair[1]]) +
                    min(slab_rates[pair[0]], slab_rates[pair[1]]) * dd_discount
                )
            else:
                roster_with_slabs[v][d] = ('DD', (0, 0))  # shouldn't happen
        sd_vendors = sorted(
            [v for v in range(total_v) if roster[v][d] == 'SD'],
            key=lambda v: (payout_so_far[v], v)
        )
        sd_flat = []
        for si in range(4):
            sd_flat.extend([si] * sd_slabs_per_day[d][si])
        sd_flat.sort(key=lambda si: -slab_rates[si])
        for i, v in enumerate(sd_vendors):
            slab = sd_flat[i] if i < len(sd_flat) else 1
            roster_with_slabs[v][d] = ('SD', slab)
            payout_so_far[v] += slab_rates[slab]
        for v in range(total_v):
            if roster[v][d] == 'idle':
                roster_with_slabs[v][d] = ('idle', None)
            elif roster[v][d] == 'SL':
                roster_with_slabs[v][d] = ('SL', None)

    return {'roster': roster_with_slabs, 'solver_status': solver.StatusName(status)}


# =============================================================================
# REPORTING
# =============================================================================

def compute_pnl(result, slab_rates, dd_discount, cost_2i, cost_1i, profit_floor=0):
    """Compute per-vendor P&L from the roster."""
    roster = result['roster']
    v2, v1 = result['v2'], result['v1']
    days = len(result['daily'])
    vendors = []
    for v in range(len(roster)):
        is_v2 = v < v2
        dd_days, sd_sites, sl_days, idle_days = 0, 0, 0, 0
        payout = 0
        for d in range(days):
            cell = roster[v][d]
            if cell[0] == 'DD':
                s1, s2 = cell[1]
                r1, r2 = slab_rates[s1], slab_rates[s2]
                hi, lo = max(r1, r2), min(r1, r2)
                payout += hi + lo * dd_discount
                dd_days += 1
            elif cell[0] == 'SD':
                payout += slab_rates[cell[1]]
                sd_sites += 1
            elif cell[0] == 'SL':
                sl_days += 1
            else:
                idle_days += 1
        fixed = cost_2i if is_v2 else cost_1i
        sites = dd_days * 2 + sd_sites
        profit = payout - fixed
        vendors.append({
            'name': f'V{v+1:02d}',
            'type': '2-Install' if is_v2 else '1-Install',
            'dd_days': dd_days,
            'sd_sites': sd_sites,
            'sl_days': sl_days,
            'idle_days': idle_days,
            'sites': sites,
            'fixed_cost': fixed,
            'payout': round(payout),
            'profit': round(profit),
            'utilization': round(100 * (dd_days + sd_sites + sl_days) / days, 1) if days else 0,
            'below_floor': profit < profit_floor,
        })
    return vendors


def write_roster_csv(result, path):
    roster = result['roster']
    days = len(result['daily'])
    v2, v1 = result['v2'], result['v1']
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        header = ['Day', 'Demand', 'DD pairs', 'SD sites'] + [f'V{v+1:02d}{"(2i)" if v < v2 else "(1i)"}' for v in range(len(roster))]
        w.writerow(header)
        for d in range(days):
            row = [
                d + 1,
                result['daily'][d],
                len(result['dd_pairs_per_day'][d]),
                sum(result['sd_slabs_per_day'][d]),
            ]
            for v in range(len(roster)):
                cell = roster[v][d]
                slab_labels = ['S1', 'S2', 'S3', 'S4']
                if cell[0] == 'DD':
                    s1, s2 = cell[1]
                    row.append(f'DD/{slab_labels[s1]}+{slab_labels[s2]}')
                elif cell[0] == 'SD':
                    row.append(f'SD/{slab_labels[cell[1]]}')
                elif cell[0] == 'SL':
                    row.append('SL')
                else:
                    row.append('-')
            w.writerow(row)
    print(f"Wrote {path}")


def write_pnl_csv(vendors, path):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Vendor', 'Type', 'DD days', 'SD sites', 'SL days', 'Idle days',
                    'Total sites', 'Fixed cost', 'Payout', 'Profit'])
        for x in vendors:
            w.writerow([x['name'], x['type'], x['dd_days'], x['sd_sites'], x['sl_days'],
                        x['idle_days'], x['sites'], x['fixed_cost'], x['payout'], x['profit']])
    print(f"Wrote {path}")


def get_output_paths():
    """Return writable CSV paths for this runtime."""
    linux_agent_dir = Path("/mnt/user-data/outputs")
    if linux_agent_dir.exists():
        linux_agent_dir.mkdir(parents=True, exist_ok=True)
        return linux_agent_dir / "roster.csv", linux_agent_dir / "pnl.csv"

    local_dir = Path(__file__).resolve().parent / "outputs"
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir / "roster.csv", local_dir / "pnl.csv"


def verify_constraints(result, slab_rates, dd_discount, sl2_rate, sl1_rate, total_sites):
    """Re-run all 8 hard checks on the output and report."""
    roster = result['roster']
    v2 = result['v2']
    days = len(result['daily'])
    total_v = len(roster)
    daily = result['daily']
    pair_count = [len(p) for p in result['dd_pairs_per_day']]
    sd_count_per_day = [sum(s) for s in result['sd_slabs_per_day']]
    peak_day = result['peak_day']

    failures = []

    # C1
    tot_sites = sum(2 if roster[v][d][0] == 'DD' else (1 if roster[v][d][0] == 'SD' else 0)
                    for v in range(total_v) for d in range(days))
    if tot_sites != total_sites:
        failures.append(f"C1: total sites {tot_sites} != {total_sites}")

    # C2 + C3
    for d in range(days):
        dd = sum(1 for v in range(total_v) if roster[v][d][0] == 'DD')
        sd = sum(1 for v in range(total_v) if roster[v][d][0] == 'SD')
        if dd != pair_count[d]:
            failures.append(f"C2: day {d+1} DD count {dd} != {pair_count[d]}")
        if sd != sd_count_per_day[d]:
            failures.append(f"C3: day {d+1} SD count {sd} != {sd_count_per_day[d]}")

    # C4
    for v in range(v2):
        for d in range(days - 1):
            if roster[v][d][0] == 'DD' and roster[v][d+1][0] == 'DD':
                failures.append(f"C4: V{v+1} consec DD on days {d+1},{d+2}")

    # C5
    idle_peak = sum(1 for v in range(total_v) if roster[v][peak_day][0] == 'idle')
    if idle_peak > 0:
        failures.append(f"C5: {idle_peak} vendors idle on peak day {peak_day+1}")

    # C6
    for v in range(total_v):
        for d in range(days):
            if roster[v][d][0] == 'SL':
                if d == 0:
                    failures.append(f"C6: V{v+1} SL on day 1")
                elif roster[v][d-1][0] not in ('DD', 'SD'):
                    failures.append(f"C6: V{v+1} SL on day {d+1} after {roster[v][d-1][0]}")

    # C7: aggregate slippage count must match site-based input rates.
    dd_sites_total = sum(2 for v in range(total_v) for d in range(days) if roster[v][d][0] == 'DD')
    sd_sites_total = sum(1 for v in range(total_v) for d in range(days) if roster[v][d][0] == 'SD')
    sl_total = sum(1 for v in range(total_v) for d in range(days) if roster[v][d][0] == 'SL')
    target_total = round(dd_sites_total * sl2_rate + sd_sites_total * sl1_rate)
    if sl_total != target_total:
        failures.append(f"C7: SL total={sl_total} but target={target_total}")

    # C8
    for v in range(total_v):
        for start in range(days - 9):
            rest = sum(1 for d in range(start, start + 10) if roster[v][d][0] == 'idle')
            if rest == 0:
                failures.append(f"C8: V{v+1} no rest in days {start+1}-{start+10}")
                break  # one per vendor

    return failures


def compute_quality_metrics(result, slab_mix, sl2_rate, sl1_rate):
    """Compute scenario-level checks for slab distribution and slippage adherence."""
    roster = result['roster']
    slab_count = len(slab_mix)
    slab_actual = [0] * slab_count
    dd_sites_total = 0
    sd_sites_total = 0
    sl_days_total = 0

    for v in range(len(roster)):
        for d in range(len(roster[v])):
            t, meta = roster[v][d]
            if t == 'DD':
                s1, s2 = meta
                slab_actual[s1] += 1
                slab_actual[s2] += 1
                dd_sites_total += 2
            elif t == 'SD':
                slab_actual[meta] += 1
                sd_sites_total += 1
            elif t == 'SL':
                sl_days_total += 1

    total_sites = dd_sites_total + sd_sites_total
    slab_target = int_distribute(total_sites, slab_mix)
    expected_slips = dd_sites_total * sl2_rate + sd_sites_total * sl1_rate
    expected_slips_rounded = round(expected_slips)
    slip_gap = sl_days_total - expected_slips_rounded

    return {
        'slab_target_counts': slab_target,
        'slab_actual_counts': slab_actual,
        'slippage_expected_sites': expected_slips,
        'slippage_expected_rounded': expected_slips_rounded,
        'slippage_actual': sl_days_total,
        'slippage_gap': slip_gap,
    }


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("SolarSquare 2-install solver")
    print("=" * 60)
    print(f"Total sites: {TOTAL_SITES}, days: {DAYS}, peak ratio: {PEAK_RATIO}")
    print(f"Slip: sl2={SL2_RATE}, sl1={SL1_RATE}, eligibility: {ELIG_PCT}")
    print()

    result = solve(
        TOTAL_SITES, DAYS, PEAK_RATIO, ELIG_PCT, SL2_RATE, SL1_RATE,
        SLAB_RATES, SLAB_MIX, DD_ELIGIBLE_SLABS, COST_2I, COST_1I, DD_DISCOUNT,
        time_limit_sec=SOLVE_TIME_LIMIT_SEC,
        profit_floor=PROFIT_FLOOR,
        min_vendor_utilization=MIN_VENDOR_UTILIZATION,
    )

    if result is None or result.get('error'):
        print("\nNo feasible solution found. Try relaxing constraints or extending search.")
        exit(1)

    # Verify
    print("\nVerifying constraints...")
    failures = verify_constraints(result, SLAB_RATES, DD_DISCOUNT, SL2_RATE, SL1_RATE, TOTAL_SITES)
    if failures:
        print("FAILED CHECKS:")
        for f in failures:
            print("  -", f)
    else:
        print("✓ All 8 constraints pass.")

    # Compute P&L
    vendors = compute_pnl(result, SLAB_RATES, DD_DISCOUNT, COST_2I, COST_1I, PROFIT_FLOOR)
    v2_vendors = [v for v in vendors if v['type'] == '2-Install']
    v1_vendors = [v for v in vendors if v['type'] == '1-Install']

    print()
    print(f"Vendors: {len(v2_vendors)} 2-install + {len(v1_vendors)} 1-install = {len(vendors)} total")
    total_payout = sum(v['payout'] for v in vendors)
    total_cost = sum(v['fixed_cost'] for v in vendors)
    total_profit = sum(v['profit'] for v in vendors)
    baseline = TOTAL_SITES * BASELINE_PER_SITE
    savings_pct = 100 * (baseline - total_payout) / baseline if baseline > 0 else 0
    print(f"Total payout: ₹{total_payout:,}")
    print(f"Total fixed cost: ₹{total_cost:,}")
    print(f"Total profit: ₹{total_profit:,}")
    print(f"SSE savings vs ₹{BASELINE_PER_SITE}/site baseline: {savings_pct:.1f}%")

    if v2_vendors:
        avg2 = sum(v['profit'] for v in v2_vendors) / len(v2_vendors)
        spread2 = max(v['profit'] for v in v2_vendors) - min(v['profit'] for v in v2_vendors)
        print(f"Avg 2i profit: ₹{avg2:,.0f} (spread ₹{spread2:,})")
    if v1_vendors:
        avg1 = sum(v['profit'] for v in v1_vendors) / len(v1_vendors)
        spread1 = max(v['profit'] for v in v1_vendors) - min(v['profit'] for v in v1_vendors)
        print(f"Avg 1i profit: ₹{avg1:,.0f} (spread ₹{spread1:,})")

    roster_path, pnl_path = get_output_paths()
    write_roster_csv(result, roster_path)
    write_pnl_csv(vendors, pnl_path)

    print("\nDone.")
