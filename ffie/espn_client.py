"""Authenticated transport to the ESPN fantasy backend.

Two layers, deliberately:

1. ``connect()`` returns an ``espn_api.football.League`` — the ergonomic,
   well-tested wrapper. Used for settings, rosters, and the free-agent pool.

2. ``RawClient`` speaks directly to the ESPN v3 JSON endpoints with the same
   session cookies. Used for the live-draft hot path and the full player
   universe, where we need control over the ``x-fantasy-filter`` header and
   cannot afford the wrapper's full-league re-fetch on every poll.

Both layers authenticate with the identical ``SWID`` / ``espn_s2`` pair
captured by ``auth_bridge.py``. Nothing here ever writes credentials to disk
or transmits them anywhere except ``lm-api-reads.fantasy.espn.com``.
"""

from __future__ import annotations

import json
from typing import Any

import requests
from espn_api.football import League

from .config import Credentials, load_credentials

FANTASY_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# ESPN's slot identifiers, keyed by the integer the API actually returns.
SLOT_MAP: dict[int, str] = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE",
    21: "IR", 23: "FLEX", 24: "ER",
}

# ``defaultPositionId`` -> position. Distinct from slot ids above.
POSITION_BY_ID: dict[int, str] = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}

PRO_TEAM_BY_ID: dict[int, str] = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
    20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
    26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}


class ESPNAuthError(RuntimeError):
    """Raised when ESPN rejects the session cookies."""


def connect(creds: Credentials | None = None) -> League:
    """Build an authenticated ``League``.

    A 401 here means the ``espn_s2`` cookie has expired — ESPN rotates it
    roughly every 12 months, or immediately on password change. The fix is to
    re-run ``auth_bridge.py``, not to edit ``.env`` by hand.
    """
    creds = creds or load_credentials()
    try:
        return League(
            league_id=creds.league_id,
            year=creds.season,
            espn_s2=creds.espn_s2,
            swid=creds.swid,
        )
    except Exception as exc:  # espn-api raises bare Exceptions on auth failure
        message = str(exc)
        if "401" in message or "Unauthorized" in message or "private" in message.lower():
            raise ESPNAuthError(
                "ESPN rejected the session cookies (401).\n"
                "The espn_s2 cookie has most likely expired.\n"
                "Re-run:  python auth_bridge.py"
            ) from exc
        raise


class RawClient:
    """Thin, explicit client over the ESPN v3 JSON API."""

    def __init__(self, creds: Credentials | None = None, timeout: float = 12.0):
        self.creds = creds or load_credentials()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.cookies.update(self.creds.cookies)
        self.session.headers.update({"Accept": "application/json"})

    @property
    def league_endpoint(self) -> str:
        return (
            f"{FANTASY_BASE}/seasons/{self.creds.season}"
            f"/segments/0/leagues/{self.creds.league_id}"
        )

    def get(
        self,
        views: list[str],
        fantasy_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {}
        if fantasy_filter is not None:
            headers["x-fantasy-filter"] = json.dumps(fantasy_filter)

        response = self.session.get(
            self.league_endpoint,
            params={"view": views},
            headers=headers,
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise ESPNAuthError(
                f"ESPN returned {response.status_code} for views={views}. "
                "Session cookies are invalid or expired — re-run auth_bridge.py."
            )
        response.raise_for_status()
        return response.json()

    # -- Draft ------------------------------------------------------------

    def draft_picks(self) -> list[dict[str, Any]]:
        """Every pick made so far, newest last.

        This is the single most-polled call in the war room. It is deliberately
        the *narrowest* possible request — one view, no filter — so it returns
        in well under a second even mid-draft when ESPN is under load.
        """
        detail = self.get(["mDraftDetail"]).get("draftDetail", {})
        return detail.get("picks", []) or []

    def draft_state(self) -> dict[str, Any]:
        detail = self.get(["mDraftDetail"]).get("draftDetail", {})
        picks = detail.get("picks", []) or []
        return {
            "drafted": bool(detail.get("drafted")),
            "in_progress": bool(detail.get("inProgress")),
            "picks": picks,
            "pick_count": len(picks),
        }

    # -- Player universe --------------------------------------------------

    def player_universe(self, limit: int = 1000) -> list[dict[str, Any]]:
        """The full draftable player pool with season projections.

        Pulled once and cached to disk. During a live draft, players do not
        leave this set — they leave the *available* set, which we derive by
        subtracting drafted player ids. That distinction is what keeps the
        assistant correct when ESPN's own free-agent endpoint lags behind the
        draft board by a pick or two.
        """
        payload = self.get(
            ["kona_player_info"],
            fantasy_filter={
                "players": {
                    "limit": limit,
                    "sortDraftRanks": {
                        "sortPriority": 100,
                        "sortAsc": True,
                        "value": "PPR",
                    },
                }
            },
        )
        return payload.get("players", []) or []


def normalise_player(entry: dict[str, Any], season: int) -> dict[str, Any]:
    """Flatten one ``kona_player_info`` record into the engine's schema."""
    player = entry.get("player", {}) or {}
    player_id = entry.get("id") or player.get("id")

    projected = 0.0
    for stat in player.get("stats", []) or []:
        # statSourceId 1 == projection; statSplitTypeId 0 == full season.
        if (
            stat.get("seasonId") == season
            and stat.get("statSourceId") == 1
            and stat.get("statSplitTypeId") == 0
        ):
            projected = round(float(stat.get("appliedTotal", 0.0)), 2)
            break

    ranks = player.get("draftRanksByRankType", {}) or {}
    ppr_rank = (ranks.get("PPR") or {}).get("rank")

    return {
        "player_id": player_id,
        "name": player.get("fullName", ""),
        "position": POSITION_BY_ID.get(player.get("defaultPositionId"), "UNK"),
        "pro_team": PRO_TEAM_BY_ID.get(player.get("proTeamId"), "FA"),
        "projected_points": projected,
        "adp_rank": ppr_rank,
        "percent_owned": round(
            float((player.get("ownership") or {}).get("percentOwned", 0.0)), 1
        ),
        "injury_status": player.get("injuryStatus", "ACTIVE"),
        "injured": bool(player.get("injured", False)),
    }
