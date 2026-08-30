#!/usr/bin/env python3
"""Telemetry & rulebook decoder.

Connects to the ESPN backend with the captured session credentials and prints
the league's *actual* configuration: roster slot constraints, scoring
coefficients, and structural parameters.

This is the validation gate for every downstream script. The engine's draft
logic is calibrated to a specific economy — 12 teams, full PPR, 4-point
passing touchdowns, -2 turnovers. If any of those change, the value model in
``draft_plan.py`` is quietly wrong. Rather than let that happen silently, this
script diffs the live settings against the expected constants in
``ffie/config.py`` and exits non-zero on a mismatch.

Usage:
    python get_settings.py              # human-readable report
    python get_settings.py --json       # machine-readable, for piping
    python get_settings.py --strict     # exit 1 on any validation mismatch
"""

from __future__ import annotations

import argparse
import json
import sys

from ffie.config import DATA_DIR, SCORING, SHAPE, ConfigError, load_credentials
from ffie.espn_client import SLOT_MAP, ESPNAuthError, RawClient, connect

# ESPN statId -> the scoring coefficients we actually care about.
TRACKED_STATS = {
    3: "passing_yards",
    4: "passing_td",
    20: "interception",
    24: "rushing_yards",
    25: "rushing_td",
    42: "receiving_yards",
    43: "receiving_td",
    53: "reception",
    72: "fumble_lost",
}


def decode_roster_slots(raw_settings: dict) -> dict[str, int]:
    """Turn ``lineupSlotCounts`` into named slots with non-zero counts."""
    counts = (raw_settings.get("rosterSettings") or {}).get("lineupSlotCounts", {})
    decoded: dict[str, int] = {}
    for slot_id, count in counts.items():
        if not count:
            continue
        name = SLOT_MAP.get(int(slot_id), f"SLOT_{slot_id}")
        decoded[name] = int(count)
    return decoded


def decode_scoring(raw_settings: dict) -> dict[str, float]:
    """Extract the coefficients that define the league's economy."""
    items = (raw_settings.get("scoringSettings") or {}).get("scoringItems", [])
    decoded: dict[str, float] = {}
    for item in items:
        key = TRACKED_STATS.get(item.get("statId"))
        if key is None:
            continue
        override = (item.get("pointsOverrides") or {}).get("16")
        decoded[key] = float(override if override is not None else item.get("points", 0))
    return decoded


def validate(scoring: dict[str, float], slots: dict[str, int], team_count: int) -> list[str]:
    """Diff live settings against the calibrated expectation. Returns problems."""
    problems: list[str] = []

    def check(label: str, actual, expected):
        if actual is None:
            problems.append(f"{label}: not reported by ESPN (expected {expected})")
        elif abs(float(actual) - float(expected)) > 1e-6:
            problems.append(f"{label}: live={actual} expected={expected}")

    check("reception (PPR)", scoring.get("reception"), SCORING.reception)
    check("passing TD", scoring.get("passing_td"), SCORING.passing_td)
    check("interception", scoring.get("interception"), SCORING.interception)
    check("fumble lost", scoring.get("fumble_lost"), SCORING.fumble_lost)

    if team_count != SHAPE.team_count:
        problems.append(f"team count: live={team_count} expected={SHAPE.team_count}")

    starting = sum(v for k, v in slots.items() if k not in ("BE", "IR"))
    total = sum(slots.values())
    if total and total != SHAPE.roster_size:
        problems.append(f"roster size: live={total} expected={SHAPE.roster_size}")

    expected_starting = sum(SHAPE.starters.values())
    if starting and starting != expected_starting:
        problems.append(
            f"starting slots: live={starting} expected={expected_starting}"
        )

    return problems


def build_report() -> dict:
    creds = load_credentials()
    league = connect(creds)
    raw = RawClient(creds).get(["mSettings"]).get("settings", {})

    slots = decode_roster_slots(raw)
    scoring = decode_scoring(raw)
    settings = league.settings

    return {
        "league": {
            "id": creds.league_id,
            "name": settings.name,
            "season": creds.season,
            "team_count": settings.team_count,
            "scoring_type": settings.scoring_type,
            "regular_season_weeks": settings.reg_season_count,
            "playoff_teams": settings.playoff_team_count,
            "keeper_count": settings.keeper_count,
        },
        "roster_slots": slots,
        "bench_slots": slots.get("BE", 0),
        "roster_size": sum(slots.values()),
        "scoring": scoring,
        "teams": [
            {"id": t.team_id, "name": t.team_name, "owner": getattr(t, "owners", None)}
            for t in league.teams
        ],
        "validation": validate(scoring, slots, settings.team_count),
    }


def render(report: dict) -> None:
    league = report["league"]
    line = "-" * 68

    print(f"\n{line}\nLEAGUE TELEMETRY\n{line}")
    print(f"  Name              {league['name']}")
    print(f"  League ID         {league['id']}    Season  {league['season']}")
    print(f"  Teams             {league['team_count']}")
    print(f"  Scoring type      {league['scoring_type']}")
    print(f"  Regular season    {league['regular_season_weeks']} weeks"
          f"    Playoff teams  {league['playoff_teams']}")
    print(f"  Keepers           {league['keeper_count']}")

    print(f"\n{line}\nROSTER CONSTRAINTS\n{line}")
    for slot, count in report["roster_slots"].items():
        tag = "  (bench)" if slot == "BE" else "  (IR)" if slot == "IR" else ""
        print(f"  {slot:<8} x{count}{tag}")
    print(f"  {'TOTAL':<8} x{report['roster_size']}")

    print(f"\n{line}\nSCORING ECONOMY\n{line}")
    for key in sorted(report["scoring"]):
        print(f"  {key:<20} {report['scoring'][key]:>7}")

    print(f"\n{line}\nVALIDATION\n{line}")
    if not report["validation"]:
        print("  PASS — live settings match the calibrated model in ffie/config.py")
    else:
        print("  MISMATCH — downstream draft logic is calibrated for different rules:")
        for problem in report["validation"]:
            print(f"    - {problem}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 if validation finds a mismatch")
    parser.add_argument("--save", action="store_true",
                        help="write data/settings.json")
    args = parser.parse_args()

    try:
        report = build_report()
    except (ConfigError, ESPNAuthError) as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    if args.save:
        DATA_DIR.mkdir(exist_ok=True)
        (DATA_DIR / "settings.json").write_text(json.dumps(report, indent=2))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render(report)

    if args.strict and report["validation"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
