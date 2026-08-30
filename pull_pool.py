#!/usr/bin/env python3
"""Global asset inventory.

Indexes every draftable player by position, professional team affiliation, and
season projection, then writes a normalised board to ``data/pool.json`` for the
planner and the war-room assistant to consume.

Two sources, cross-checked:

* ``universe``     — the ``kona_player_info`` view, sorted by ESPN's own PPR
                     draft ranking. This is the complete pool including players
                     already rostered, which is what you want *before* a draft.
* ``free-agents``  — the ``free_agents`` endpoint via ``espn-api``. This is the
                     live-availability view, which is what you want *during* a
                     season. Mid-draft it can lag the board by a pick or two,
                     which is exactly why ``draft_assistant.py`` derives
                     availability by subtracting drafted ids instead.

Every projection is already denominated in the league's own scoring, so the
full-PPR reception credit and the 4-point passing touchdown are baked in by
ESPN before the numbers reach us. No re-scoring is needed or attempted.

Usage:
    python pull_pool.py                       # full board, both sources
    python pull_pool.py --position RB --top 40
    python pull_pool.py --source free-agents
    python pull_pool.py --csv                 # also write data/pool.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict

from ffie.config import DATA_DIR, DRAFTABLE_POSITIONS, SHAPE, ConfigError, load_credentials
from ffie.espn_client import ESPNAuthError, RawClient, connect, normalise_player

POOL_PATH = DATA_DIR / "pool.json"
CSV_PATH = DATA_DIR / "pool.csv"


def pull_universe(creds, limit: int) -> list[dict]:
    client = RawClient(creds)
    raw = client.player_universe(limit=limit)
    players = [normalise_player(entry, creds.season) for entry in raw]
    return [p for p in players if p["position"] in DRAFTABLE_POSITIONS and p["name"]]


def pull_free_agents(creds, per_position: int) -> list[dict]:
    league = connect(creds)
    players: list[dict] = []
    for position in DRAFTABLE_POSITIONS:
        try:
            found = league.free_agents(size=per_position, position=position)
        except Exception as exc:
            print(f"  warn: free_agents({position}) failed: {exc}", file=sys.stderr)
            continue
        for player in found:
            players.append(
                {
                    "player_id": player.playerId,
                    "name": player.name,
                    "position": player.position,
                    "pro_team": player.proTeam,
                    "projected_points": round(float(player.projected_total_points or 0), 2),
                    "adp_rank": getattr(player, "posRank", None),
                    "percent_owned": getattr(player, "percent_owned", -1),
                    "injury_status": getattr(player, "injuryStatus", "ACTIVE"),
                    "injured": bool(getattr(player, "injured", False)),
                }
            )
    return players


def index_by_position(players: list[dict]) -> dict[str, list[dict]]:
    """Position-specific sorting matrices, ranked by projected points.

    Sorting *within* position rather than across it is the whole point. A
    cross-positional list ranked by raw projection puts quarterbacks on top and
    is actively misleading: in this economy the QB1 outscores the QB12 by a
    margin the bench absorbs, while the RB1/RB30 gap decides seasons.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for player in players:
        buckets[player["position"]].append(player)

    for position, group in buckets.items():
        group.sort(key=lambda p: (-p["projected_points"], p["adp_rank"] or 9999))
        for rank, player in enumerate(group, start=1):
            player["position_rank"] = rank
    return dict(buckets)


def replacement_levels(buckets: dict[str, list[dict]]) -> dict[str, float]:
    """Projected points of the last startable player at each position.

    Replacement level is where a position stops being scarce. With 12 teams,
    league-wide demand is ~29 RB, ~29 WR, 13 TE, 12 QB (flex-adjusted in
    ``ffie/config.py``). The Nth-best player at a position is what you can
    still get for free; everything above that line is the only value a draft
    pick can actually buy.
    """
    levels: dict[str, float] = {}
    for position, group in buckets.items():
        demand = SHAPE.league_wide_demand(position) or len(group)
        index = min(demand, len(group)) - 1
        levels[position] = group[index]["projected_points"] if index >= 0 else 0.0
    return levels


def apply_vor(buckets: dict[str, list[dict]], levels: dict[str, float]) -> None:
    """Value Over Replacement — the only number comparable across positions."""
    for position, group in buckets.items():
        baseline = levels.get(position, 0.0)
        for player in group:
            player["vor"] = round(player["projected_points"] - baseline, 2)


def build_board(source: str, limit: int, per_position: int) -> dict:
    creds = load_credentials()

    universe = pull_universe(creds, limit) if source in ("universe", "both") else []
    free_agents = (
        pull_free_agents(creds, per_position) if source in ("free-agents", "both") else []
    )

    primary = universe or free_agents
    if not primary:
        raise RuntimeError("No players returned from either source.")

    buckets = index_by_position(primary)
    levels = replacement_levels(buckets)
    apply_vor(buckets, levels)

    available_ids = {p["player_id"] for p in free_agents} if free_agents else None

    return {
        "season": creds.season,
        "league_id": creds.league_id,
        "source": source,
        "counts": {position: len(group) for position, group in sorted(buckets.items())},
        "replacement_levels": {k: round(v, 2) for k, v in sorted(levels.items())},
        "free_agent_ids": sorted(available_ids) if available_ids else [],
        "players_by_position": buckets,
    }


def render(board: dict, position_filter: str | None, top: int) -> None:
    line = "-" * 78
    print(f"\n{line}\nGLOBAL ASSET INVENTORY  —  season {board['season']}"
          f"  ({board['source']})\n{line}")

    print("  Pool depth:  " + "   ".join(
        f"{pos} {count}" for pos, count in board["counts"].items()
    ))
    print("  Replacement: " + "   ".join(
        f"{pos} {value}" for pos, value in board["replacement_levels"].items()
    ))

    positions = [position_filter] if position_filter else list(DRAFTABLE_POSITIONS)
    for position in positions:
        group = board["players_by_position"].get(position, [])
        if not group:
            continue
        demand = SHAPE.league_wide_demand(position)
        print(f"\n{line}\n{position}   "
              f"(league-wide weekly demand ~{demand}; "
              f"replacement {board['replacement_levels'].get(position)})\n{line}")
        print(f"  {'#':>3}  {'PLAYER':<26} {'TM':<4} {'PROJ':>7} {'VOR':>7} "
              f"{'OWN%':>6}  STATUS")
        for player in group[:top]:
            flag = "" if player["injury_status"] in ("ACTIVE", "NORMAL") else \
                f"  {player['injury_status']}"
            marker = "  <<< replacement line" if player["position_rank"] == demand else ""
            print(
                f"  {player['position_rank']:>3}  {player['name'][:26]:<26} "
                f"{player['pro_team']:<4} {player['projected_points']:>7.1f} "
                f"{player['vor']:>7.1f} {player['percent_owned']:>6}"
                f"{flag}{marker}"
            )
    print()


def write_csv(board: dict) -> None:
    rows = [
        {**player, "position": position}
        for position, group in board["players_by_position"].items()
        for player in group
    ]
    rows.sort(key=lambda r: -r.get("vor", 0))
    fields = ["position_rank", "position", "name", "pro_team", "projected_points",
              "vor", "adp_rank", "percent_owned", "injury_status", "player_id"]
    with CSV_PATH.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  wrote {CSV_PATH}  ({len(rows)} rows)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("universe", "free-agents", "both"),
                        default="both")
    parser.add_argument("--limit", type=int, default=800,
                        help="universe pull size (default: 800)")
    parser.add_argument("--per-position", type=int, default=120,
                        help="free-agent pull size per position (default: 120)")
    parser.add_argument("--position", choices=DRAFTABLE_POSITIONS,
                        help="render one position only")
    parser.add_argument("--top", type=int, default=30, help="rows per position")
    parser.add_argument("--csv", action="store_true", help="also write data/pool.csv")
    parser.add_argument("--quiet", action="store_true", help="write files, skip render")
    args = parser.parse_args()

    try:
        board = build_board(args.source, args.limit, args.per_position)
    except (ConfigError, ESPNAuthError) as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(exist_ok=True)
    POOL_PATH.write_text(json.dumps(board, indent=2))

    if not args.quiet:
        render(board, args.position, args.top)
    print(f"  wrote {POOL_PATH}")
    if args.csv:
        write_csv(board)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
