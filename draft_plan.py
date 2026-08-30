#!/usr/bin/env python3
"""Target matrix for the pick-9 snake draft.

Consumes ``data/pool.json`` and produces a round-by-round plan: which position
to attack at each of our sixteen picks, which tier we expect to still be alive
when the pick arrives, and which specific names to have queued.

The structural facts this plan is built on
------------------------------------------
Slot 9 of 12 produces this pick sequence:

    R1   9      R5   57     R9   105    R13  153
    R2   16     R6   64     R10  112    R14  160
    R3   33     R7   81     R11  129    R15  177
    R4   40     R8   88     R12  136    R16  184

Note the alternation in the gaps: 6, 16, 6, 16, ... We pick twice in quick
succession (9/16, 33/40, 57/64 ...) and then wait through sixteen selections.

That rhythm, not any ranking, is what drives the plan:

* On a **short turn** (6 players off the board) we can let a tier ride. If two
  players we rate equally are both available, we take the scarcer position and
  expect the other to survive six picks.
* On a **long drought** (16 players off the board) an entire tier will empty.
  Any tier we still need must be entered *now*, at its last member, because it
  will not exist at our next selection.

Positional value hoarding
-------------------------
Full PPR does not make running backs valuable — it makes *pass-catching*
running backs valuable, and it deepens the wide receiver pool enormously.
Roughly 29 RBs and 29 WRs start league-wide each week (2 + a flex share, over
12 teams), but the WR pool stays productive far deeper than the RB pool does.
The consequence is that RB value decays faster than WR value, so early picks
are spent hoarding the position whose replacement level is furthest below its
elite tier — which in this economy is almost always RB, then WR.

Quarterback value compression
-----------------------------
At 4 points per passing touchdown with -2 per interception, the spread from
QB1 to QB12 is narrow enough that a starting quarterback is a commodity. Every
pick spent on one before the tier actually breaks is value burned. The plan
therefore holds QB until the last round in which the position's current tier
still has more members than there are teams still needing a starter.

Usage:
    python draft_plan.py                 # full target matrix
    python draft_plan.py --refresh       # re-pull the pool first
    python draft_plan.py --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

from ffie.config import (
    DATA_DIR,
    OUR_GAPS,
    OUR_PICKS,
    SCORING,
    SHAPE,
)

POOL_PATH = DATA_DIR / "pool.json"

# Round -> ordered position priorities. Encodes the strategy described above.
# "BPA" resolves to the highest-VOR player at any of the listed positions.
ROUND_PRIORITIES: dict[int, tuple[str, ...]] = {
    1: ("RB", "WR"),
    2: ("RB", "WR"),
    3: ("WR", "RB"),
    4: ("WR", "RB", "TE"),
    5: ("RB", "WR", "TE"),
    6: ("WR", "RB", "TE"),
    7: ("RB", "WR", "TE"),
    8: ("WR", "RB", "TE"),
    9: ("QB", "RB", "WR"),
    10: ("QB", "RB", "WR"),
    11: ("RB", "WR", "QB"),
    12: ("RB", "WR"),
    13: ("WR", "RB"),
    14: ("WR", "RB", "TE"),
    15: ("D/ST",),
    16: ("K",),
}


def load_pool(refresh: bool) -> dict:
    if refresh or not POOL_PATH.exists():
        print("  pulling fresh pool ...")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "pull_pool.py"), "--quiet"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout, result.stderr, file=sys.stderr)
            raise SystemExit("pull_pool.py failed — cannot build a plan.")
    if not POOL_PATH.exists():
        raise SystemExit(f"{POOL_PATH} not found. Run: python pull_pool.py")
    return json.loads(POOL_PATH.read_text())


def detect_tiers(
    group: list[dict],
    sensitivity: float = 1.5,
    min_tier_size: int = 3,
) -> list[list[dict]]:
    """Split a position list into tiers at statistically large scoring gaps.

    A tier is a set of players who are, for drafting purposes, the same player.
    The break points are where the projection curve steps down noticeably more
    than it has been stepping — not at arbitrary round numbers.

    ``min_tier_size`` matters more than it looks. Without it, a noisy
    projection curve shatters into singleton tiers, and every player becomes
    "the last one in his tier." That turns the scarcity signal the war room
    depends on into constant noise, which is worse than having no signal: it
    justifies reaching for anybody. A tier has to be able to hold more than one
    person before "the tier is emptying" means anything.
    """
    if len(group) < min_tier_size + 1:
        return [group] if group else []

    points = [p["projected_points"] for p in group]
    gaps = [a - b for a, b in zip(points, points[1:])]
    positive = [g for g in gaps if g > 0]
    if not positive:
        return [group]

    threshold = statistics.mean(positive) + sensitivity * (
        statistics.pstdev(positive) or 0.0
    )

    tiers: list[list[dict]] = []
    current: list[dict] = [group[0]]
    for player, gap in zip(group[1:], gaps):
        if gap >= threshold and len(current) >= min_tier_size:
            tiers.append(current)
            current = [player]
        else:
            current.append(player)
    if current:
        # Never leave a stub tier dangling at the bottom of the board.
        if len(current) < min_tier_size and tiers:
            tiers[-1].extend(current)
        else:
            tiers.append(current)
    return tiers


def survival_estimate(player: dict, overall_pick: int) -> float:
    """Rough probability the player is still on the board at ``overall_pick``.

    Modelled off ESPN's own PPR draft rank as a proxy for consensus ADP, with a
    deliberately wide uncertainty band: real drafts deviate from ADP by roughly
    a round in either direction, and a 12-team room of humans deviates more.
    This is a triage aid for ordering a queue, not a forecast — it is never
    used to override an explicit tier break.
    """
    adp = player.get("adp_rank")
    if not adp:
        return 0.5
    # Logistic centred on ADP with a ~12-pick (one full round) scale.
    delta = (adp - overall_pick) / 12.0
    return round(1.0 / (1.0 + pow(2.718281828, -delta)), 2)


def build_plan(board: dict) -> dict:
    buckets = board["players_by_position"]
    tiers_by_position = {
        position: detect_tiers(group) for position, group in buckets.items()
    }

    rows = []
    for index, (pick, priorities) in enumerate(
        zip(OUR_PICKS, [ROUND_PRIORITIES[r] for r in range(1, SHAPE.rounds + 1)])
    ):
        rnd = index + 1
        gap_after = OUR_GAPS[index] if index < len(OUR_GAPS) else None
        turn = (
            "final pick" if gap_after is None
            else "SHORT TURN" if gap_after <= 8
            else "LONG DROUGHT"
        )

        targets = []
        for position in priorities:
            group = buckets.get(position, [])
            candidates = [
                {
                    "name": p["name"],
                    "pro_team": p["pro_team"],
                    "position": position,
                    "position_rank": p["position_rank"],
                    "projected_points": p["projected_points"],
                    "vor": p.get("vor", 0),
                    "adp_rank": p.get("adp_rank"),
                    "survival": survival_estimate(p, pick),
                }
                for p in group
                if 0.15 <= survival_estimate(p, pick) <= 0.97
            ]
            candidates.sort(key=lambda c: -c["vor"])
            targets.extend(candidates[:4])

        targets.sort(key=lambda c: -c["vor"])

        rows.append(
            {
                "round": rnd,
                "overall_pick": pick,
                "priorities": list(priorities),
                "players_off_board_before_next": gap_after,
                "turn_type": turn,
                "directive": _directive(rnd, turn, priorities),
                "targets": targets[:6],
            }
        )

    return {
        "season": board["season"],
        "draft_slot": SHAPE.draft_slot,
        "team_count": SHAPE.team_count,
        "picks": OUR_PICKS,
        "gaps": OUR_GAPS,
        "tier_counts": {
            position: [len(tier) for tier in tiers]
            for position, tiers in sorted(tiers_by_position.items())
        },
        "replacement_levels": board["replacement_levels"],
        "rounds": rows,
    }


def _directive(rnd: int, turn: str, priorities: tuple[str, ...]) -> str:
    head = priorities[0]
    if rnd == 1:
        return ("Anchor. Take the highest-VOR back or receiver on the board. "
                "Do not reach for positional need in round 1 — there is no need yet.")
    if rnd in (15, 16):
        return f"Stream slot. Take any startable {head}; the spread here is noise."
    if head == "QB":
        return ("QB window. Take the last member of the current QB tier. "
                "If that tier still has more bodies than teams needing a starter, "
                "pass and take the best RB/WR instead.")
    if turn == "LONG DROUGHT":
        return (f"Sixteen players come off before we pick again. Enter any tier "
                f"we still need NOW — prefer {head} — rather than the marginally "
                f"better player from a tier that will survive.")
    return (f"Short turn: only six picks until we are back up. Take the scarcer "
            f"position ({head}) and expect the other tier to hold.")


def render(plan: dict) -> None:
    line = "=" * 78
    thin = "-" * 78
    print(f"\n{line}")
    print(f"TARGET MATRIX  —  slot {plan['draft_slot']} of {plan['team_count']}, "
          f"snake, {plan['season']}")
    print(f"{line}")
    print(f"  Scoring     full PPR ({SCORING.reception}/rec), "
          f"{SCORING.passing_td} pt pass TD, {SCORING.interception} turnovers")
    print(f"  QB posture  {'COMPRESSED — wait' if SCORING.qb_is_compressed else 'premium'}")
    print(f"  Our picks   {', '.join(str(p) for p in plan['picks'])}")
    print(f"  Gaps        {', '.join(str(g) for g in plan['gaps'])}")

    print(f"\n{thin}\nTIER STRUCTURE (players per tier, best tier first)\n{thin}")
    for position, counts in plan["tier_counts"].items():
        print(f"  {position:<6} {counts[:9]}")

    for row in plan["rounds"]:
        print(f"\n{thin}")
        marker = "!!" if row["turn_type"] == "LONG DROUGHT" else "  "
        print(f"{marker} ROUND {row['round']:<2}  overall pick {row['overall_pick']:<4} "
              f"[{row['turn_type']}]  priorities: {' > '.join(row['priorities'])}")
        print(f"{thin}")
        print(f"   {row['directive']}")
        if not row["targets"]:
            print("   (no candidates in the survival window — board is thin here)")
            continue
        print(f"\n   {'PLAYER':<24} {'POS':<5} {'TM':<4} {'PROJ':>7} {'VOR':>7} {'AVAIL':>6}")
        for target in row["targets"]:
            print(
                f"   {target['name'][:24]:<24} "
                f"{target['position'] + str(target['position_rank']):<5} "
                f"{target['pro_team']:<4} {target['projected_points']:>7.1f} "
                f"{target['vor']:>7.1f} {int(target['survival'] * 100):>5}%"
            )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="re-pull the pool first")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    board = load_pool(args.refresh)
    plan = build_plan(board)

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "plan.json").write_text(json.dumps(plan, indent=2))

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        render(plan)
        print(f"  wrote {DATA_DIR / 'plan.json'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
