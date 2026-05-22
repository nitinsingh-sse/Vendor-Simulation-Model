from flask import Flask, request, jsonify, send_from_directory
from solarsquare_solver import solve, compute_pnl, compute_quality_metrics

app = Flask(__name__)


@app.after_request
def add_cors_headers(resp):
    # Lets the frontend call /solve even when index.html is opened directly.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "solarsquare-simulator"})


@app.route("/solve", methods=["POST"])
def solve_api():
    try:
        p = request.json
        print(f"/solve called: sites={p.get('total_sites')} days={p.get('days')} timeout={p.get('time_limit_sec')}")

        result = solve(
            total_sites=p["total_sites"],
            days=p["days"],
            peak_ratio=p["peak_ratio"],
            elig_pct=p["elig_pct"],
            sl2_rate=p["sl2_rate"],
            sl1_rate=p["sl1_rate"],
            slab_rates=p["slab_rates"],
            slab_mix=p["slab_mix"],
            dd_elig_slabs=p["dd_elig_slabs"],
            cost_2i=p["cost_2i"],
            cost_1i=p["cost_1i"],
            dd_discount=p["dd_discount"],
            time_limit_sec=p["time_limit_sec"],
            profit_floor=p.get("profit_floor", 0),
            max_vendor_search_extra=p.get("max_vendor_search_extra", 15)
        )

        if result is None or result.get("error"):
            return jsonify({
                "ok": False,
                "reason": result.get("error", "No feasible solution found") if result else "No feasible solution found",
                "simulations": result.get("simulations", []) if result else []
            })

        vendors = compute_pnl(
            result,
            p["slab_rates"],
            p["dd_discount"],
            p["cost_2i"],
            p["cost_1i"],
            p.get("profit_floor", 0)
        )

        total_payout = sum(v["payout"] for v in vendors)
        total_cost = sum(v["fixed_cost"] for v in vendors)
        total_profit = sum(v["profit"] for v in vendors)

        baseline = p["total_sites"] * p["baseline_per_site"]

        savings_pct = (
            100 * (baseline - total_payout) / baseline
            if baseline > 0 else 0
        )

        v2_vendors = [v for v in vendors if v["type"] == "2-Install"]
        v1_vendors = [v for v in vendors if v["type"] == "1-Install"]

        avg_2i = (
            sum(v["profit"] for v in v2_vendors) / len(v2_vendors)
            if v2_vendors else 0
        )

        avg_1i = (
            sum(v["profit"] for v in v1_vendors) / len(v1_vendors)
            if v1_vendors else 0
        )

        spread_2i = (
            max(v["profit"] for v in v2_vendors)
            - min(v["profit"] for v in v2_vendors)
            if len(v2_vendors) > 1 else 0
        )

        spread_1i = (
            max(v["profit"] for v in v1_vendors)
            - min(v["profit"] for v in v1_vendors)
            if len(v1_vendors) > 1 else 0
        )
        quality = compute_quality_metrics(
            result,
            p["slab_mix"],
            p["sl2_rate"],
            p["sl1_rate"]
        )

        roster = []

        for v in result["roster"]:
            row = []

            for cell in v:
                if cell[0] == "DD":
                    row.append({
                        "type": "DD",
                        "label": f"S{cell[1][0]+1}+S{cell[1][1]+1}"
                    })

                elif cell[0] == "SD":
                    row.append({
                        "type": "SD",
                        "label": f"S{cell[1]+1}"
                    })

                elif cell[0] == "SL":


                    row.append({
                        "type": "SL",
                        "label": ""
                    })

                else:
                    row.append({
                        "type": "idle",
                        "label": ""
                    })

            roster.append(row)

        return jsonify({
            "ok": True,
            "status": result["solver_status"],
            "v2": result["v2"],
            "v1": result["v1"],
            "total_v": result["v2"] + result["v1"],
            "daily": result["daily"],
            "peak_day": result["peak_day"],
            "pair_count_per_day": [
                len(x) for x in result["dd_pairs_per_day"]
            ],
            "roster": roster,
            "vendors": vendors,
            "total_payout": total_payout,
            "total_cost": total_cost,
            "total_profit": total_profit,
            "savings_pct": savings_pct,
            "avg_2i": avg_2i,
            "avg_1i": avg_1i,
            "spread_2i": spread_2i,
            "spread_1i": spread_1i,
            "simulations": result.get("simulations", []),
            "quality": quality,
            "warning": result.get("warning")
        })

    except Exception as e:
        import traceback

        return jsonify({
            "ok": False,
            "reason": str(e),
            "traceback": traceback.format_exc()
        })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
