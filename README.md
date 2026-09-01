# Underwriter v1

A desktop draft-intelligence application for ESPN fantasy football. It
authenticates against ESPN's private v3 API using a browser-captured session,
indexes the full draftable player universe against the league's own scoring
economy, and drives an on-the-clock advisor that re-queries the live draft board
on demand.

Calibrated for a **12-team head-to-head full-PPR league, drafting from slot 9**.

Previously the *Fantasy Football Intelligence Engine*, a terminal war room.
v1 keeps that engine intact and puts a native macOS window in front of it, so
the people who need it most no longer need a Python environment to use it.

---

## Executive summary

Draft software usually fails for one of two reasons. Either it ranks players
against a generic scoring system that is not the one you play in, or it hands
you a static cheat sheet that is stale by the fourth round. This engine is built
to avoid both.

Three things make it different from a ranked list:

**It reads your league, not a league.** Projections come from ESPN already
denominated in your scoring rules, and the protocol check validates the live
rulebook against the model the draft logic is calibrated on. If someone changes
the reception value or the roster shape, you find out at the top of the session
instead of discovering it in round eight.

**It prices scarcity, not points.** A raw projection ranking puts quarterbacks
at the top and is actively misleading in this format. Every player is scored by
value over replacement — points above the last startable player at that
position, where "startable" is derived from the league's real weekly demand.
That is the only number comparable across positions.

**It knows the shape of your turn.** Slot 9 in a 12-team snake does not draft on
an even rhythm. It alternates between a six-pick turn and a sixteen-pick
drought, and that alternation, not any ranking, determines whether you can let a
tier ride or have to enter it now.

---

## Architecture

v1 is a `pywebview` desktop shell over the existing Python core. The UI is
HTML rendered in a native macOS WebKit window; every button calls a Python
method directly through pywebview's `js_api` bridge. There is no local web
server, no REST layer, and no second runtime to ship.

```
                    ┌──────────────────────────────────────────┐
                    │  Underwriter.app                         │
                    │  native macOS window (WebKit)            │
                    │  950x850, HTML UI                        │
                    └────────────────────┬─────────────────────┘
                                         │  window.pywebview.api.*
                                         │  (direct in-process call)
                    ┌────────────────────v─────────────────────┐
                    │  DesktopBridge          main.py          │
                    │  trigger_login          save_league       │
                    │  get_league_protocol    predraft_board    │
                    │  get_live_draft_advice                    │
                    └───┬──────────────┬───────────────┬────────┘
                        │              │               │
          ┌─────────────v───┐   ┌──────v──────┐  ┌─────v──────────────┐
          │  playwright     │   │  pandas     │  │  ffie/config.py    │
          │  Chrome session │   │  pool frame │  │  scoring model,    │
          │  capture        │   │  VOR pass   │  │  snake-slot math   │
          └─────────────┬───┘   └──────┬──────┘  └─────┬──────────────┘
                        │              │               │
                        └──────────────┼───────────────┘
                                       v
                    ┌──────────────────────────────────────────┐
                    │  ffie/espn_client.py                     │
                    │  authenticated I/O, both transports      │
                    └───────┬──────────────────────────┬───────┘
                            │                          │
              espn-api wrapper                 raw v3 JSON transport
              (settings, rosters,              (mDraftDetail hot path,
               free agents)                     kona_player_info universe)
```

The original command-line tools remain in the repository and still work. They
are a parallel implementation, not a backend the app shells out to — the desktop
app calls the same client and config layers directly, in process.

### Why two transport layers

`ffie/espn_client.py` exposes both an `espn-api` `League` object and a raw
`requests` client, deliberately.

The wrapper is well-tested and ergonomic, and it handles settings, rosters, and
the free-agent pool. But it re-fetches the whole league on refresh, which is the
wrong shape for a hot path polled once per query on draft night.

So the live draft board goes through `RawClient.draft_state()`, which requests
exactly one view (`mDraftDetail`) with no filter. It is the narrowest request
the API accepts and it returns fast even while ESPN is under peak load.

There is a second, more important reason. During a live draft ESPN's free-agent
endpoint lags the board by a pick or two — precisely when that lag is most
expensive. The advisor therefore never asks "who is available." It caches the
full player universe once and derives availability by subtracting drafted player
ids from it. Set arithmetic on a local dict cannot lag.

---

## End-user macOS installation

No terminal, no Python, no dependencies. The app is self-contained.

1. Download `Underwriter.zip` and double-click it to extract.
2. Drag **Underwriter.app** into your **Applications** folder.
3. **Right-click** the app and choose **Open**. Confirm **Open** in the dialog
   that appears.

Step 3 matters and only applies the first time. The build is unsigned and not
notarized, so Gatekeeper blocks a normal double-click with a message saying the
developer cannot be verified. Right-click → Open is the supported way to grant
an exception. After that it launches normally like any other app.

### Using it

The window has five controls, in the order you use them:

| Control | What it does |
|---|---|
| **Connect ESPN Account** | Opens a real Chrome window at ESPN Fantasy. Log in by hand, then close the window. Your session cookies are captured and stored locally. |
| **Sync League** | Paste your league URL or ID. Confirms the league and lists the teams and owners. |
| **1. Protocol** | Decodes the live rulebook — roster slots, scoring coefficients — and validates it against the calibrated model. Run this first; everything downstream depends on it. |
| **2. Pre-Draft Board** | Indexes the draftable universe, computes replacement levels and value over replacement, and returns the board. |
| **3. On-The-Clock Advisor** | Re-polls the live draft and returns ranked recommendations for your next pick, with the reasoning printed alongside each one. |

Credentials are stored at
`~/Library/Application Support/Underwriter/.env`, written mode `600`. Nothing
leaves your machine except requests to ESPN.

---

## Developer setup and PyInstaller build

### Running from source

```bash
git clone https://github.com/jonesmarquise31/fantasy-football-intelligence-engine.git
cd fantasy-football-intelligence-engine

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python main.py
```

Running from source keeps `.env` beside the project rather than in Application
Support, which is what `.gitignore` already expects.

The original command-line tools still work and are useful while developing:

```bash
python auth_bridge.py       # log in once; writes SWID + ESPN_S2
python get_settings.py      # verify the connection and the rulebook
python pull_pool.py --csv   # build the board
python draft_plan.py        # build the round-by-round plan
python draft_assistant.py   # terminal war room
python draft_assistant.py --offline   # practise against a mock board
```

### Building the app

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name="Underwriter" main.py
```

The `--windowed` flag is what produces a real macOS bundle at
`dist/Underwriter.app` rather than a bare executable sitting next to an exposed
`_internal/` folder. `Underwriter.spec` is checked in and carries the same
configuration, including the `BUNDLE()` block that generates the `.app`; rebuild
from it with `pyinstaller --noconfirm --clean Underwriter.spec` to keep any spec
edits.

### Packaging for distribution

Compress the bundle itself, not the directory above it:

```bash
cd dist
zip -r -y Underwriter.zip Underwriter.app
```

The `-y` preserves symlinks, which macOS frameworks inside the bundle depend on;
without it the extracted app can fail to launch. Zipping `dist/` instead of
`Underwriter.app` produces an archive that extracts to a `dist` folder and gives
the user the `_internal` layout rather than something draggable, so check the
archive lists `Underwriter.app/` at its root before shipping it.

The result is unsigned. Distributing it beyond your own machine means every
recipient performs the right-click → Open step. Signing and notarizing with an
Apple Developer account removes that, and is the correct next step if this goes
to more than a handful of people.

---

## Authentication

ESPN's fantasy API has no public OAuth flow and no API keys. Access to a private
league is gated entirely on two browser cookies:

| Cookie | What it is |
|---|---|
| `SWID` | The account's global identifier, a braced UUID |
| `espn_s2` | A long-lived, URL-encoded session token, typically 300+ characters |

The only supported way to obtain them is to be logged in to ESPN in a real
browser. **Connect ESPN Account** automates that honestly:

1. Launches a genuine Chrome binary through Playwright (`channel="chrome"`).
2. Opens ESPN Fantasy and hands you the window.
3. **You** log in, by hand. Nothing types your password and nothing reads it.
4. Once both cookies appear in the jar, they are written to the local `.env` at
   mode `600`.

**On not reading your everyday Chrome profile.** Chrome holds an exclusive lock
on a running profile, and its cookie store is encrypted against an OS keychain
entry. Prying that open would be fragile and a poor security posture. A separate
browser session is the correct boundary, and the cost is a manual login.

### Security posture

- `.env` and `.chrome-profile/` are gitignored. `.env` is written `600`.
- Credentials are read into memory and sent to exactly one host,
  `lm-api-reads.fantasy.espn.com`. Nothing is transmitted anywhere else.
- Writing credentials merges into the existing `.env` rather than overwriting
  it, so keys the app does not manage survive a re-authentication.
- `espn_s2` is a **live session token for your ESPN account**. Treat a leak the
  way you would treat a leaked password: change your ESPN password, which
  invalidates it, then re-authenticate.
- A `401` from any path means the token expired. Re-run **Connect ESPN
  Account**. Do not hand-edit `.env`.

---

## The scoring economy

Full PPR with 4-point passing touchdowns and a -2 turnover penalty is a specific
economy, not a cosmetic setting. Three coefficients do most of the work:

**`reception = 1.0`** inflates target-volume receivers and pass-catching backs
far above their standard-scoring rank. A 90-catch receiver gains 90 points of
floor that an equivalent rushing profile never sees. This is what makes the wide
receiver pool stay productive so much deeper than the running back pool.

**`passing_td = 4.0`** compresses the quarterback position. Against the more
common 6-point setting, every passing score loses a third of its value, and
since passing touchdowns are most of what separates quarterbacks, the QB1-to-QB12
spread narrows to roughly the width of a single flex decision.

**`interception = -2.0`** and **`fumble_lost = -2.0`** punish high-volume,
low-efficiency passers, flattening the quarterback curve further and raising the
relative value of a clean-usage running back.

### Value over replacement

Replacement level is the projected total of the last player at a position the
league must start each week. Everything above that line is the only value a
draft pick can actually buy.

With 12 teams starting `QB / RB / RB / WR / WR / TE / FLEX / D-ST / K`, and a
flex that resolves roughly 45/45/10 across RB/WR/TE in full PPR, weekly demand
lands near:

| Position | Weekly league-wide demand |
|---|---|
| RB | ~29 |
| WR | ~29 |
| TE | ~13 |
| QB | 12 |

Demand for RB and WR is nearly identical. Supply is not. The receiver pool stays
productive far deeper than the back pool, so RB value decays faster — which is
the entire argument for hoarding backs early and taking receivers on the way
back down.

### Quarterback value compression

Because the QB curve is flat, a starting quarterback is a commodity in this
league. Every pick spent on one before the tier actually breaks is value burned.
The engine holds QB until the position's current tier stops having more bodies
than there are teams still needing a starter, and applies an explicit penalty to
early quarterbacks with the reason printed next to it:

```
QB compressed — 19 startable left, wait
```

---

## The pick-9 structure

Slot 9 of 12 produces this sequence, and this is the constraint the whole plan
is built around:

| | | | |
|---|---|---|---|
| R1 &nbsp; **9** | R5 &nbsp; **57** | R9 &nbsp; **105** | R13 &nbsp; **153** |
| R2 &nbsp; **16** | R6 &nbsp; **64** | R10 &nbsp; **112** | R14 &nbsp; **160** |
| R3 &nbsp; **33** | R7 &nbsp; **81** | R11 &nbsp; **129** | R15 &nbsp; **177** |
| R4 &nbsp; **40** | R8 &nbsp; **88** | R12 &nbsp; **136** | R16 &nbsp; **184** |

The gaps between our picks alternate: **6, 16, 6, 16, ...**

We select twice in quick succession — 9 and 16, then 33 and 40 — and then wait
through sixteen selections. Every recommendation is a function of which side of
that alternation the next pick sits on:

- **Short turn (6 off the board).** We can let a tier ride. Given two players we
  rate equally, take the scarcer position and expect the other to survive.
- **Long drought (16 off the board).** An entire tier will empty. Any tier we
  still need must be entered *now*, at its last member, because it will not
  exist when we are back up.

The advisor prints which regime the next pick is in, every time:

```
  Pick 15 made  |  our next: #16 (round 2)  |  0 away  >>> WE ARE ON THE CLOCK <<<
  After that pick: 16 players come off the board before we choose again  [LONG DROUGHT]
```

### Tiering

A tier is a set of players who are, for drafting purposes, the same player.
`draft_plan.detect_tiers()` finds the breaks statistically — where the
projection curve steps down more than it has been stepping — rather than at
arbitrary round numbers.

A minimum tier size is enforced, and it matters more than it looks. Without it a
noisy projection curve shatters into singleton tiers and every player becomes
"the last one in his tier." That converts the scarcity signal into constant
noise, which is worse than no signal at all: it justifies reaching for anybody.

---

## How a recommendation is scored

Base score is value over replacement. Four adjustments follow, and each one
prints its own reason so you can disagree with it:

| Adjustment | Why |
|---|---|
| `+12` fills an open starting slot | A bench player scores zero points for you |
| `+5` flex-eligible when flex is open | Partial credit toward a real slot |
| `-6` position already covered | Depth is worth less than a hole |
| `+15` last 1-2 in tier **and** a long drought follows | The tier will not exist when we are back up |
| `+6` last 1-2 in tier on a short turn | Real but not urgent |
| `+7` a run is underway at that position | The room is repricing the position live |
| `-25` early QB while the position is compressed | Value burned on a commodity |
| `-8` carrying an injury designation | Risk discount |

Kickers and defenses are suppressed entirely until the final two rounds. This is
not a ranking opinion — it is the difference between a useful list and a list
with a kicker on it in round six.

---

## Components

| File | Role |
|---|---|
| `main.py` | The desktop application. `DesktopBridge` is the `js_api` surface; the HTML UI is rendered into a native window at the bottom of the file. |
| `Underwriter.spec` | PyInstaller configuration, including the `BUNDLE()` block that produces `Underwriter.app`. |
| `ffie/config.py` | Scoring model, roster shape, snake-slot mathematics. |
| `ffie/espn_client.py` | Authenticated transport, both layers, plus player normalisation. |
| `auth_bridge.py` | Command-line session capture. Writes `SWID` / `espn_s2` to `.env`. |
| `get_settings.py` | Decodes the live rulebook and validates it against the calibrated model. |
| `pull_pool.py` | Indexes the draftable universe. Computes replacement levels and VOR. Writes `data/pool.json`. |
| `draft_plan.py` | Builds the round-by-round target matrix for slot 9: tiers, survival estimates, a directive per pick. |
| `draft_assistant.py` | The terminal war room. Re-polls ESPN on every prompt. |

### Terminal war-room commands

| Command | Effect |
|---|---|
| `rec [n]` | Ranked recommendations for our next pick, with reasoning |
| `board [n]` | Best available overall, need-agnostic |
| `pos <POS>` | Best available at one position |
| `roster` | Our roster and which starting slots are still open |
| `taken [n]` | Most recent picks league-wide |
| `runs` | Positional run detection over the last 12 picks |
| `gaps` | Our remaining picks and the drought structure |
| `sync` | Force a re-poll |
| `drafted <name>` / `mine <name>` | Offline mock mode only |
| `quit` | Exit |

Every command except `help`, `quit`, and the offline mock commands re-polls ESPN
before answering. That is the point: the answer must reflect the board as it is
at the moment you ask, not when the session started.

---

## Operational notes

**Poll cost.** The hot path is a single-view request with no filter. The player
universe is fetched once per session and cached. Polling once per query is well
within reasonable use of the endpoint; do not wrap the advisor in a tight loop.

**Projections are ESPN's.** This engine does not build its own projections. It
consumes ESPN's, which are already denominated in your league's scoring, and
adds the structural layer ESPN does not provide: replacement level, tiering,
turn-aware urgency, and roster-gap awareness. If you disagree with a projection,
you will disagree with the output — that is working as intended, and the reason
strings are there so you can see exactly why a player is ranked where they are.

**ESPN's API is private and undocumented.** It changes without notice. The
`espn-api` dependency is the layer that absorbs most of that; the raw client
touches only two views, both of which have been stable for several seasons. When
something breaks, the Protocol check is the fastest way to find out where.

**Survival estimates are triage, not forecast.**
`draft_plan.survival_estimate()` is a logistic on ESPN's PPR draft rank with a
one-round scale. Real drafts deviate from consensus by more than that. It orders
a queue; it never overrides an explicit tier break.

**The bundle is large.** `pandas` pulls in numpy and a BLAS library, which is
most of the shipped weight. That is the cost of doing the value-over-replacement
pass in a dataframe rather than by hand, and it is paid once at download.

**macOS only, for now.** The build target, the Gatekeeper instructions, and the
Application Support credential path are all macOS-specific. `pywebview` supports
Windows and Linux, but neither has been built or tested.

---

## License

MIT.
