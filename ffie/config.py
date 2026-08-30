"""Configuration surface for the Fantasy Football Intelligence Engine.

Everything the engine needs to know that is *not* discoverable from the ESPN
API lives here. Anything that IS discoverable (roster slots, scoring rules,
team count) is pulled live by ``get_settings.py`` and treated as the source of
truth — the constants below are the *expected* values we validate against, so
a silent league-settings change surfaces as a loud mismatch instead of a bad
draft board.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
ENV_PATH = PROJECT_ROOT / ".env"


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Credentials:
    """ESPN session credentials, sourced from the local ``.env``."""

    league_id: int
    season: int
    swid: str
    espn_s2: str
    team_id: int | None = None

    @property
    def cookies(self) -> dict[str, str]:
        return {"SWID": self.swid, "espn_s2": self.espn_s2}


def load_credentials(required: bool = True) -> Credentials:
    """Read credentials from ``.env`` (or the ambient environment).

    ``.env`` is gitignored and never leaves the machine. The Playwright auth
    bridge (``auth_bridge.py``) is what populates SWID/espn_s2; this function
    only consumes them.
    """
    load_dotenv(ENV_PATH)

    league_id = os.getenv("LEAGUE_ID", "").strip()
    season = os.getenv("SEASON", "").strip()
    swid = os.getenv("SWID", "").strip()
    espn_s2 = os.getenv("ESPN_S2", "").strip()
    team_id = os.getenv("TEAM_ID", "").strip()

    if required:
        missing = [
            name
            for name, value in (
                ("LEAGUE_ID", league_id),
                ("SEASON", season),
                ("SWID", swid),
                ("ESPN_S2", espn_s2),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + f"\nExpected them in {ENV_PATH}."
                + "\nRun `python auth_bridge.py` to capture SWID/espn_s2, "
                "and copy .env.example to .env for the rest."
            )

    # ESPN is tolerant of the SWID braces being present or absent depending on
    # the endpoint; normalising to the braced form is what the cookie jar uses.
    if swid and not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"

    return Credentials(
        league_id=int(league_id) if league_id else 0,
        season=int(season) if season else 0,
        swid=swid,
        espn_s2=espn_s2,
        team_id=int(team_id) if team_id else None,
    )


# --------------------------------------------------------------------------
# League mathematics
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringModel:
    """The league's scoring rules, expressed as the coefficients that actually
    move player value.

    Full PPR with 4-point passing touchdowns and a -2 turnover penalty is a
    specific economy, not a cosmetic setting:

    * ``reception = 1.0`` inflates target-volume receivers and pass-catching
      backs far above their standard-scoring rank. A 90-catch WR2 gains 90
      points of floor that a 90-catch-equivalent rushing profile never sees.
    * ``pass_td = 4.0`` (vs. 6.0) compresses the quarterback position. The gap
      between QB1 and QB12 shrinks to roughly the width of a single starting
      flex decision, which is the entire justification for the late-QB posture
      encoded in ``draft_plan.py``.
    * ``interception = -2.0`` and ``fumble_lost = -2.0`` punish high-volume,
      low-efficiency passers, further flattening the quarterback curve and
      raising the relative value of a clean-usage running back.
    """

    reception: float = 1.0
    receiving_yard: float = 0.1
    receiving_td: float = 6.0
    rushing_yard: float = 0.1
    rushing_td: float = 6.0
    passing_yard: float = 0.04
    passing_td: float = 4.0
    interception: float = -2.0
    fumble_lost: float = -2.0

    @property
    def is_full_ppr(self) -> bool:
        return self.reception == 1.0

    @property
    def qb_is_compressed(self) -> bool:
        """4-point passing TDs plus turnover penalties = flat QB curve."""
        return self.passing_td <= 4.0 and self.interception <= -2.0


@dataclass(frozen=True)
class LeagueShape:
    """Structural constraints of the league."""

    team_count: int = 12
    draft_slot: int = 9
    roster_size: int = 16
    starters: dict[str, int] = field(
        default_factory=lambda: {
            "QB": 1,
            "RB": 2,
            "WR": 2,
            "TE": 1,
            "FLEX": 1,
            "D/ST": 1,
            "K": 1,
        }
    )
    flex_eligible: tuple[str, ...] = ("RB", "WR", "TE")

    @property
    def bench_size(self) -> int:
        return self.roster_size - sum(self.starters.values())

    @property
    def rounds(self) -> int:
        return self.roster_size

    def starters_at(self, position: str) -> int:
        return self.starters.get(position, 0)

    def league_wide_demand(self, position: str) -> int:
        """How many of a position the league must start every week.

        This is the scarcity denominator. With 12 teams starting 2 RB plus a
        flex that is usually an RB, real RB demand sits near 36 — which is why
        the RB pool is exhausted long before the WR pool despite WRs scoring
        more points in full PPR.
        """
        base = self.starters_at(position) * self.team_count
        if position in self.flex_eligible:
            # Empirically the flex resolves RB/WR ~45/45/10 vs TE in full PPR.
            flex_share = {"RB": 0.45, "WR": 0.45, "TE": 0.10}[position]
            base += round(self.starters_at("FLEX") * self.team_count * flex_share)
        return base


def snake_picks(draft_slot: int, team_count: int, rounds: int) -> list[int]:
    """Overall pick numbers for a given slot in a snake draft.

    For slot 9 in a 12-team, 16-round draft this yields:
        9, 16, 33, 40, 57, 64, 81, 88, 105, 112, 129, 136, 153, 160, 177, 184
    """
    picks: list[int] = []
    for rnd in range(1, rounds + 1):
        position = draft_slot if rnd % 2 == 1 else (team_count - draft_slot + 1)
        picks.append((rnd - 1) * team_count + position)
    return picks


def pick_gaps(picks: list[int]) -> list[int]:
    """Players taken between consecutive picks of ours.

    Slot 9 alternates a *short* turn (6 players off the board between picks 9
    and 16) with a *long* drought (16 players between 16 and 33). Every
    recommendation in the war room is a function of which side of that
    alternation the next pick sits on: on a short turn we can let a tier ride,
    on a long drought we must take the last member of any tier we still need.
    """
    return [b - a - 1 for a, b in zip(picks, picks[1:])]


# Canonical expected configuration for this league.
SCORING = ScoringModel()
SHAPE = LeagueShape()
OUR_PICKS = snake_picks(SHAPE.draft_slot, SHAPE.team_count, SHAPE.rounds)
OUR_GAPS = pick_gaps(OUR_PICKS)

DRAFTABLE_POSITIONS = ("QB", "RB", "WR", "TE", "D/ST", "K")
