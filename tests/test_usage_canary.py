#!/usr/bin/env python3
"""Pins the usage port to the golden numbers of eliophan/t3code-bar's canary.

`tests/canary/state` and `tests/canary/golden.json` are copied from the
original repository's `Tests/Canary`. The golden numbers come from its oracle,
a port of T3 Code's Usage page. Run with:

    python3 -I tests/test_usage_canary.py
"""

import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = json.load(open(os.path.join(HERE, "canary", "golden.json")))

os.environ["TZ"] = GOLDEN.get("timeZone") or "Asia/Ho_Chi_Minh"
time.tzset()
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import t3bar  # noqa: E402

PERIODS = {"Past 24h": 1, "7 days": 7, "30 days": 30, "90 days": 90}


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def main() -> int:
    state = os.path.join(HERE, "canary", "state")
    now = t3bar.parse_date(GOLDEN["now"])
    status, path, _ = t3bar.locate_cache(state)
    assert status == "available", status
    dataset = t3bar.load_dataset(path)
    prices = t3bar.load_prices(state)
    failures = 0

    def check(label, actual, expected):
        nonlocal failures
        ok = close(actual, expected) if isinstance(expected, float) or isinstance(actual, float) else actual == expected
        if not ok:
            failures += 1
            print(f"  FAIL {label}: got {actual!r}, want {expected!r}")

    for name, days in PERIODS.items():
        want = GOLDEN["periods"][name]
        got = t3bar.build_report(dataset, t3bar.make_window(days, now), prices)
        print(name)
        check("costUsd", got["cost"], want["costUsd"])
        check("cacheSavingsUsd", got["cacheSavings"], want["cacheSavingsUsd"])
        check("totalTokens", got["processed"], want["totalTokens"])
        check("uncachedInput", got["tokens"]["uncached"], want["uncachedInput"])
        check("cachedInput", got["tokens"]["cached"], want["cachedInput"])
        check("cacheCreation", got["tokens"]["creation"], want["cacheCreation"])
        check("output", got["tokens"]["output"], want["output"])
        check("reasoning", got["tokens"]["reasoning"], want["reasoning"])
        check("records", got["records"], want["records"])
        check("unpricedRecords", got["unpriced"], want["unpricedRecords"])
        check("sessions", got["sessions"], want["sessions"])
        check("duplicatesDropped", got["duplicatesDropped"], want["duplicatesDropped"])
        for field, value in want["costByType"].items():
            check(f"costByType.{field}", got["costByType"][field], value)
        for field, value in want["costBySpeed"].items():
            check(f"costBySpeed.{field}", got["costBySpeed"][field], value)
        providers = {p["kind"]: p for p in got["providers"]}
        check("providers", sorted(providers), sorted(want["providers"]))
        for kind, value in want["providers"].items():
            p = providers.get(kind, {})
            check(f"providers.{kind}.cost", p.get("cost", 0.0), value["cost"])
            check(f"providers.{kind}.tokens", p.get("tokens", 0), value["tokens"])
            check(f"providers.{kind}.sessions", p.get("sessions", 0), value["sessions"])
        models = {f"{m['kind']}/{m['name']}": m for m in got["models"]}
        check("models", sorted(models), sorted(want["models"]))
        for key, value in want["models"].items():
            m = models.get(key, {})
            check(f"models.{key}.cost", m.get("cost", 0.0), value["cost"])
            check(f"models.{key}.tokens", m.get("tokens", 0), value["tokens"])
            check(f"models.{key}.unpriced", m.get("unpriced", False), value["unpriced"] >= value["records"] > 0)

    print("OK" if failures == 0 else f"{failures} mismatches")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
