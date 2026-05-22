from solarsquare_solver import solve

# Test with peak_ratio = 1.0 (the failing scenario)
print("=" * 60)
print("Testing peak_ratio=1.0 (flat demand)")
print("=" * 60)

r = solve(
    530, 30, 1.0, 0.40, 0.10, 0.20,
    [8000, 8500, 10000, 15000],
    [0.098, 0.796, 0.10, 0.006],
    [True, True, False, False],
    180000, 140000, 0.7,
    time_limit_sec=30,
    profit_floor=0,
    min_vendor_utilization=0.50
)

if r is None or r.get('error'):
    print("\nFAILED:", r.get('error') if r else 'None')
    if r and r.get('simulations'):
        for s in r['simulations']:
            print(f"  v2={s['v2']}, v1={s['v1']}, total={s['total_v']}, status={s['status']}")
else:
    print(f"\nSUCCESS: v2={r['v2']}, v1={r['v1']}, total={r['v2']+r['v1']}")

print()
print("=" * 60)
print("Testing peak_ratio=1.3 (original scenario - should still work)")
print("=" * 60)

r2 = solve(
    530, 30, 1.3, 0.40, 0.10, 0.20,
    [8000, 8500, 10000, 15000],
    [0.098, 0.796, 0.10, 0.006],
    [True, True, False, False],
    180000, 140000, 0.7,
    time_limit_sec=30,
    profit_floor=0,
    min_vendor_utilization=0.50
)

if r2 is None or r2.get('error'):
    print("\nFAILED:", r2.get('error') if r2 else 'None')
else:
    print(f"\nSUCCESS: v2={r2['v2']}, v1={r2['v1']}, total={r2['v2']+r2['v1']}")
