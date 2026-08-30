#!/usr/bin/env python3
"""The war-room advisor.

An interactive command-line assistant for a live draft. Every prompt re-queries
the ESPN backend, so the board in front of you is the board that exists right
now — not a snapshot taken when the session started.

What it does on each poll
-------------------------
1. Fetches ``mDraftDetail`` — the narrowest possible request, one view, no
   filter, so it returns fast even while ESPN is under draft-night load.
2. Subtracts every drafted player id from the cached player universe. This is
   deliberately *not* a call to the free-agent endpoint, which lags the live
   board by a pick or two at exactly the moment that lag matters most.
3. Recomputes our roster against the league's starting-slot constraints and
   reports which slots are still empty.
4. Ranks the remaining pool by value over replacement, then adjusts for two
   things a raw ranking cannot see: whether a position fills a hole we still
   have, and whether a tier is about to empty before our next pick.

Commands
--------
    rec [n]      ranked recommendations for our next pick (default 8)
    board [n]    best available overall, ignoring need
    pos <POS>    best available at one position
    roster       our roster and which starting slots are still open
    taken [n]    the most recent picks league-wide
    runs         positional run detection over the last 12 picks
    gaps         our remaining picks and the drought structure
    sync         force a re-poll
    help         this list
    quit         exit

Usage:
    python draft_assistant.py
    python draft_assistant.py --team-id 4
    python draft_assistant.py --offline      # mock mode, picks entered by hand
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ffie.config import (
    DATA_DIR,
    OUR_GAPS,
    OUR_PICKS,
    SCORING,
    SHAPE,
    ConfigError,
    load_credentials,
)
from ffie.espn_client import ESPNAuthError, RawClient

POOL_PATH = DATA_DIR / "pool.json"
OFFLINE_TEAM_ID = 0  # our team in --offline mock sessions

BAR = "=" * 76
THIN = "-" * 76


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class DraftState:
    """Live view of the draft, refreshed on demand."""

    def __init__(self, board: dict, team_id: int | None, offline: bool = False):
        self.board = board
        self.team_id = OFFLINE_TEAM_ID if offline else team_id
        self.offline = offline
        self.client: RawClient | None = None if offline else RawClient()

        self.players: dict[int, dict] = {}
        for group in board["players_by_position"].values():
            for player in group:
                self.players[player["player_id"]] = player

        self.by_name = {p["name"].lower(): p for p in self.players.values()}
        self.picks: list[dict] = []
        self.last_sync: float = 0.0
        self.tiers = self._build_tiers()

    # -- tiers ------------------------------------------------------------

    def _build_tiers(self) -> dict[int, tuple[str, int]]:
        """Map player_id -> (position, tier_index)."""
        from draft_plan import detect_tiers

        mapping: dict[int, tuple[str, int]] = {}
        for position, group in self.board["players_by_position"].items():
            for tier_index, tier in enumerate(detect_tiers(group)):
                for player in tier:
                    mapping[player["player_id"]] = (position, tier_index)
        return mapping

    # -- sync -------------------------------------------------------------

    def sync(self) -> int:
        """Re-poll ESPN. Returns the number of new picks seen."""
        if self.offline:
            return 0
        assert self.client is not None
        before = len(self.picks)
        state = self.client.draft_state()
        self.picks = state["picks"]
        self.last_sync = time.time()
        if self.team_id is None:
            self.team_id = self._infer_team_id()
        return len(self.picks) - before

    def _infer_team_id(self) -> int | None:
        """Identify our team from the pick sequence, if we have picked."""
        for pick in self.picks:
            if pick.get("overallPickNumber") in OUR_PICKS:
                return pick.get("teamId")
        return None

    def record_manual_pick(self, name: str, ours: bool = False) -> dict | None:
        """Offline mode: mark a player drafted by name.

        ``ours`` routes the pick onto our own roster so a mock session
        exercises the same gap analysis the live draft will.
        """
        player = self.resolve(name)
        if not player:
            return None
        self.picks.append(
            {
                "playerId": player["player_id"],
                "teamId": OFFLINE_TEAM_ID if ours else -1,
                "overallPickNumber": len(self.picks) + 1,
                "roundId": len(self.picks) // SHAPE.team_count + 1,
            }
        )
        return player

    def resolve(self, query: str) -> dict | None:
        query = query.strip().lower()
        if query in self.by_name:
            return self.by_name[query]
        matches = [p for name, p in self.by_name.items() if query in name]
        return matches[0] if len(matches) == 1 else None

    # -- derived ----------------------------------------------------------

    @property
    def drafted_ids(self) -> set[int]:
        return {p["playerId"] for p in self.picks if p.get("playerId")}

    @property
    def pick_count(self) -> int:
        return len(self.picks)

    def available(self, position: str | None = None) -> list[dict]:
        taken = self.drafted_ids
        pool = [p for p in self.players.values() if p["player_id"] not in taken]
        if position:
            pool = [p for p in pool if p["position"] == position]
        pool.sort(key=lambda p: -p.get("vor", 0))
        return pool

    def our_roster(self) -> list[dict]:
        if self.team_id is None:
            return []
        return [
            self.players[p["playerId"]]
            for p in self.picks
            if p.get("teamId") == self.team_id and p.get("playerId") in self.players
        ]

    def next_pick(self) -> int | None:
        """The next overall pick number that belongs to us."""
        for pick in OUR_PICKS:
            if pick > self.pick_count:
                return pick
        return None

    def picks_until_our_turn(self) -> int | None:
        nxt = self.next_pick()
        return None if nxt is None else nxt - self.pick_count - 1

    def gap_after_next(self) -> int | None:
        nxt = self.next_pick()
        if nxt is None or nxt not in OUR_PICKS:
            return None
        index = OUR_PICKS.index(nxt)
        return OUR_GAPS[index] if index < len(OUR_GAPS) else None


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def roster_gaps(roster: list[dict]) -> dict[str, int]:
    """Starting slots still unfilled, flex-aware.

    Fills dedicated slots first, then lets the surplus cascade into the flex.
    A team with 3 RBs and 1 WR has an open WR slot, not an open flex — the
    third back is the flex, and reporting otherwise sends you chasing the
    wrong position.
    """
    counts: dict[str, int] = {}
    for player in roster:
        counts[player["position"]] = counts.get(player["position"], 0) + 1

    gaps: dict[str, int] = {}
    surplus = 0
    for position, needed in SHAPE.starters.items():
        if position == "FLEX":
            continue
        have = counts.get(position, 0)
        if have < needed:
            gaps[position] = needed - have
        elif position in SHAPE.flex_eligible:
            surplus += have - needed

    flex_needed = SHAPE.starters.get("FLEX", 0)
    if surplus < flex_needed:
        gaps["FLEX"] = flex_needed - surplus
    return gaps


def detect_runs(state: DraftState, window: int = 12) -> dict[str, int]:
    """Count positions taken in the last ``window`` picks.

    A run is the market repricing a position in real time. Three backs in six
    picks means the room has decided backs are scarce, and the tier you were
    planning to enter next turn will not be there.
    """
    recent = state.picks[-window:]
    counts: dict[str, int] = {}
    for pick in recent:
        player = state.players.get(pick.get("playerId"))
        if player:
            counts[player["position"]] = counts.get(player["position"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def recommend(state: DraftState, limit: int = 8) -> list[dict]:
    """Rank available players for our next pick, with explicit reasoning."""
    roster = state.our_roster()
    gaps = roster_gaps(roster)
    gap_after = state.gap_after_next()
    long_drought = gap_after is not None and gap_after > 8
    runs = detect_runs(state)
    picks_made = len(roster)
    current_round = picks_made + 1

    # Which positions are still open in our lineup?
    open_positions: set[str] = set()
    for position in gaps:
        if position == "FLEX":
            open_positions.update(SHAPE.flex_eligible)
        else:
            open_positions.add(position)

    # How many of each position remain in the *current* tier?
    tier_remaining: dict[tuple[str, int], int] = {}
    for player in state.available():
        key = state.tiers.get(player["player_id"])
        if key:
            tier_remaining[key] = tier_remaining.get(key, 0) + 1

    scored: list[dict] = []
    for player in state.available():
        position = player["position"]
        score = float(player.get("vor", 0))
        reasons: list[str] = []

        # Kickers and defenses are noise until the last two rounds. Suppressing
        # them is not a ranking opinion; it is the difference between a useful
        # list and a list with a kicker on it in round 6.
        if position in ("K", "D/ST") and current_round < SHAPE.rounds - 1:
            continue

        if position in open_positions:
            score += 12.0
            reasons.append(f"fills open {position}")
        elif position in SHAPE.flex_eligible and "FLEX" in gaps:
            score += 5.0
            reasons.append("flex-eligible")
        else:
            score -= 6.0
            reasons.append(f"{position} already covered")

        key = state.tiers.get(player["player_id"])
        if key:
            remaining = tier_remaining.get(key, 0)
            if remaining <= 2 and long_drought and position in open_positions:
                score += 15.0
                reasons.append(
                    f"LAST {remaining} in tier — {gap_after} picks before we're back"
                )
            elif remaining <= 2:
                score += 6.0
                reasons.append(f"last {remaining} in tier")

        if runs.get(position, 0) >= 3:
            score += 7.0
            reasons.append(f"run on {position} ({runs[position]} of last 12)")

        if position == "QB" and SCORING.qb_is_compressed:
            qb_pool = len([p for p in state.available("QB")])
            if current_round < 9 and qb_pool > SHAPE.team_count:
                score -= 25.0
                reasons.append(f"QB compressed — {qb_pool} startable left, wait")

        if player.get("injured"):
            score -= 8.0
            reasons.append(f"injury: {player.get('injury_status')}")

        scored.append({**player, "score": round(score, 1), "reasons": reasons})

    scored.sort(key=lambda p: -p["score"])
    return scored[:limit]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_header(state: DraftState) -> None:
    nxt = state.next_pick()
    until = state.picks_until_our_turn()
    gap_after = state.gap_after_next()

    print(f"\n{BAR}")
    if nxt is None:
        print(f"  Draft complete for our slot.  {state.pick_count} picks made league-wide.")
    else:
        rnd = OUR_PICKS.index(nxt) + 1
        turn = "SHORT TURN" if (gap_after or 0) <= 8 else "LONG DROUGHT"
        on_clock = "  >>> WE ARE ON THE CLOCK <<<" if until == 0 else ""
        print(f"  Pick {state.pick_count} made  |  our next: #{nxt} (round {rnd})  "
              f"|  {until} away{on_clock}")
        if gap_after is not None:
            print(f"  After that pick: {gap_after} players come off the board "
                  f"before we choose again  [{turn}]")
    print(BAR)


def render_recommendations(state: DraftState, rows: list[dict]) -> None:
    gaps = roster_gaps(state.our_roster())
    print(f"\n  Open starting slots: "
          f"{', '.join(f'{k} x{v}' for k, v in gaps.items()) or 'none — bench value now'}")
    print(f"\n  {'#':>2}  {'PLAYER':<24} {'POS':<6} {'TM':<4} {'PROJ':>6} {'VOR':>6} {'SCORE':>6}")
    print(f"  {THIN}")
    for index, player in enumerate(rows, start=1):
        print(
            f"  {index:>2}  {player['name'][:24]:<24} "
            f"{player['position'] + str(player.get('position_rank', '')):<6} "
            f"{player['pro_team']:<4} {player['projected_points']:>6.1f} "
            f"{player.get('vor', 0):>6.1f} {player['score']:>6.1f}"
        )
        if player["reasons"]:
            print(f"      {' | '.join(player['reasons'])}")
    print()


def render_roster(state: DraftState) -> None:
    roster = state.our_roster()
    if state.team_id is None:
        print("\n  Our team id is not known yet. Pass --team-id, set TEAM_ID in .env,")
        print("  or wait until our first pick is made and it will be inferred.\n")
        return
    print(f"\n  OUR ROSTER (team {state.team_id}) — {len(roster)} players")
    print(f"  {THIN}")
    if not roster:
        print("  (empty)")
    for player in sorted(roster, key=lambda p: p["position"]):
        print(f"  {player['position']:<6} {player['name'][:26]:<26} "
              f"{player['pro_team']:<4} proj {player['projected_points']:>6.1f}")
    gaps = roster_gaps(roster)
    print(f"\n  Open starting slots: "
          f"{', '.join(f'{k} x{v}' for k, v in gaps.items()) or 'none'}")
    print(f"  Bench slots to fill: {SHAPE.bench_size}\n")


def render_taken(state: DraftState, count: int) -> None:
    recent = state.picks[-count:]
    print(f"\n  LAST {len(recent)} PICKS")
    print(f"  {THIN}")
    for pick in recent:
        player = state.players.get(pick.get("playerId"))
        label = (
            f"{player['name']} ({player['position']}, {player['pro_team']})"
            if player
            else f"player {pick.get('playerId')}"
        )
        mine = "  <- us" if pick.get("teamId") == state.team_id else ""
        print(f"  #{pick.get('overallPickNumber'):<4} R{pick.get('roundId'):<3} "
              f"team {pick.get('teamId'):<4} {label}{mine}")
    print()


def render_gaps(state: DraftState) -> None:
    print(f"\n  OUR REMAINING PICKS  (slot {SHAPE.draft_slot} of {SHAPE.team_count})")
    print(f"  {THIN}")
    for index, pick in enumerate(OUR_PICKS):
        if pick <= state.pick_count:
            continue
        gap = OUR_GAPS[index] if index < len(OUR_GAPS) else None
        tag = "" if gap is None else (
            "  SHORT TURN" if gap <= 8 else "  LONG DROUGHT — tiers will empty"
        )
        gap_text = "-" if gap is None else str(gap)
        print(f"  R{index + 1:<3} overall #{pick:<5} then {gap_text:>3} off the board{tag}")
    print()


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------

HELP = """
  rec [n]      ranked recommendations for our next pick (default 8)
  board [n]    best available overall, ignoring roster need
  pos <POS>    best available at one position (QB RB WR TE K D/ST)
  roster       our roster and open starting slots
  taken [n]    most recent picks league-wide
  runs         positional run detection over the last 12 picks
  gaps         our remaining picks and the drought structure
  drafted <name>   (offline) mark a player taken by another team
  mine <name>      (offline) mark a player drafted onto our roster
  sync         force a re-poll
  help         this list
  quit         exit
"""


def repl(state: DraftState) -> int:
    print(f"\n{BAR}")
    print("  FANTASY FOOTBALL INTELLIGENCE ENGINE — WAR ROOM")
    print(f"  {SHAPE.team_count}-team | full PPR | slot {SHAPE.draft_slot} | "
          f"{SHAPE.rounds} rounds"
          + ("  [OFFLINE MOCK MODE]" if state.offline else ""))
    print(f"  {len(state.players)} players indexed.  Type 'help' for commands.")
    print(BAR)

    while True:
        try:
            raw = input("\n  war-room> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  exiting.\n")
            return 0

        if not raw:
            continue
        parts = raw.split()
        command, args = parts[0].lower(), parts[1:]

        if command in ("quit", "exit", "q"):
            print("  exiting.\n")
            return 0

        if command in ("help", "?", "h"):
            print(HELP)
            continue

        if command in ("drafted", "mine"):
            if not state.offline:
                print("  'drafted'/'mine' are only for --offline mock mode; "
                      "live picks come from ESPN.")
                continue
            player = state.record_manual_pick(" ".join(args), ours=(command == "mine"))
            if not player:
                print("  no unambiguous match for that name.")
            else:
                where = "OUR roster" if command == "mine" else "another team"
                print(f"  pick #{state.pick_count}: {player['name']} -> {where}")
            continue

        # Every other command re-polls first. This is the whole point: the
        # answer must reflect the board as it is at the moment you ask.
        try:
            new = state.sync()
        except ESPNAuthError as exc:
            print(f"\n  {exc}\n")
            continue
        except Exception as exc:
            print(f"  poll failed ({exc}) — showing last known board.")
            new = 0

        if new:
            print(f"  (+{new} new pick{'s' if new != 1 else ''} since last poll)")

        if command == "sync":
            render_header(state)
        elif command in ("rec", "r"):
            render_header(state)
            limit = int(args[0]) if args and args[0].isdigit() else 8
            render_recommendations(state, recommend(state, limit))
        elif command in ("board", "b"):
            limit = int(args[0]) if args and args[0].isdigit() else 15
            print(f"\n  BEST AVAILABLE (by VOR, need-agnostic)\n  {THIN}")
            for index, player in enumerate(state.available()[:limit], start=1):
                print(f"  {index:>2}  {player['name'][:26]:<26} "
                      f"{player['position'] + str(player['position_rank']):<6} "
                      f"{player['pro_team']:<4} proj {player['projected_points']:>6.1f} "
                      f"vor {player.get('vor', 0):>6.1f}")
            print()
        elif command in ("pos", "p"):
            if not args:
                print("  usage: pos <QB|RB|WR|TE|K|D/ST>")
                continue
            position = args[0].upper()
            pool = state.available(position)
            print(f"\n  BEST AVAILABLE {position}  ({len(pool)} left)\n  {THIN}")
            for index, player in enumerate(pool[:15], start=1):
                print(f"  {index:>2}  {player['name'][:26]:<26} {player['pro_team']:<4} "
                      f"proj {player['projected_points']:>6.1f} "
                      f"vor {player.get('vor', 0):>6.1f}")
            print()
        elif command == "roster":
            render_roster(state)
        elif command == "taken":
            render_taken(state, int(args[0]) if args and args[0].isdigit() else 12)
        elif command == "runs":
            runs = detect_runs(state)
            print(f"\n  LAST 12 PICKS BY POSITION\n  {THIN}")
            for position, count in runs.items():
                flag = "   <- RUN" if count >= 3 else ""
                print(f"  {position:<6} {count}{flag}")
            print()
        elif command == "gaps":
            render_gaps(state)
        else:
            print(f"  unknown command: {command}   (try 'help')")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", type=int, help="our ESPN team id")
    parser.add_argument("--offline", action="store_true",
                        help="mock mode: no ESPN polling, picks entered by hand")
    args = parser.parse_args()

    if not POOL_PATH.exists():
        print(f"\n  {POOL_PATH} not found.\n  Run:  python pull_pool.py\n",
              file=sys.stderr)
        return 2
    board = json.loads(POOL_PATH.read_text())

    team_id = args.team_id
    if team_id is None and not args.offline:
        try:
            team_id = load_credentials().team_id
        except ConfigError:
            team_id = None

    sys.path.insert(0, str(Path(__file__).parent))

    try:
        state = DraftState(board, team_id, offline=args.offline)
        if not args.offline:
            state.sync()
    except (ConfigError, ESPNAuthError) as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    return repl(state)


if __name__ == "__main__":
    raise SystemExit(main())
