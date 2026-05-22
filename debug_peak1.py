"""Quick debug script to trace why peak_ratio=1.0 gives infeasible."""
import math
from solarsquare_solver import (
    bell_curve_demand, compute_daily_demand, int_distribute,
    TOTAL_SITES, DAYS, ELIG_PCT, SL2_RATE, SL1_RATE,
    SLAB_MIX, DD_ELIGIBLE_SLABS, SLAB_RATES,
    COST_2I, COST_1I, DD_DISCOUNT, MIN_VENDOR_UTILIZATION
)

peak_ratio = 1.0
total_sites = 530
days = 30

daily = bell_curve_demand(total_sites, days, peak_ratio)
print("Daily demand:", daily)
print("Sum:", sum(daily))
print("Max:", max(daily), "Min:", min(daily))

dd_pairs_per_day, sd_slabs_per_day = compute_daily_demand(
    daily, SLAB_MIX, DD_ELIGIBLE_SLABS, ELIG_PCT
)
pair_count_per_day = [len(p) for p in dd_pairs_per_day]
sd_count_per_day = [sum(s) for s in sd_slabs_per_day]
peak_day = daily.index(max(daily))

total_dd_pairs = sum(pair_count_per_day)
total_sd_sites = sum(sd_count_per_day)
expected_total_sl = round((2 * total_dd_pairs) * SL2_RATE + total_sd_sites * SL1_RATE)
total_active_slots_required = total_dd_pairs + total_sd_sites + expected_total_sl

print(f"\nPeak day: {peak_day} (demand {daily[peak_day]})")
print(f"DD pairs per day: {pair_count_per_day}")
print(f"SD sites per day: {sd_count_per_day}")
print(f"Total DD pairs: {total_dd_pairs}, Total SD sites: {total_sd_sites}")
print(f"Expected total SL: {expected_total_sl}")
print(f"Total active slots required: {total_active_slots_required}")

# Lower bounds
lb_v2 = 0
for d in range(days - 1):
    lb_v2 = max(lb_v2, pair_count_per_day[d] + pair_count_per_day[d + 1])
lb_v2 = max(lb_v2, pair_count_per_day[peak_day])

v2_off_peak = lb_v2 - pair_count_per_day[peak_day]
lb_v1 = max(0, sd_count_per_day[peak_day] - v2_off_peak)

print(f"\nLower bounds: v2 >= {lb_v2}, v1 >= {lb_v1}")
print(f"Total vendors at lower bound: {lb_v2 + lb_v1}")

# Check C5: peak day no idle vendors
# On peak day, need pair_count_per_day[peak_day] DD vendors + sd_count_per_day[peak_day] SD vendors
# Plus SL vendors on peak day
# Total must be <= total_v (no idle)
peak_dd = pair_count_per_day[peak_day]
peak_sd = sd_count_per_day[peak_day]
print(f"\nPeak day needs: {peak_dd} DD + {peak_sd} SD = {peak_dd + peak_sd} working vendors")
print(f"But we also need SL vendors on peak day (C6+C7)")

# Check constraint: model.Add(day_sl[peak_day] >= 1)
# This means at LEAST 1 vendor must be doing SL on the peak day
# But C5 says NO vendor can be idle on peak day
# SL is NOT idle, so that's fine... BUT
# On peak day: DD + SD + SL = total_v (no idle)
# So: peak_dd + peak_sd + SL_on_peak = total_v
# With total_v = lb_v2 + lb_v1 = lb_v2 + max(0, peak_sd - v2_off_peak)

# When peak_ratio=1, demand is FLAT => all days have same demand
# => pair_count_per_day is same every day
# => lb_v2 = 2 * pair_count_per_day[0] (sum of two consecutive equal days)
# => v2_off_peak = lb_v2 - pair_count_per_day[peak_day] = pair_count_per_day[0]
# => lb_v1 = max(0, sd_count - pair_count) = sd_count - pair_count if sd > pair

total_v = lb_v2 + lb_v1
peak_work_needed = peak_dd + peak_sd  # minimum DD+SD on peak day

print(f"\nWith {total_v} vendors, peak day has {peak_work_needed} work slots")
print(f"Remaining for SL on peak: {total_v - peak_work_needed}")
print(f"But constraint requires day_sl[peak_day] >= 1")

# Check: is the problem that we have NO room for SL on peak day?
if total_v - peak_work_needed < 1:
    print("\n*** BUG FOUND: No room for SL on peak day! ***")
    print("C5 says no idle, so all vendors must be DD/SD/SL")
    print(f"But DD({peak_dd}) + SD({peak_sd}) already uses {peak_work_needed} of {total_v} vendors")
    print(f"And constraint requires at least 1 SL on peak day")
    print(f"So we need total_v >= {peak_work_needed + 1}")

# Also check: C4 (no consecutive DD) with flat demand
print(f"\n--- C4 analysis ---")
print(f"With flat demand, every day needs {peak_dd} DD pairs")
print(f"C4 says no vendor can do DD two days in a row")
print(f"So minimum 2i vendors = {peak_dd * 2} (need {peak_dd} DD/day, alternating)")
print(f"But lb_v2 calculation gives: {lb_v2}")

# The real issue: lb_v2 = max of consecutive pair sums
# With flat demand of N pairs/day: consecutive sum = N + N = 2N
# But C4 means a vendor doing DD on day d can't do DD on d+1
# So we need at least 2N vendors to serve N pairs/day
# lb_v2 = 2*N which is correct

# BUT: each of those 2i vendors that does DD on day d is NOT doing DD on d+1
# On d+1 they can do SD, SL, or idle
# So on peak day (which is every day for flat), we have:
# - peak_dd = N vendors doing DD  
# - lb_v2 - N = N vendors NOT doing DD (available for SD/SL/idle)
# Plus lb_v1 vendors available for SD/SL/idle
# C5: no idle => all must be DD/SD/SL
# So: N (DD) + (remaining doing SD or SL) = total_v
# Need: SD on peak = sd_count, plus SL >= 1
# So remaining = total_v - N must cover sd_count + SL(>=1)

remaining_v2 = lb_v2 - peak_dd  # 2i vendors not doing DD on peak day
print(f"\n--- Peak day capacity ---")
print(f"2i vendors doing DD: {peak_dd}")
print(f"2i vendors NOT doing DD: {remaining_v2}")
print(f"1i vendors: {lb_v1}")
print(f"Available for SD+SL: {remaining_v2 + lb_v1}")
print(f"Need SD: {peak_sd}, need SL >= 1, total need >= {peak_sd + 1}")
if remaining_v2 + lb_v1 < peak_sd + 1:
    print("*** PROBLEM: Not enough vendors for SD + SL on peak day ***")
else:
    print("OK: Enough room for SD + SL")

# Check utilisation constraint
util_floor_days = max(0, min(days, math.ceil(days * MIN_VENDOR_UTILIZATION)))
min_active_slots_implied = total_v * util_floor_days
print(f"\n--- Utilisation check ---")
print(f"util_floor_days = {util_floor_days}")
print(f"min_active_slots_implied = {min_active_slots_implied}")
print(f"total_active_slots_required = {total_active_slots_required}")
if min_active_slots_implied > total_active_slots_required:
    print("*** SKIPPED due to utilisation floor ***")
else:
    print("OK: Passes utilisation check")

# C8: rolling 10-day rest
# With flat demand, every day needs work => very hard to give rest days
# Let's check: total work per vendor
work_per_vendor_approx = total_active_slots_required / total_v
print(f"\n--- Rest day feasibility ---")
print(f"Approx work per vendor: {work_per_vendor_approx:.1f} of {days} days")
print(f"Rest days per vendor: ~{days - work_per_vendor_approx:.1f}")
print(f"C8 requires 1 rest day per 10-day window")
print(f"Min rest days needed: {days // 10} = {math.ceil(days / 10)}")
