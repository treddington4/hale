# HALE — Execution Plan

Task-level breakdown of [ROADMAP.md](ROADMAP.md), written for an execution model
(Sonnet) to work through **in order**. This file is the single source of progress
truth — keep it updated as you go.

## Working rules

1. **Order**: work sections top-to-bottom; within a section, tasks top-to-bottom.
   A section is one coherent, shippable unit.
2. **Mark completion immediately**: flip `- [ ]` to `- [x]` the moment a task is done
   *and verified* — never in bulk at the end. If a task turns out wrong or unnecessary,
   don't silently skip it: strike it through and add a one-line reason.
3. **Commit after every section**: one commit per completed section, message =
   what changed + what was verified (follow the repo's existing commit-message style:
   rationale-rich body, no bullet spam). Do **not** push unless the user asks.
4. **Verify before marking done** — this repo has no test suite; the established
   discipline is:
   - Python: `python -c "import ast; ast.parse(open('<file>').read())"` pre-deploy
   - JS (legacy): `node --check app/static/app.js` · Frontend (new): `npm run build` must pass
   - Deploy: `docker compose up -d --build` on the host (host specifics live in the
     gitignored `.RUNBOOK.md`; if absent, ask the user rather than guessing)
   - API: `curl` against the live deployment; UI: `scripts/screenshot.py` and actually
     read the image
   - **Migrations: run against a copy of the live DB first, never the original**
5. **Docs**: at the end of each numbered phase (not each section), update `STATUS.md`
   and, when architecture-level facts changed, `CLAUDE.md`.
6. **Don't re-implement what exists**: GAP (Minetti) lives in `app/util.py` + a
   documented client copy; stats are computed once in `app/stats.py`; sync-time
   enrichment happens in `app/strava.py`/`app/garmin_sync.py`. Extending these is
   right; duplicating them is a defect.

---

## Phase 0 — Frontend re-architecture

Stack decision (made): Vite + React + TypeScript + Tailwind + shadcn/ui in a new
`web/` directory. FastAPI API contracts unchanged. Chart.js + Leaflet carry over.
This supersedes the "no build step" principle — deliberate, documented in ROADMAP.

### 0.1 Scaffold + design tokens
- [x] `web/`: Vite React-TS scaffold; Tailwind; shadcn/ui init; ESLint+Prettier —
      shadcn CLI init was skipped in favor of a hand-written `components.json` +
      `button.tsx`/`card.tsx` (avoided a second interactive install after two prior
      installs already raced each other on this machine's SMB-mounted working copy;
      see below); scaffold's default `oxlint` kept in place of ESLint (paired with
      Prettier) — same purpose, already wired by create-vite, not worth fighting
- [x] Vite dev proxy: `/api/*` and `/auth/*` → live backend URL (env var, not hardcoded)
- [x] Design tokens in Tailwind config: dark palette from current app (`#0B0E12` bg
      family, amber `#FFC857` accent), spacing/radius scale, Inter with
      `font-variant-numeric: tabular-nums` for stat values; JetBrains Mono kept
      as the wordmark/stat-value accent — ported 1:1 from `app/static/style.css`
      into `web/src/index.css` as shadcn-compatible CSS variables (dark-only, no
      light theme — matches the legacy app)
- [x] Shared API client (`web/src/lib/api.ts`) with typed responses for existing
      endpoints (start with the ones Home needs) — `dashboardSummary()` +
      `HeaderStats`/`DashboardSummary` types, matching `stats._header_stats`'s
      real field names exactly
- [x] Verify: `npm run dev` renders a token-styled placeholder against live API data —
      `npx tsc -b --noEmit` clean, `npx oxlint` clean (one expected fast-refresh
      warning on `button.tsx`, matches upstream shadcn), `npm run build` succeeds,
      screenshotted desktop+mobile against the live NAS backend via the dev proxy —
      HALE wordmark (white HAL + amber E) renders correctly, card shows real
      `headerStats` JSON (`totalActivityCount`, `runCountAllTime`, etc.) fetched
      through `/api/dashboard/summary`
- [x] Commit: "Phase 0.1: web/ scaffold, design tokens, API client"

  **Environment note for future sections**: on a network-mounted working copy
  (confirm with your platform's mount-info command — e.g. `net use` on
  Windows), avoid running `npm`/Vite directly against that mount: bulk
  `node_modules` operations are extremely slow and can fail outright
  (`ENOTEMPTY` on deletes; Vite's dev server can crash on startup with
  `Error: UNKNOWN: unknown error, watch`, since native `fs.watch()` isn't
  supported over network filesystems). The real fix isn't a slower-but-working
  code tweak — if your network mount points at a real machine you can reach
  (a NAS, a remote dev box), run `npm`/`vite` **there**, against the local
  path the mount resolves to, ideally inside a throwaway container pinned to
  a modern Node (this repo's target: `node:22-slim`, matching Phase 0.10's
  eventual Dockerfile stage) so the host's own Node version doesn't matter.
  Confirmed in this repo: the same `npm install` that took ~15min over the
  mount took ~9s run this way, and Vite's dev-server startup dropped from
  ~28s to ~480ms with native (non-polling) file-watching working correctly.
  `server.watch.usePolling` in `vite.config.ts` is kept only as a defensive
  fallback for whoever runs `npm run dev` directly over a network mount
  anyway — see the gitignored `.RUNBOOK.md` for this dev environment's exact
  commands.

### 0.2 App shell
- [x] Persistent left sidebar (desktop ≥900px) / bottom tab bar (mobile): Home, Goals,
      Activities, Insights, Map, Chat, Workouts, Settings — `min-[900px]:` arbitrary
      Tailwind breakpoint used for the exact 900px cutoff (`Shell.tsx`)
- [x] React Router routes per tab; HALE wordmark (white `HAL` + amber `E`) + tagline
      "HALE's Adaptive Life Engine"; race-countdown chip in the shell header —
      `RaceCountdown.tsx` reads `daysUntil` from `/api/goals`' already-computed
      `progress` field (`stats.goal_progress()`) rather than recomputing date math
      client-side like the legacy `renderRaceCountdown()` did
- [x] Loading skeleton components + empty-state component (icon, message, CTA) —
      reused by every tab port below — `components/ui/skeleton.tsx` +
      `components/ui/empty-state.tsx`; all 8 routes currently render
      `PlaceholderPage` (thin `EmptyState` wrapper) pointing at the PLAN.md section
      that ports them, swapped out route-by-route in 0.3–0.9
- [x] Verify: screenshot desktop + mobile viewports — `tsc -b --noEmit` and `oxlint`
      clean (same one expected `button.tsx` warning), `npm run build` succeeds,
      screenshotted Home/Goals/Workouts routes at both viewports via the NAS-hosted
      dev server (see `.RUNBOOK.md`): sidebar nav + active-route highlighting work
      correctly on desktop, bottom tab bar (all 8 icons, not cramped) + top header
      work correctly on mobile, race-countdown chip shows real data ("53 days to
      Wedding") in both layouts
- [x] Commit: "Phase 0.2: app shell — sidebar/bottom-tab nav, skeletons, empty states"

### 0.3 Home tab port
- [x] Stat strip (fast paint from `/api/dashboard/summary` headerStats, exact numbers
      after `/api/runs` — preserve the existing two-source pattern), goals cards,
      dashboard cards, wellness cards — added TanStack Query (`lib/queryClient.ts`)
      for shared/cached fetching (`hooks/useRuns.ts`, `useGoals.ts`,
      `useDashboardSummary.ts`, `useWellness.ts`); `lib/runs.ts` ports
      `mergeDuplicateRuns`/`isLikelyDuplicate`/`mergeRunPair`/`canonicalActivityType`
      1:1 from `app.js` (same client-side, never-in-storage duplicate merge — see
      CLAUDE.md); `RaceCountdown` refactored onto the shared `useGoals()` query
      instead of its own fetch, so the chip and Home's Goals section can't drift
- [x] Card component system (replaces settings-row-for-everything): `ChartCard` +
      `CardGrid` (`components/home/`) — title/value/sub-metric hierarchy, hover
      state + click-through navigation on `onClick` (`data-nav-tab`/`data-nav-run`
      from the legacy `wireNavCards()` become `navigate("/activities?filter=...")`
      / `navigate("/activities?run=...")` — query params 0.5's Activities port
      will read); `GoalCard` ports the race/consistency/distance_target dispatch
      from `goalCardBody()`
- [x] Verify: side-by-side screenshot vs legacy Home; all numbers identical —
      `tsc`/`oxlint`/`build` clean via the NAS-container workflow; screenshotted
      desktop + mobile against the live backend and confirmed byte-for-byte
      identical values against a fresh legacy-Home screenshot (This week 10.0 mi,
      avg pace 10:57/mi, 201 runs, breakdown line, both goal cards, all 7 dashboard
      cards incl. bar-fill widths/colors, all 3 wellness cards); confirmed the
      mobile bottom-tab-bar "gap" seen in a full-page capture is a `position:fixed`
      screenshot artifact, not a real overlap (re-verified with a scrolled
      viewport-only capture)
- [x] Commit: "Phase 0.3: Home tab ported"

### 0.4 Workouts + Recovery port
- [x] Unified date-ordered list (workouts + recovery sessions interleaved — preserve
      current behavior), structured-steps rendering with expandable how-to details,
      status actions, new-workout modal — added shadcn Dialog/Input/Label/Select/
      Textarea primitives (`@radix-ui/react-dialog`, `-label`, `-select`); TanStack
      Query mutations (`hooks/useWorkouts.ts`) invalidate the one list that actually
      changed rather than a full-tab re-render
- [x] Verify: screenshot; create/edit/delete round-trip against live API — caught and
      fixed a real bug during verification: `Workout.steps` is genuinely nullable
      (not just `[]`) for Garmin-suggested workouts with no structured steps, which
      the initial port typed as non-nullable and crashed `WorkoutCard` on
      (`Cannot read properties of null (reading 'length')` — legacy app.js's
      `w.steps && w.steps.length` guard had this right, the port initially didn't);
      fixed the type + render guard, re-verified clean. Confirmed real Garmin-
      suggested workouts, badges, multi-line notes, and the interleaved recovery
      session all render correctly; did a real create (via the actual form) →
      confirmed via `GET /api/workouts` → delete (via the actual UI) → confirmed
      gone via `GET /api/workouts` round-trip against the live backend, test data
      cleaned up after
- [x] Commit: "Phase 0.4: Workouts tab ported"

### 0.5 Activities (Runs) port
- [x] Run cards (badges, mini-stats, weather, dynamics rows), expand with splits/
      intervals/inline map, edit modal (activity-family-aware fields — preserve
      `isDistanceActivity` logic), filter bar (modes, type select, date nav) —
      `components/activities/`: `RunCard`, `SplitsTable`, `IntervalsTable`,
      `ExerciseSetsTable`, `MiniMap` (Leaflet, one instance per expanded card,
      cleans up on unmount/route-change), `EditRunDialog`, `FilterBar`; ported
      `mergeDuplicateRuns`'s partner logic (GAP/`gapSecPerMi`+`minettiCost` in
      `lib/gap.ts`, explicitly the documented client-side GAP duplicate per
      CLAUDE.md), route-gap splitting (`lib/route.ts`), HR-floor computation
      (`hooks/useHrFloor.ts`) faithfully; fixed one real legacy bug in passing —
      badge alpha colors were built by string-concatenating a hex suffix onto
      `TYPE_COLORS`, which silently produced invalid CSS for the `rgb(...)`
      entries (Interval/Long Run) — `lib/color.ts`'s `withAlpha()` parses either
      form properly instead of reproducing the same breakage
- [x] While here: filter-driven fetching — `/api/runs` gains `start`/`end`/`all`
      params (`main.py`); default load = last 90 days; `all=true` bypasses the
      window for callers needing true all-time totals (Home's exact stat-strip,
      `hooks/useRuns.ts`'s `useAllRuns()`) — wider Activities filters (6 Months/
      Year/All) fetch on demand via TanStack Query's per-key caching, no explicit
      pagination code needed; client merge/dedup logic (`lib/runs.ts`) unchanged
- [x] Verify: payload size before/after; screenshot; edit round-trip — deployed
      the backend change and confirmed via curl: default 148 runs vs `all=true`'s
      524, explicit `start`/`end` correctly bounded; payload 7.24MB → 2.85MB (61%)
      on the default view. Screenshotted the filter bar (all 8 modes, prev/next
      nav, custom range, type select) and an expanded run card against real data
      — numbers matched a direct API fetch exactly (splits, mini-stats, weather).
      Separately verified the two less-common expand paths against real runs:
      a strength session's `ExerciseSetsTable` (warmup badges, per-exercise set
      grouping) and an interval run's `IntervalsTable` + mini-map. Did a real
      edit (RPE + notes) via the actual dialog on a real run, confirmed via
      `GET /api/runs`, then reverted it back to its original `null`/`null` via a
      direct PATCH (this touches real synced data, unlike Workouts' disposable
      test rows, so the round-trip had to restore state exactly rather than
      delete). Console/pageerror-checked the route before screenshotting — no
      errors.
- [x] Commit: "Phase 0.5: Activities tab ported + windowed /api/runs fetching"

### 0.6 Insights port
- [x] All existing charts (temp-vs-pace/HR/cadence scatter, weekly mileage, pace/
      cadence/HR trend, 7-day rolling pace, cadence-vs-pace scatter, steps, resting
      HR, VO2max, sleep score/duration, sleep-stage hypnogram) ported — `chart.js`
      added as a real dependency (legacy had no build step so it loaded via CDN);
      `lib/chartTheme.ts`'s `applyChartTheme()` centralizes the palette + grid/tick
      defaults legacy hand-repeated per chart (called once in `main.tsx`, before
      any chart mounts anywhere — fixes a real legacy fragility where Chat's charts
      silently depended on Insights having rendered first to set `Chart.defaults`);
      `components/insights/ChartCanvas.tsx` is a small per-canvas Chart.js
      lifecycle wrapper (create/destroy via `useEffect` cleanup keyed on a
      `useMemo`'d config) — deliberately not the legacy global `charts` array +
      manual `destroyCharts()`, since React's unmount timing doesn't line up with
      that pattern's assumptions; `ChartPanel.tsx` ports the title/sub/canvas/
      empty-state card shell (`chartCardHTML`); `lib/sleepStages.ts` ports the
      hypnogram's EST-timezone tick/label helpers 1:1; reused the existing
      `FilterBar`/`useHrFloor`/`isPlausiblePace`/`isPlausibleHR` rather than
      duplicating; added `api.steps()`/`api.sleepStages()` + `useSteps`/
      `useSleepStages` hooks (previously unused by any ported tab). One
      Chart.js/TS typing gap hit and resolved: the sleep hypnogram's floating-bar-
      on-a-category-axis pattern (`x: [start,end], y: label`) isn't modeled by
      Chart.js's bundled bar-chart types (they expect `Point`/`BubbleDataPoint`) —
      narrowly cast just the `dataset.data` field rather than the whole config
      object, which preserves contextual typing (and real type-checking) for
      every sibling callback (tooltip formatters, `afterBuildTicks`)
- [x] Verify: screenshot vs legacy for chart parity — `tsc -b --noEmit` and
      `oxlint` clean (same one expected `button.tsx` fast-refresh warning as every
      prior phase), `npm run build` succeeds. Screenshotted against the live NAS
      backend at both the default 7-day range and a wider "Month" range: real
      data renders correctly in every panel — temp-effect scatters, weekly
      mileage bars, the dual-axis pace/cadence/HR trend line with its legend row,
      7-day rolling pace, cadence-vs-pace scatter, daily steps bars, resting HR,
      VO2max (stepped line), sleep score+duration dual-axis line, and the sleep
      hypnogram (correct per-stage colors, EST time-of-night axis, working
      night-picker showing "2026-07-20") — no broken canvases, no console/page
      errors. Confirmed chart cleanup works correctly by navigating Insights →
      Activities → Insights and re-checking for console errors (none) — this
      exercises the `ChartCanvas` unmount path the legacy app never had to handle
      (it only ever tore down charts on tab-*in*, never on tab-away)
- [x] Commit: "Phase 0.6: Insights tab ported"

### 0.7 Map port
- [x] Leaflet map, location select, metric modes (density/pace/HR/cadence/grade) —
      per-run mini-maps were already ported in 0.5 (`MiniMap.tsx`). New:
      `lib/mapClusters.ts` (greedy proximity clustering + centroid, ported from
      `clusterRuns`/`clusterCentroid`), `lib/mapHeat.ts` (gradients, `METRIC_CONFIG`,
      `heatColor`, `buildMetricSegments`), `api.geocode()` (wraps the existing
      `/api/geocode` reverse-geocoding endpoint — server-cached, rate-limited,
      unchanged), `MapPage.tsx`. Exported `haversineKm`/`computeGapThresholdKm`
      from `lib/route.ts` (previously internal) since the metric-segment builder
      needs the gap threshold directly, not just the pre-split polyline segments
      `splitRouteAtGaps` produces. Ported the dark-theme Leaflet chrome overrides
      (`.leaflet-control-zoom`/`.leaflet-control-attribution`/`.leaflet-container`)
      into `index.css`, which had been missed in 0.5 (MiniMap's `zoomControl:false`
      meant the gap was invisible until Map's full page showed a visible zoom
      control). One deliberate behavior change from legacy: the Leaflet map
      instance is created on mount / destroyed on unmount (a real `useEffect`
      lifecycle) rather than ported as legacy's module-level `if (!map)` singleton
      that persists across tab switches — legacy's approach only worked because
      the vanilla app never unmounts tab content (just toggles `display:none`);
      this component genuinely mounts/unmounts with route navigation, so
      create-on-mount/destroy-on-unmount is the correct mapping, not a shortcut
- [x] While here: found and fixed a real regression from Phase 0.5's backend
      change — `GET /api/runs` now defaults to a 90-day window, but the
      still-in-production legacy `app.js` was never updated to pass `all=true`,
      so the live app had silently lost access to run history older than 90 days
      (Map's clustering, Insights' all-time rolling-pace lookback, etc.). Fixed
      and deployed independently (commit `e6e8f60`), verified live via curl
      (148 windowed vs 524 all-time) and by confirming the deployed `app.js`
      actually contains the fix
- [x] Verify: screenshot each metric mode — `tsc -b --noEmit` and `oxlint` clean
      (same one expected `button.tsx` warning). Caught and fixed a real bug during
      first-pass verification: the map canvas rendered completely blank because
      the component's loading-state `Skeleton` early-return meant the map
      container `<div>` didn't exist in the DOM on first mount (when
      `useAllRuns()` data was still loading) — since the map-creation effect has
      an empty dependency array, it only runs once, found `containerRef.current`
      null, and never created the Leaflet map at all, even after data arrived and
      the component re-rendered with the container present. Fixed by removing the
      early return (the container now always renders; the "no items yet" empty
      state already degrades correctly during the brief loading window). After
      the fix, screenshotted all 5 modes (Density/Pace/Heart Rate/Cadence/Grade)
      against live data — correct tiles, dark zoom-control styling, geocoded
      location label ("Manchester, New Hampshire"), correct per-mode gradient
      colors and legend text (e.g. "Pace · 160 runs · blue 23:04/mi → red
      2:16/mi"), and "All locations" correctly zooming out to include a real
      travel run near the Dominican Republic. Confirmed the map's create/destroy
      lifecycle is clean by navigating Map → Insights → Map and re-checking for
      console errors (none) — the map correctly re-initializes and re-auto-selects
      the most-recent-activity cluster on remount
- [x] Commit: "Phase 0.7: Map tab ported"

### 0.8 Chat port
- [x] Thread UI, tool-call transparency chips, inline charts (`charts` payload),
      persona-aware empty state, send flow with optimistic pending bubble —
      `lib/chatMarkdown.ts` ports the legacy hand-rolled Markdown subset
      (`escapeHtml`/`inlineMd`/`splitTableRow`/`renderMarkdown`) line-for-line —
      escapes everything first, only ever injects tags it generates itself, so
      it's safe to render via `dangerouslySetInnerHTML` despite not being a real
      markdown library; `components/chat/ChatBubble.tsx` (bubble shell + tool
      trace + charts), `ChatChart.tsx` (own `useMemo`'d Chart.js config, reusing
      0.6's `ChartCanvas`/`chartTheme` — no manual `chatCharts` array/
      `destroyChatCharts()` needed, React's per-component effect cleanup gives
      Insights-vs-Chat chart isolation for free), `ChatInputBar.tsx` (owns its
      own input state so keystroke re-renders never touch the message list or
      any mounted chart instance — the React-idiomatic replacement for legacy's
      DOM-mutation approach, which never re-rendered the thread per keystroke
      either, just via a different mechanism). `pages/ChatPage.tsx` reuses
      `DashboardCards` exported from `HomePage.tsx` (matches legacy's
      `renderChatTab()` reusing the same `renderDashboardCards()` Home uses).
      New persona-aware empty state (`PERSONA_LABELS`, short UI glosses of
      `coach.py`'s `PERSONA_PROMPTS` tones, fetched via new `api.coachPersonality()`)
      — this is a deliberate addition beyond legacy parity per this section's own
      checklist, since legacy has no empty state at all (blank thread pane until
      the first message)
- [x] While here: `api.sendChatMessage()` never throws — returns a discriminated
      `ChatSendResult` (`{ok:true,...}` or `{ok:false, kind:"http"|"network", message}`)
      so the page can reproduce legacy's exact two different error strings
      (`Error: ${detail}` for a real HTTP error vs. the bare literal
      `"Network error — try again."` for a fetch failure) without a try/catch
      in the component
- [x] Verify: real message round-trip; history renders with charts — `tsc -b
      --noEmit` and `oxlint` clean (same one expected `button.tsx` warning),
      `npm run build` succeeds. Screenshotted against live data (14 real
      persisted messages, `insulting` persona active): collapse/expand toggle
      works ("Show earlier (12)" / "Hide earlier"), markdown renders correctly
      (bold, lists, dashes), tool-trace chip shows real bare tool names
      (`get_scheduled_workouts, get_run_summary, get_training_load_trend,
      get_health_history`). Caught and fixed a real bug during verification:
      user-message bubbles showed literal `&#39;` instead of an apostrophe —
      `ChatBubble` was calling `escapeHtml()` on plain JSX text children, but
      React already escapes text nodes itself; `escapeHtml` is only correct for
      the markdown branch, which builds a raw HTML string for
      `dangerouslySetInnerHTML`. Removed the double-escaping, re-verified with a
      screenshot showing the correct apostrophe. Did a real send round-trip
      (not a mock): typed a message, clicked Send, confirmed the `POST
      /api/chat/message` request/response over the network, and confirmed the
      real reply rendered in the active `insulting` persona's tone, input
      cleared and re-enabled correctly. Deliberately did **not** exercise the
      "Clear conversation" button against this live data — unlike Workouts'
      disposable test rows, resetting Chat destroys the entire real
      conversation history irreversibly with no undo, so this path was verified
      by code review (the handler is a two-line `resetChat()` + cache-clear)
      rather than a live click
- [x] Commit: "Phase 0.8: Chat tab ported"

### 0.9 Goals + Settings port
- [x] Goals CRUD + progress cards — `GoalCard.tsx` (already ported in 0.3) extended
      with optional `onEdit`/`onComplete`/`onAbandon`/`onDelete` props rendering the
      legacy action row (Home's usage is unaffected, passes none of them);
      `ChartCard` gained a generic `actions` slot to carry them. New
      `GoalFormDialog.tsx` (discriminated race/consistency/distance_target fields,
      activity-type checklist data-driven from real run history via `useAllRuns()`,
      same "only send the fields relevant to the current type" behavior as legacy
      — switching goal type on edit leaves old fields stale server-side but
      harmless, since `goal_progress()`'s dispatch is entirely keyed on
      `goal_type`), `GoalsPage.tsx` (Active/Completed/Abandoned sections),
      `useGoalMutations()` (create/update/delete, one shared `["goals"]`
      invalidation covering the Shell's race countdown and Home's goals section too)
- [x] Settings: connections, sync controls with live sync/backlog status panels,
      coach personality, Garmin import, About — `hooks/useSettings.ts` ports every
      remaining endpoint (`stravaStatus`, `garminStatus`, `syncMeta`, `connections`,
      `routeDiagnostics`, `config`) plus `useSyncStatus`/`useBacklogStatus`, which
      reproduce the poll-only-while-running discipline (see the flashing-loop bug
      history) via TanStack Query's `refetchInterval` callback — `(query) =>
      query.state.data?.status === "running" ? interval : false` — rather than
      porting the manual `setTimeout`/`stopBacklogPolling`/`checkBacklogOnce` state
      machine: a query with no active observers simply doesn't refetch, so there is
      no way to reintroduce the original unconditional-poll bug this pattern was
      written to fix. `manualSync`/`backlogSync`/`garminImport` in `api.ts` never
      throw (mirrors Chat's `sendChatMessage` convention from 0.8) so the UI can
      show the exact inline failure text a non-OK response or network error
      produces. `components/settings/SyncControls.tsx` is shared by both sources'
      Strava/Garmin sections
- [x] Verify: sync-now round-trip shows live status; screenshot — `tsc -b --noEmit`
      and `oxlint` clean (same one expected `button.tsx` warning), `npm run build`
      succeeds. Screenshotted Goals (active/completed cards with real countdown/
      progress data) and every Settings section against live data — status dots,
      last-synced/last-error text (including Garmin's real rate-limit cooldown
      message), route-source diagnostics, resting HR, steps, connections, coach
      personality, sync schedule, About. Did real round-trips against the live
      backend, not mocks: created a distance-target test goal through the actual
      dialog (confirmed via screenshot: "0 / 100 mi", "0% complete"), then deleted
      it through the UI and confirmed via `GET /api/goals` it's gone; clicked
      "Sync Now" for Strava and confirmed via network-request logging that the
      button correctly POSTs, the status panel shows "Syncing…"/"N runs synced so
      far…" while running, polling stops and the button/panel revert to idle once
      the job finishes; toggled the coach personality select (Insulting →
      Encouraging → back to Insulting) and confirmed the "Saved" flash and that
      `POST /api/coach/personality` actually fired each time
- [x] Commit: "Phase 0.9: Goals + Settings ported"

### 0.10 Cutover
- [x] Dockerfile → multi-stage: `node:22-slim` builds `web/dist` → copied into the
      python image; FastAPI serves `web/dist` at `/` (keep legacy at `/legacy` for
      one release) — `main.py`'s final route registration replaced the old bare
      `app.mount("/", StaticFiles(directory="static", html=True))` with: a
      `/legacy` mount for the old app unchanged, a plain `StaticFiles` mount at
      `/assets` for Vite's content-hashed bundle, and an explicit catch-all
      `@app.get("/{full_path:path}")` (`serve_web_app`) that serves a real file
      in `web-dist/` if one exists at that path, else falls through to
      `index.html` — needed because `StaticFiles(html=True)` only auto-serves
      `index.html` at a mount's own root, not for arbitrary unmatched sub-paths,
      so a hard reload on a React Router route like `/insights` would otherwise
      404 rather than letting client-side routing take over. Falls back to
      serving legacy at `/` if `web-dist/` doesn't exist (e.g. local dev running
      `main.py` directly against the Vite dev server on :5173 instead of a built
      image) so the app is never left with nothing at `/`
- [x] `scripts/screenshot.py`: updated tab navigation for the new shell — the old
      `navigateTo()` global JS function doesn't exist in the new frontend
      (React Router paths, not client-side tab-switching in one page); replaced
      with a `TAB_PATHS` map and a real `page.goto()` per tab, which is simpler
      than the old approach and works identically across every viewport
      regardless of which nav chrome (sidebar vs. icon-only bottom bar) is visible
- [ ] Delete `app/static/` legacy after one week of parity (separate commit) —
      deliberately not done yet; `/legacy` needs to stay reachable for the parity
      window described above before this is safe
- [x] Verify: full-container build + deploy; every tab screenshot; `STATUS.md` +
      `CLAUDE.md` updated (build step now exists; architecture section rewritten) —
      caught and fixed one real Dockerfile bug during the first build attempt
      (`chown ... /web-dist` referenced an absolute path that doesn't exist —
      the copy target was `/app/web-dist` given `WORKDIR /app`, already covered
      by the recursive `chown -R runlog:runlog /app`). After the fix: full
      `docker compose up -d --build` succeeded; curl-verified `/` serves the new
      React shell (real `<script src="/assets/...">` tags), `/legacy` serves the
      unchanged old app, a hard-reload-style request to `/insights` returns 200
      (SPA fallback working), `/api/config` still responds correctly, and
      `/assets/*` files serve with 200. Ran the full updated `scripts/screenshot.py`
      suite (all 8 tabs × desktop + mobile = 16 screenshots) against the live
      production deployment at `192.168.68.80:8000` (not the dev server) and
      read every one: Home/Goals/Activities/Insights/Map/Chat/Workouts/Settings
      all render real data correctly on both viewports, mobile bottom nav intact.
      Updated `CLAUDE.md`'s "What this is"/Commands/Architecture sections (new
      "Frontend" section describing the SPA-fallback serving approach, corrected
      the stale "no Node.js needed in the Dockerfile" claim, updated file-path
      references from `app/static/app.js` to their `web/src/` equivalents) and
      `STATUS.md` (new frontend-rewrite-complete status line, resolved the
      "no visual QA pass" backlog item, corrected the GAP-duplication note to
      mention all copies)
- [x] Commit: "Phase 0.10: cutover to built frontend"

### 0.11 PWA
- [x] Manifest (`web/vite.config.ts`'s `VitePWA({ manifest: {...} })`) — name
      "HALE — HALE's Adaptive Life Engine", short name "HALE", `#0b0e12`
      background/theme (matches `--hale-bg`), `standalone` display. Icon set
      generated via a one-off Pillow script (bold amber "E" on `--hale-bg`,
      matching the Wordmark's `HAL<span class="text-primary">E</span>`
      treatment): `pwa-192`/`pwa-512` (purpose `any`), `maskable-512` (extra
      padding so circular/squircle OS masks don't clip the glyph),
      `apple-touch-icon` (opaque, for iOS home screen) — all under
      `web/public/icons/`
- [x] Service worker (`web/src/sw.ts`, `strategies: 'injectManifest'` — the
      default `generateSW` can't inject a custom `push`/`notificationclick`
      listener, which this needs): precaches the app shell via
      `precacheAndRoute(self.__WB_MANIFEST)`, `NetworkFirst` runtime caching
      for `/api/**` (8s timeout, falls back to last-known-good when offline).
      `registerType: 'autoUpdate'` — no update-available prompt, matching a
      single-user self-hosted app's low-stakes update model.
      `web/tsconfig.app.json` excludes `sw.ts` from the app's `tsc -b` project
      (its `webworker` lib conflicts with the app's `DOM` lib) — vite-plugin-pwa
      builds it via its own separate esbuild pass regardless
- [x] Web push backend: `PushSubscription` table (`models.py`, brand-new — no
      `_MIGRATABLE_TABLES` entry needed, same as `ApiToken`); `app/push.py`
      (`is_configured()`/`subscribe()`/`unsubscribe()`/`send_push()`, degrades
      cleanly with no VAPID keypair set, same pattern as `assistant.py`'s
      Claude-credential check). `POST /api/push/subscribe`,
      `POST /api/push/unsubscribe`, unauthenticated `GET
      /api/push/vapid-public-key` (a public key by definition — same non-secret
      status as an OAuth client id), `POST /api/push/test` (sends a real
      notification to every device the current user has subscribed — the one
      concrete verification hook, independent of the two triggers this
      checklist names below, since neither exists as a feature yet).
      `GET /api/config` gained `pushConfigured` so the frontend can hide the
      whole Settings section cleanly when unconfigured. VAPID keypair
      generated once via `py-vapid` or `pywebpush==2.3.0`, stored in `.env`
      (`VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`/`VAPID_CLAIMS_EMAIL`) and
      threaded through `docker-compose.yml`
- [x] Frontend: `usePush()` hook (`web/src/hooks/usePush.ts`) — checks
      `serviceWorker`/`PushManager` support, calls
      `Notification.requestPermission()` + `pushManager.subscribe()`, POSTs the
      subscription; a `PushSection` in Settings shows Enable/Disable + "Send
      test notification", hidden entirely when `pushConfigured` is false
- [x] **Known gap, explicitly deferred**: `send_push()` has no real caller yet.
      This checklist item's own two named triggers — a daily insight and a
      generated workout — aren't features that exist in this codebase (no
      `assistant.get_daily_insight()`, no workout-generator). Wiring either one
      up is out of scope until that feature itself is built; today's one real
      caller is the manual "Send test notification" action, which exists
      specifically to prove the plumbing end-to-end without waiting on either
- [x] Verify: `tsc -b`/`oxlint`/`npm run build` all clean (build output
      confirmed `manifest.webmanifest` + `sw.js` with 14 precached entries);
      full `docker compose up -d --build` deploy, clean startup logs.
      `GET /api/config` confirmed `pushConfigured:true` post-deploy (had to add
      the 3 new env vars to `docker-compose.yml` — a real gap hit here:
      `.env`'s own values are invisible to the container unless also listed in
      compose's `environment:` block, same lesson as every other secret in
      this file). Screenshotted Settings against the live LAN URL (plain HTTP)
      — Push section correctly rendered "Not supported in this browser" (no
      `serviceWorker` in an insecure context — this *is* correct browser
      behavior, not a bug); re-screenshotted against the tailnet HTTPS URL —
      same section now showed a live "Enable" button, confirming the SW
      registered and `PushManager` is available under a real secure context.
      Drove the actual subscribe flow with a real (non-headless-limited logic)
      Chromium context with notification permission pre-granted: got as far as
      `Notification.requestPermission()` resolving `"granted"` and the app
      correctly calling `pushManager.subscribe()`, which then failed with
      "Registration failed - permission denied" — a well-documented headless-
      Chromium limitation (no real FCM sender registration path without an
      actual signed-in browser), not a bug in this implementation; confirmed
      the failure surfaced cleanly through the UI's own error state rather
      than hanging or crashing. Backend robustness verified directly: inserted
      a fake/malformed `PushSubscription` row via `docker exec` + a scratch
      script, called `POST /api/push/test`, confirmed it returned a clean
      `{"sent":0}` (200, not a 500) with the failure logged — this caught and
      fixed a real gap along the way (the original `except WebPushException`
      only handler didn't cover a plain `requests` exception from an
      unreachable endpoint, which would have 500'd the whole call and blocked
      delivery to a user's *other*, healthy devices; broadened to catch
      `Exception` generally, pruning only on a genuine 404/410 from the push
      service itself). Cleaned up the fake row afterward. **Not verified from
      this environment** (documented limitation, same shape as the existing
      LAN-visibility one in `.RUNBOOK.md`): an actual OS-level notification
      arriving on a real device — headless Chromium can't complete a real push
      subscription, and there's no phone on hand here. Next real step is the
      user clicking Enable + Send test notification on their own phone/browser
      via the tailnet HTTPS URL. Also fixed a real regression in
      `scripts/screenshot.py` hit while verifying this: the earlier sidebar-
      scroll fix (`Shell.tsx`, `h-svh overflow-hidden` + `overflow-y-auto` on
      `<main>`) capped the *document* to viewport height, so the script's
      `full_page=True` capture silently stopped seeing anything below the
      fold on any tab taller than one screen (Settings, Insights) — fixed by
      temporarily neutralizing the scroll-capping styles on the throwaway
      Playwright page right before capture
- [x] Commit: "Phase 0.11: PWA + push notifications"

---

## Phase 1 — Multi-tenant isolation & auth

### 1.1 daily_steps composite PK
- [x] Copy-table migration in `models.init_db()` (SQLite can't alter PKs): new table
      PK `(date, user_id)`, backfill NULL user_id → `'default'`, swap, idempotent —
      `_migrate_daily_steps_composite_pk()` reflects the live table's exact column
      set via `PRAGMA table_info` (so it carries any column added since by
      `_migrate_add_missing_columns()`, which runs first in `init_db()`), copies
      every row with `COALESCE(user_id, 'default')`, then drops/renames. Idempotent
      via a `PRAGMA table_info` check for whether `user_id` is already part of the
      primary key — true for both an already-migrated DB and a brand-new one
      (`create_all()` already builds the composite-PK schema from scratch there).
      `DailySteps.user_id` changed from `nullable=True` to `primary_key=True,
      default=DEFAULT_USER_ID`
- [x] Update every `db.get(DailySteps, date)` call site (garmin_sync, garmin_import,
      models.py's `day_needs_wellness_sync`) to composite `(date, user_id)` lookup —
      `stats.py`'s `DailySteps` queries use `.query().filter()`, not `.get()` by PK,
      so they needed no change (already `owned_by()`-scoped). `day_needs_wellness_sync`
      gained a `user_id` parameter, threaded from its one call site in
      `garmin_sync.py`. No `coach.py` call site exists — that module doesn't touch
      `DailySteps` at all
- [x] Verify: migration on a **copy** of the live DB; row counts identical; wellness
      cards still render — copied the live DB out of the running container
      (`docker cp`), ran the actual `_migrate_daily_steps_composite_pk()` function
      against the copy inside a throwaway container built from the real app image
      (so real SQLAlchemy/dependencies, not a bare Python venv): 208 rows before
      and after (no data loss), PK correctly `(date, user_id)`, zero NULL `user_id`
      rows, and a second run confirmed idempotency (no further change). Deployed
      for real via `docker compose up -d --build`; confirmed via a fresh `sqlite3`
      read against the actual production DB (not just the copy) that the composite
      PK and all 208 rows are present; `GET /api/wellness`/`GET /api/steps` both
      still return correct real data post-migration. Did not force a live Garmin
      sync to exercise the write path directly, since Garmin was mid-rate-limit-
      cooldown at deploy time and forcing a sync would only have extended that
      backoff — relied instead on the migration's data-preservation proof plus
      direct review of every updated `.get()`/constructor call site
- [x] Commit: "Phase 1.1: daily_steps composite PK migration"

### 1.2 Auth schema
- [x] `User.oidc_subject` (unique, nullable) — `users` already in `_MIGRATABLE_TABLES`,
      so `_migrate_add_missing_columns()` picks it up with no extra migration code
- [x] New `ApiToken` table: `id, user_id, token_hash (sha256), name, created_at,
      last_used_at` — device tokens for headless clients. A whole new table
      (`create_all()` creates it from scratch), no `_MIGRATABLE_TABLES` entry needed
- [x] Verify: deployed via `docker compose up -d --build`; confirmed against the
      live production DB that `users` gained the `oidc_subject` column and
      `api_tokens` exists as a real table; confirmed `GET /api/coach/personality`
      (a `User`-table read) and `GET /api/config` still work correctly post-migration
- [x] Commit: "Phase 1.2: auth schema (oidc_subject, api_tokens)"

### 1.3 Auth middleware
- [x] `app/auth.py`: `current_user_id()` FastAPI dependency — `AUTH_MODE=disabled`
      (default) → DEFAULT_USER_ID; else Bearer JWT (PyJWT + cached JWKS fetch;
      env `OIDC_ISSUER/OIDC_AUDIENCE/OIDC_JWKS_URL`; auto-provision User on first
      valid sub) or `X-Api-Token` (hash lookup, stamp last_used_at); else 401 — JWKS
      cache is a module-level dict with a 1hr TTL, force-refetched once if a token's
      `kid` isn't found in the current cache (covers an IdP key-rotation edge case
      without needing that hourly wait). Not wired into any endpoint yet — that's
      Phase 1.4; this module is dormant/unused by itself, exactly why disabled-mode
      verification below is trivially "zero behavior change"
- [x] Added `PyJWT`/`cryptography` to `requirements.txt` — had to bump the initial
      `PyJWT==2.9.0` pin to `2.10.1` after a real dependency-resolution conflict:
      `claude-agent-sdk`'s `mcp` dependency requires `pyjwt>=2.10.1`
- [x] Verify: disabled mode = zero behavior change (curl suite); enabled mode rejects
      missing/bad tokens, accepts a hand-built test JWT — deployed (confirming the
      new deps installed cleanly and the app starts with zero behavior change, since
      nothing imports `auth.py` yet) and curl-verified `/api/config` still works.
      Wrote two isolated test scripts run inside the real app image against a
      throwaway scratch DB (`DB_PATH` pointed at a `/tmp` file, never the real
      production data): **disabled mode** — confirmed `current_user_id()` returns
      `DEFAULT_USER_ID` unconditionally even with garbage `Authorization`/
      `X-Api-Token` headers. **enabled mode** — generated a real RSA keypair,
      built a matching JWKS, hand-signed a test JWT, and pre-populated
      `auth._jwks_cache` (so no real network fetch to a real IdP was needed):
      confirmed a valid JWT auto-provisions a `User` row with the correct
      `oidc_subject`, a second call with the same `sub` returns the same
      `user_id` (no duplicate), a malformed JWT and a missing credential both
      correctly 401, and the `X-Api-Token` path correctly resolves via SHA-256
      hash lookup, stamps `last_used_at`, and 401s on an unknown token. All test
      artifacts (scratch DBs, scripts) cleaned up afterward
- [x] Commit: "Phase 1.3: OIDC/JWT + device-token auth middleware"

### 1.4 Endpoint threading
- [x] Every endpoint in `main.py`: `user_id = Depends(auth.current_user_id)` replaces
      DEFAULT_USER_ID literals — all ~40 endpoints threaded (catalogued exhaustively
      first via a research pass before editing). Along the way, fixed several endpoints
      that had **no user scoping at all** (not just a hardcode) prior to this phase:
      `PATCH /api/runs/{run_id}` (`db.get(Run, run_id)` → `owned_by()`-filtered query),
      `GET /api/garmin/route-diagnostics` (added an `owned_by()` filter it never had).
      The 9 `coach.py`-backed endpoints (health-notes/workouts/recovery-*) turned out to
      already accept a `user_id` parameter (defaulting to `DEFAULT_USER_ID`) — main.py
      just wasn't passing it through; smaller fix than the original research pass
      expected, since it only needed call-site threading, not new function signatures.
      `/auth/strava/login`, `/api/geocode`, `/api/chat/status`, and the SPA catch-all
      stay unscoped (genuinely no user concept)
- [x] In-memory job state keyed `(user_id, source)` (quick-sync + backlog dicts) — both
      `_quick_sync_jobs`/`_backlog_jobs` switched from eagerly-initialized `{source: {...}}`
      dicts to lazily-created `{(user_id, source): {...}}` via `_get_quick_sync_job()`/
      `_get_backlog_job()` (`.setdefault(...)`), since the set of real users isn't known
      ahead of time the way the 2 fixed sources were
- [x] `sync_meta` scoping: `user_key(user_id, key)` helper (`models.py`) applied to
      every genuinely per-user key across `main.py` (`_record_sync`,
      `_refresh_dashboard_cache`, the dashboard cache pair, `manual_sync`/
      `start_backlog_sync`'s error-clear) and `garmin_sync.py` (the 4 rate-limit-cooldown
      helper functions gained a `user_id` parameter and now use `user_key()`; the
      adaptive-plan-last-checked and activities-backlog-offset/complete keys too).
      Deliberately **not** applied to the geocode cache (`f"geocode_{lat:.2f}_{lon:.2f}"`)
      — that's keyed by physical location, not by asker, and should stay one shared
      cache. `_next_auto_sync_time()` checks `DEFAULT_USER_ID`'s own namespaced key
      specifically (documented simplification — it's a one-time scheduler-startup
      heuristic to avoid hammering Strava right after a redeploy, not per-user data,
      and `_auto_sync()` already re-syncs every credentialed user on every tick
      regardless of what this heuristic decides). One-time copy of every pre-1.4
      global key to its `user_key(DEFAULT_USER_ID, key)` equivalent —
      `_migrate_sync_meta_to_user_keys()`, copies (not moves) so a rollback still reads
      its own expected keys, idempotent (skips a key whose namespaced target is already set)
- [x] Run-id collision guard: on cross-user id conflict in `_process_activity`, write
      `{source}_{user_id}_{activity_id}` — `models.resolve_run_id(db, source, activity_id,
      user_id)`, shared by both `strava.py` and `garmin_sync.py`'s `_process_activity`
      *and* their loop-level dedup-check call sites (both must agree on the same id for
      the same activity). Plain `f"{source}_{activity_id}"` id used in the common case
      (no existing row, or an existing row already owned by this user or unowned);
      falls back to the user-suffixed id only on a genuine cross-user conflict
- [x] Verify: full curl regression as default user; `STATUS.md` — deployed for real
      (`docker compose up -d --build`, confirmed clean startup logs) and ran a full
      curl regression across every read endpoint (all 200s) plus content-level checks
      confirming exact byte-for-byte-equivalent data to before the refactor (same sync
      timestamps — proving the one-time key migration correctly carried forward
      existing state, same route-diagnostics counts, same dashboard headerStats, same
      run counts windowed/all-time, same goal count). Verified a real write path
      end-to-end, not just reads: triggered `POST /api/sync/strava`, confirmed the job
      dict correctly tracked running→done under the new `(user_id, source)` keying,
      and confirmed `GET /api/sync/meta` reflected the new sync timestamp under the
      namespaced key. Screenshotted Home against the live production deployment —
      identical to pre-refactor, confirming the full stack (new frontend + refactored
      backend) still works together correctly
- [x] Commit: "Phase 1.4: per-user scoping of endpoints, job state, sync_meta"

### 1.5 Token management + onboarding
- [x] `POST/GET/DELETE /api/tokens` (raw token shown once); Settings UI section —
      the raw token (`secrets.token_urlsafe(32)`) is only ever returned from the
      create call; every other read persists/returns just its SHA-256 hash,
      matching `ApiToken`'s existing design from Phase 1.2. New `TokensSection` in
      Settings shows a one-time "copy now" box on create, plus a list of existing
      tokens (name/created/last-used) with a revoke action
- [x] First-run wizard (new frontend): connect Strava/Garmin → create goal —
      ~~confirm training config (feeds Phase 4's UserTrainingConfig)~~ struck: that
      table/settings don't exist yet (Phase 4 hasn't started), so there's nothing
      real to confirm — a step that configures nothing isn't worth building yet;
      revisit once Phase 4.2 ships. New `OnboardingPage.tsx` (`/onboarding`, outside
      the `Shell` nav chrome) with the 2 real steps, reusing `GoalFormDialog` from
      0.9 rather than a new form. New `useOnboardingGate()` hook (called from
      `Shell`) redirects there automatically only when every one of 4 signals
      agrees the account is genuinely fresh (no Strava, no Garmin, zero goals, zero
      runs) — deliberately conservative so it can never misfire against an
      already-populated account. While here: fixed a real gap noticed along the
      way — the new Settings page had no way to actually *connect* Strava if
      disconnected at all (legacy had this as a header button, never ported when
      the header was rebuilt in 0.2) — added a "Connect Strava" link to Settings'
      Strava section too, not just the wizard
- [x] Verify: token round-trip incl. ingest auth (after 2.2); wizard screenshot —
      the "(after 2.2)" qualifier in this checklist item is load-bearing: Phase
      2.2's ingest endpoint doesn't exist yet, so there's no real endpoint to test
      token-gated ingest auth against. Verified everything that *is* testable now:
      real `POST/GET/DELETE /api/tokens` round-trip against the live production
      backend (create → list shows it without the raw token → delete → list empty).
      Verified actual authentication (not just CRUD) in an isolated test — same
      technique as Phase 1.3, a throwaway scratch DB, never production data:
      created a token the same way the real endpoint does (`secrets.token_urlsafe`
      + SHA-256 hash), confirmed it authenticates via `X-Api-Token`, stamps
      `last_used_at`, and correctly stops authenticating once revoked (401).
      Screenshotted the wizard directly (`/onboarding`) against live production —
      both steps correctly detect and reflect the account's real state (Strava
      "Connected", Garmin "Configured", "4 goals set." on step 2) rather than
      showing empty-account UI against populated data. Screenshotted Home to
      confirm the onboarding gate correctly stays dormant for the real,
      already-populated account (no unwanted redirect)
- [x] Commit: "Phase 1.5: device tokens + onboarding wizard"

---

## Phase 4 — Workout generator

### 4.1 Readiness core
- [x] **HRV had zero backing at all before this** (no column, no Garmin fetch code
      anywhere — confirmed by grepping `garmin_sync.py` in full). Added
      `DailySteps.hrv_last_night_avg_ms`/`hrv_status`, and extended
      `_sync_daily_wellness` with a 4th independently-wrapped `try/except` calling
      `client.get_hrv_data()` (`_extract_hrv`, mirrors `_extract_vo2max`'s defensive
      multi-candidate-key pattern since the exact 0.3.6 response shape isn't vendored
      anywhere in this repo to check against)
- [x] `stats.readiness(db, user_id, date)` → hrvDeltaMs (vs 7d baseline),
      restingHrDelta, sleepScore, acuteChronicRatio (7d/28d mileage — a genuinely new
      window, not a reuse of `training_load_trend`'s existing 28d-vs-28d comparison),
      daysSinceHard, flags (`hrv_below_baseline` >10ms drop, `rhr_spike` +5bpm,
      `sleep_deficit` <6.5h) — single computation core, chat tool `get_readiness`
      added in `assistant.py`
- [x] Verify: deployed for real; a live Garmin sync immediately after deploy pulled
      real HRV on the first attempt with no raw-key mismatch (53ms/BALANCED and
      49ms/UNBALANCED for two real days — the guessed field names in `_extract_hrv`
      were correct against the actual account, no fallback debug-log path needed).
      `stats.readiness()` run directly against live data returned sensible real
      numbers (correctly `null` for `hrvDeltaMs`/`restingHrDelta` where a 7-day
      baseline/resting-HR reading doesn't exist yet, rather than fabricating one).
      Confirmed the chat tool end-to-end via a real `/api/chat/message` call — fired
      `get_readiness`, cited the exact real numbers back correctly, and combined them
      naturally with existing health-note context in the same reply
- [x] Commit: "Phase 4.1: readiness computation + chat tool"

### 4.2 Structured endurance steps
- [x] Extended `coach._validate_steps` into a dispatcher on `stepType` presence —
      absent = the original generic shape (every already-stored mobility/warmup
      workout keeps validating unchanged); present =
      `{stepType: warmup|active|rest|cooldown|repeat, durationSec XOR distanceM
      (or neither = lap-press "open"), targetType: hr_zone|hr_custom|power|pace|
      cadence|open, targetZone XOR targetLow/High, repeatCount+children (1 level —
      a repeat's children may not themselves repeat, enforced in
      `_validate_endurance_step`)}` — metric units stored (distanceM in meters).
      `_steps_total_duration_sec` reworked into a recursive `_step_duration_sec` so a
      `repeat` block's duration (children's total × repeatCount) is accounted for
      correctly, and so it can cleanly skip `strength_exercise` steps once 4.4 adds them
- [x] `UserTrainingConfig` table: `user_id PK, max_hr, threshold_hr, ftp_watts?,
      zones_json, weekly_ramp_pct (default 3.0), mesocycle_pattern ("3:1"),
      distribution ("pyramidal")`, plus 2 fields pulled forward from 4.4's design
      since they belong on the same flat per-user row: `strength_days_per_week`
      (default 2), `strength_template` (default `"full_body_ab"`) — `GET/PATCH
      /api/training-config` + a new Settings "Training" section. Caught and fixed a
      real bug here: `Column(default=...)` only applies at INSERT/flush time, not to
      a plain unflushed Python object — the original `get_training_config()`'s
      "return defaults for a fresh account" fallback silently returned `None` for
      every default field until the defaults were passed explicitly in Python instead
      of relying on the ORM column default
- [x] Frontend: endurance steps render in `WorkoutCard` (stepType label, duration/
      distance, humanized target — `repeat` nests its children one level, matching
      the backend's own 1-level rule); `WorkoutInput`/`api.ts` gained a `steps` field
      that didn't exist on the wire type at all before this (a real pre-existing gap,
      not something 4.2 introduced)
- [x] Verify: deployed for real; curled a workout create with a nested `repeat` block
      and confirmed `targetDurationSec` auto-computed correctly (1980s = 600 +
      4×(180+90) + 300, i.e. the repeat-duration fix above actually works against a
      real request, not just in theory); confirmed mutual-exclusion and
      missing-target-field validation both 400 with specific messages; confirmed the
      original legacy generic-step shape still creates correctly unchanged. Screenshotted
      Settings' new Training section against live production (all defaults correct:
      ramp 3%, 2 strength days/week, 3:1, Pyramidal) — one capture caught the
      pre-existing "Sync schedule"/`Resting HR` rows still loading (a screenshot-
      timing race, confirmed non-reproducible on a second capture, not a real
      regression from this work). Cleaned up all test workout rows/training-config
      values from the real production DB afterward
- [x] Commit: "Phase 4.2: endurance step contract + training config"

### 4.4 Strength step contract + progression state

New sub-phase (not in the original plan) — added when the user asked to expand the
generator to also prescribe strength/weight-training sessions with real sets/reps/
weight/rest structure and a live rest-timer, mirroring a real Hevy-routine gap this
session's own memory already flagged (`feedback_workout_rest_times`: a Hevy routine
built with `restSeconds` left null on every exercise, with a direct note that RunLog's
own `Workout.steps` schema would likely need the same fix eventually).

- [x] `coach._validate_steps` gained a third dispatched shape,
      `stepType: "strength_exercise"` → `{exercise, restSeconds, sets: [{index,
      targetType: "reps"|"hold_sec", targetReps?, targetHoldSec?, targetWeightLb?,
      actualReps?, actualHoldSec?, actualWeightLb?, completedAt?}]}` — `restSeconds`
      lives on the exercise, not per-set, mirroring the real Hevy routine shape
      (confirmed from an actual captured Hevy API response in this session's own
      history: rest lives per-exercise there too, not per-set). `actual*`/
      `completedAt` start absent at prescription time and fill in incrementally via
      a plain `PATCH /api/workouts/{id}` steps replacement as Phase 4.5's workout-
      runner logs each set live — no new endpoint needed
- [x] New `ExerciseProgress` table (`(user_id, exercise)` composite PK,
      `current_weight_lb`/`current_reps_target`/`current_hold_sec`/
      `last_completed_at`) + `coach.get_exercise_progress`/`list_exercise_progress`/
      `upsert_exercise_progress` — derived state the Phase 4.3 generator's double-
      progression rule reads/writes, never set directly by a chat tool or REST
      endpoint. Caught the same `Column(default=...)`-only-applies-at-flush bug as
      4.2's `get_training_config` and fixed it the same way (explicit Python
      defaults in `get_exercise_progress`'s fallback)
- [x] Frontend: `WorkoutFormDialog` gained a real step editor — scoped deliberately
      to strength steps only (shown when `workoutType === "strength"`), not a
      generic all-three-shapes editor. Authored as "N sets of one target" (matching
      how a real prescription reads, "3×8-12 @ 45lb") rather than editing every
      individual set — per-set *actuals* are what genuinely vary session to session
      and get logged live via 4.5's workout-runner, not authored here. This also
      closes a real pre-existing gap: the dialog had *no* step-editing UI at all
      before this, for any step shape. `WorkoutCard` renders a strength step as a
      collapsed `<details>` summary (exercise, set count, rest) with a per-set
      breakdown inside
- [x] Verify: deployed for real; curled a strength workout create with 2 exercises
      (a 3×10 rep-based squat + a single hold-based plank set), confirmed correct
      structure back; confirmed `targetType` validation 400s with a specific message;
      `PATCH`-ed the same workout with actuals filled in + `status: "completed"`
      (simulating what the workout-runner will do) and confirmed it saves correctly;
      exercised `get_exercise_progress`/`upsert_exercise_progress`/
      `list_exercise_progress` directly against production, confirming the same
      explicit-defaults fix pattern as 4.2 actually works here too. Drove the real
      `WorkoutFormDialog` via Playwright (selected Strength, added an exercise) and
      screenshotted the rendered editor and the resulting `WorkoutCard` against live
      production — both match the design exactly. Cleaned up all test data afterward
- [x] Commit: "Phase 4.4: strength step contract + progression state"

### 4.3 Generator engine
- [x] `WeeklyPlan` table: `(user_id, week_start) PK, target_tss, actual_tss,
      is_deload, frozen` — `target_tss`/`actual_tss` store a mileage-based proxy,
      not a real Training Stress Score (Phase 6.1's per-activity TSS hasn't shipped
      yet), same "real number now, real TSS later" tradeoff `stats.readiness()`'s
      `acuteChronicRatio` already makes
- [x] `app/generator.py` — deterministic, no LLM, endurance path evaluated in order:
      (1) phase from the nearest active race goal's date (base/build/peak/taper) +
      mesocycle position (deload week × 0.75); (2) weekly budget = min(last_week ×
      (1+ramp%), phase ceiling); (3) readiness gate — 1 flag: downgrade one tier
      (interval→tempo→easy — "Z2"/"recovery" both map to `easy`, there's no separate
      workout_type value for either); 2+: rest **and** freeze the week (`frozen=1`);
      severe HealthNote: rest, re-checked fresh every day so it naturally covers the
      rest of the week for as long as the note stays active; (4) distribution
      audit — approximated via a coarse hard/easy day-*type* ratio over the trailing
      7 days (tempo/interval count as hard), not true time-in-zone (this app doesn't
      store per-second HR-zone breakdowns at sync time — a documented v1 gap, not a
      silent one); (5) two-a-days only build/peak with 0 readiness flags, modality
      split via the new `Workout.scheduled_time` column, second session always
      `cross_train`/recovery-intensity. Idempotent per (user, date) — reruns
      recompute/overwrite only this module's own `source="generator"` rows, never a
      `"coach"`- or `"garmin"`-sourced row for the same date
- [x] **Strength path** (not in the original 4.3 spec — added when the generator's
      scope expanded to also prescribe strength sessions, see 4.4's context):
      `STRENGTH_TEMPLATES["full_body_ab"]` (hardcoded 2-day A/B rotation, ~5
      exercises/side, explicitly bounded v1 — not a real exercise-library system),
      scheduled on `UserTrainingConfig.strength_days_per_week`'s configured weekdays,
      readiness-gated the same way (0 flags: normal progression; 1: hold current
      targets, pause progression; 2+/severe health: a light bodyweight-only
      session). `apply_strength_progression()` (double progression, evaluated once a
      session is marked completed with logged actuals, not at prescription time) is
      wired into `coach.update_workout` via a lazy import on the "planned →
      completed" transition for a `workout_type="strength"` row — the same deferred-
      import convention `main.py` already uses for its own optional subsystems,
      needed here to avoid a hard circular import (`generator.py` imports `coach.py`
      for step validation; `coach.py` only needs `generator.py` at this one call site)
- [x] Scheduler: daily 04:00 `America/New_York` (via `util.APP_TIMEZONE`, not
      container-UTC — same "local means the configured timezone, not the
      container's clock" discipline `local_today()` already established) for every
      non-demo user, skipped entirely on a demo deployment (same reasoning as
      auto-sync: demo users' accounts are pre-seeded and ephemeral, a real
      periodization engine running against them would be pure waste) +
      `POST /api/generator/run` (optional `date` param, for on-demand/verification use)
- [x] Verify: deployed for real; force-ran the generator against live production data
      via the REST endpoint and directly via a container probe (bypassing real data
      with synthetic readiness states to test the downgrade ladder in isolation).
      **Caught and fixed two real bugs during this pass, not theoretical**:
      (1) the endurance and strength paths' upsert-matching both keyed on
      "first generator row for this date," so generating both for the same date
      silently overwrote the endurance prescription with the strength one — fixed by
      adding an explicit `domain` (endurance / endurance_second / strength) to the
      upsert key, confirmed by re-running and seeing two distinct rows; (2) a race
      goal whose `target_date` had already passed but was still `status="active"`
      (never marked completed) pinned `_phase_for_date` to a degenerate/negative
      "weeks until" indefinitely — fixed by filtering to `target_date >= today` in
      that query. Confirmed idempotency directly (rerunning the same date twice
      returns the same 2 row ids, no duplicates) and that a pre-existing
      `"garmin"`-sourced workout for a test date was left completely untouched.
      Confirmed the full strength-progression loop end-to-end: a hit-all-sets rep
      exercise with weight tracked bumped weight by its category increment; the same
      exercise with no weight tracked (bodyweight) correctly did *not* bump (a real,
      documented v1 gap — bodyweight rep exercises have no progression path yet,
      only weighted-rep and hold-duration exercises do); a missed-target set
      correctly held steady; a hold-based exercise correctly bumped duration.
      **Known v1 limitation surfaced by real data, not hypothetical**: this account
      has multiple active `race`-type goals (an actual marathon *and* a literally-
      named "Wedding race" goal nearer in time) — `_phase_for_date` picks the
      nearest one by design, which in this real case is the wedding, not the
      marathon being trained for. Not fixed here (the spec doesn't say how to
      disambiguate multiple active race goals); worth revisiting if it matters in
      practice. Cleaned up all test workouts/weekly-plan/exercise-progress rows from
      production afterward and confirmed real data (144 runs, 9 real workouts) unaffected
- [x] Commit: "Phase 4.3: goal-driven daily workout generator"

### 4.5 Workout-runner + rest timer UI

New sub-phase (not in the original plan) — closes the loop the user asked for at the
very start of this phase: a live foreground timer for lifting, confirmed exact UX
directly ("I start a timer to hold a 30 second plank, it gives me a 5 second
countdown then starts the actual countdown"). Only meaningful once 4.4's
`strength_exercise` step shape existed to drive it.

- [x] `web/src/hooks/useCountdown.ts` — this app's first `setInterval` usage anywhere
      (confirmed zero prior instances). One hook instance is reused across the
      runner's several sequential countdowns (5s get-ready, hold, rest) rather than
      fixing a duration/callback at construction time — `start(seconds, onComplete)`
      takes both per call, since each phase needs a different completion action.
      `pause`/`resume`/`skip` included alongside `start`.
- [x] `web/src/lib/beep.ts` — Web Audio API oscillator beep (no binary asset needed,
      nothing like this existed in the repo before). Wrapped in try/catch since
      autoplay policy can block `AudioContext` before any user gesture — the visual
      countdown stays authoritative either way.
- [x] New top-level route `/workouts/:id/run` (outside `<Shell/>`, same "focused, no
      nav chrome" pattern as `/onboarding`/`/demo-login`) — `WorkoutRunnerPage.tsx`.
      Reuses the already-cached `useWorkouts()` list (finds by id from the URL param)
      rather than adding a new single-workout GET endpoint, since the list is already
      fetched app-wide. Flattens only `strength_exercise` steps into a linear
      sequence of sets to run through — endurance steps (warmup/active/rest/
      cooldown) are GPS-tracked externally via Strava/Garmin, not a manual timer, so
      they're left alone.
- [x] Flow per set, matching the user's exact description: a hold-based set runs a
      5s "Get ready…" countdown, then the real hold countdown, then auto-records
      `actualHoldSec` = the target and advances; a rep-based set shows weight/reps
      inputs pre-filled from the target with a "Log Set" button. Every set (either
      kind) is followed by a rest countdown sized from the step's `restSeconds` —
      skipped only after the very last set overall. A beep fires at every countdown
      completion (get-ready → hold, hold → rest, rest → next set).
- [x] "Finish Workout" builds the full `steps` array with actuals folded in (adds
      `completedAt` per logged set) and does a single `PATCH /api/workouts/{id}`
      with `status: "completed"` — no new endpoint, reuses `update_workout`'s
      existing full-steps-replacement contract exactly as 4.4 designed it to be used.
      This is also what triggers `coach.update_workout`'s existing "planned →
      completed" hook into `generator.apply_strength_progression` — the runner was
      the missing piece that hook was built for in 4.3/4.4 but had no real caller yet.
- [x] `WorkoutCard.tsx` gained a "Start" button, shown only when a workout has at
      least one `strength_exercise` step and is still `status: "planned"` — an
      endurance-only workout never shows it, matching the runner's own scope.
- [x] Known v1 limitation, stated up front rather than discovered later: the runner
      always starts from set 1 on load — it does not persist/resume mid-workout
      progress across a page reload. Matches this phase's established "explicitly
      bounded v1, not a guess at unstated requirements" discipline (same framing as
      4.3's hardcoded exercise template).
- [x] Verify: `tsc -b`/`oxlint` both clean (one pre-existing, unrelated `oxlint`
      warning in `button.tsx`); `npm run build` succeeds. Built a throwaway container
      from the full image (frontend included) and created a real test workout with
      one hold-based set (10s plank) and one rep-based set (8×25lb goblet squat), then
      drove the actual rendered UI end-to-end via a scripted Playwright click-through
      (not just a curl simulation): confirmed the "Start" button appears on the
      Workouts list, the get-ready→hold→rest→log-set→finished sequence renders and
      transitions correctly with real 5s/10s/20s countdowns actually elapsing, and
      that clicking "Finish Workout" redirects back to `/workouts`. Confirmed via a
      direct DB query afterward that the `PATCH` persisted real actuals
      (`actualHoldSec: 10`, `actualReps: 9`, `actualWeightLb: 30`,
      `status: "completed"`) **and** that `apply_strength_progression` fired for
      real off that exact request — Goblet Squat's `ExerciseProgress` row bumped
      25lb→35lb (hit-target rep progression) and Plank's bumped 10s→15s (hit-target
      hold progression), confirming the full runner→completion→progression pipeline
      this phase exists to close, not just the UI in isolation. Only after that
      passed was the real production container recreated with the same verified
      image — confirmed real data (144 runs, 5 goals) untouched.
- [x] Commit: "Phase 4.5: workout-runner + rest timer UI"

---

## Phase 11 — Interactive Ephemeral Demo Environment

Meant for a separate, disposable cloud deployment (Koyeb's free tier — see 11.4) —
`ENABLE_DEMO_LOGIN`/`AUTH_MODE=enabled` are deployment env vars, unset on this app's
own real NAS instance, which sees zero behavior change throughout. Deliberately
purely request-driven, no background scheduler dependency at all (see 11.1/11.3
below) — this is what makes it viable on a free host that suspends the container
between requests, not just an always-on one.

### 11.1 Ephemeral auth & session lifecycle
- [x] `User` gains `is_demo`/`expires_at` (already-existing `_MIGRATABLE_TABLES` entry
      picks them up for free). **Real DB-level `ForeignKey(..., ondelete="CASCADE")`**
      added to every per-user table's `user_id` column (a first-of-its-kind pattern for
      this codebase, which otherwise uses zero FK constraints anywhere) plus a
      `PRAGMA foreign_keys=ON` connect-event listener — verified safe for the existing
      production DB specifically because `create_all()` never alters an already-
      existing table's schema: real production tables have no FK clause in their
      on-disk DDL (confirmed via `PRAGMA foreign_key_list(runs)` → `[]` post-deploy),
      so the constraint only ever takes effect on a freshly created database. Caught
      and fixed a real bug this exposed: without any `relationship()` between `User`
      and `ApiToken` (this codebase declares none), a single flush doesn't guarantee
      INSERT ordering across the two tables — `demo.create_demo_session()` needs an
      explicit `db.flush()` after adding the `User` row and before adding the
      `ApiToken` row, or the FK constraint trips on a genuinely fresh DB
      (`sqlite3.IntegrityError` reproduced and fixed during verification, not
      theoretical)
- [x] `app/demo.py`: `POST /auth/demo/login` (fixed `demo`/`demo` body, not a real
      credential store), capacity check under a `threading.Lock` (mirrors
      `_quick_sync_lock`), mints a real `ApiToken` (same `secrets.token_urlsafe(32)` +
      SHA-256 pattern as `POST /api/tokens`) rather than a JWT — `auth.py`'s existing
      `X-Api-Token` path authenticates it with **zero changes to `auth.py` itself**;
      the demo deployment's `AUTH_MODE=enabled` is what activates that path
- [x] `POST /auth/demo/logout` (deletes only if `is_demo`). **Revised after initial
      ship**: expiry cleanup is lazy, not a periodic scheduler job — demo users never
      have real credentials to sync, so `main.py`'s `startup()` skips registering
      `_auto_sync` entirely when demo mode is on, and `create_demo_session()`
      opportunistically sweeps expired sessions (`demo._sweep_expired()`) under the
      same capacity lock, on every login, before counting. This means the demo
      deployment registers **zero** background jobs and has no dependency on the
      process staying alive between requests — verified by backdating a session's
      `expires_at` with no scheduler running at all and confirming a plain login
      request both swept it (cascade-confirmed gone) and reclaimed its capacity slot.
      `demo.sweep_expired_demo_users()` kept as a standalone callable (ad-hoc/admin
      use, or a future always-on target that wants extra tidiness) but nothing in
      this app calls it anymore
- [x] Verify: full flow tested against an isolated **throwaway second container**
      (fresh anonymous volume, port 8001, demo env vars) — never touched the real
      running container. Two logins succeeded with independent seeded data, a 3rd hit
      429 at `DEMO_CAPACITY=2`; logout and a manually-backdated-`expires_at` sweep both
      confirmed via direct SQLite inspection that every child-table row (runs, goals,
      chat, tokens) was really gone — true FK cascade, not application-level deletes.
      Redeployed the real production container afterward on the same updated image:
      clean startup, unchanged 150 runs, `isDemoUser:false`, `/auth/demo/status` →
      `{"enabled":false}` — zero regression
- [x] Commit: "Phase 11.1: ephemeral demo auth, capacity limits, and cascade teardown"

### 11.2 On-the-fly sandbox seeding
- [x] `app/seed_engine.py` (new — no prior generic seeder existed to refactor;
      `models.py`'s `_seed_*` functions seed the *real* default user's actual gear/
      goal and were never touched). `seed_demo_user(db, user_id)` runs **synchronously**
      inside `create_demo_session` (not a `BackgroundTask` — pure Python, zero external
      I/O, fast enough that a visitor never sees an empty Home tab) — ~90 days of
      `DailySteps`, ~50-60 `Run` rows (rotating Easy/Tempo/Interval/Long Run, real
      `suggested_type` vocabulary), one active race `Goal`, a 4-message seeded `Chat`
      thread
- [x] Explicitly **not seeded**: `RouteHex` spatial data — Phase 7 (geospatial
      pipeline) doesn't exist in this codebase, so there's no real table to populate
- [x] Verify: two separate demo logins produced fully isolated accounts (58 vs. 38
      seeded runs, confirmed via direct query by `user_id`) with zero cross-talk.
      Screenshotted a logged-in demo Home tab against the live throwaway instance —
      every existing stats computation (goal countdown, 4-week training load, pace
      trend, longest run, this-month-vs-last) rendered correctly from the synthetic
      data with no special-casing needed, confirming the seed data integrates
      cleanly with the real stats engine rather than just superficially existing
- [x] Commit: "Phase 11.2: isolated on-the-fly data seeding per user"

### 11.3 Sandbox guardrails & mock overrides
- [x] `ENABLE_DEMO_LOGIN` guardrail — `/auth/demo/login` 404s when unset
- [x] Sync: `manual_sync`/`start_backlog_sync` short-circuit for a demo user straight
      to a fake `"done"` job state (no thread, no real HTTP call) before the
      credential checks even run — a demo user never has a real credential, so this
      also avoids a confusing "not authenticated" error
- [x] Chat: `chat_message` never imports `assistant.py` for a demo user (no Claude
      Agent SDK client ever constructed), writes real `ChatMessage` rows with a
      randomly-chosen canned reply, returns the same `{reply, toolCalls, charts}`
      shape the real path does
- [x] Settings lock: Garmin connection save/delete + Garmin ZIP import 403
      ("Not available in the demo") via a shared `_reject_if_demo()` helper. **Gap
      found during visual verification, not in the original plan**: the "Connect
      Strava" button (a real OAuth redirect, unrelated to the Garmin-connections
      form) was still live for a demo user — fixed by hiding it client-side when
      `isDemoUser`; noted as not airtight server-side, since `/auth/strava/login`
      is pre-existing, deliberately unscoped (no user-identity threading through the
      OAuth `state` param — a known limitation predating this phase, not fixed here)
- [x] `GET /api/config` gained `isDemoUser`, threaded to `SettingsPage.tsx`'s
      `StravaSection`/`ConnectionsSection`/`GarminImportSection`
- [x] Verify: as a demo user — Sync Now returned an instant fake "done" (confirmed via
      container logs: zero outbound calls), a chat message got an instant canned reply
      with no SDK invocation, Garmin connection save/import/Strava-connect all
      correctly blocked (403 or hidden)
- [x] Commit: "Phase 11.3: demo guardrails and external API mocks"

### 11.4 GitHub CI/CD & 1-click cloud deployment
- [x] `.github/workflows/docker-publish.yml` — checkout → Buildx → GHCR login
      (`GITHUB_TOKEN`) → build root `Dockerfile` → push
      `ghcr.io/treddington4/hale:latest` (+ semver on a version tag)
- [x] **Host: went through two picks before landing on SnapDeploy.** Render's free
      tier requires a card, so tried Koyeb next (no card, always-on free instance —
      genuinely the better fit once 11.1/11.3 dropped the background-scheduler
      dependency, since always-on avoided a cold-start wait on a visitor's first
      click). Built a "Deploy to Koyeb" button from their documented one-click-deploy
      URL params (`type=git`, `builder=dockerfile`, `instance_type=free`,
      `ports=8000;http;/`, `env[...]` pairs) — but the user actually clicked it and
      Koyeb's deploy page came back showing an acquisition banner ("Koyeb is joining
      Mistral") instead of the real form, a live signal their platform is mid-
      transition and not something to depend on right now. Switched again, to
      **SnapDeploy** (also genuinely card-free) — confirmed via their docs that they
      support deploying an existing Dockerfile ("Custom Docker"), not just framework
      auto-detection, but **there's no shareable one-click-deploy URL for it** (only
      a dashboard-driven GitHub connect flow), so `README.md` has manual setup steps
      instead of a badge. Their docs also weren't specific enough to fully confirm
      the custom-Docker path guarantees the *committed* multi-stage `Dockerfile` gets
      used verbatim rather than regenerated from framework detection — flagged
      explicitly in the README as something to double-check in their dashboard,
      since I can't verify SnapDeploy's actual runtime UI behavior from public docs
      alone. `render.yaml` stays deleted (neither Koyeb nor SnapDeploy use a repo-
      committed Blueprint file)
- [x] **Real deploy attempt surfaced a second SnapDeploy quirk**: its dependency
      scanner flagged a hard "requires PostgreSQL" gate the app has never used
      anywhere — traced to a single mention in this very `PLAN.md`'s deferred-scope
      list (`"PostGIS/PostgreSQL migration (rejected at current scale)"`), read by a
      naive text scan rather than actual manifest parsing; worked around via
      SnapDeploy's "external/hosted Postgres" option with a placeholder connection
      string the app never reads. Separately, its env-var auto-detection reads
      `.env.example` directly and demanded non-empty values for every credential
      listed there (Strava/Garmin/Claude/VAPID) despite all of them being genuinely
      optional/mocked-for-demo in the actual code — added **`.env.demo.example`**
      (new, minimal — only the 4 vars demo mode actually needs) and an explicit
      "any placeholder text works for these 8" list in `README.md`'s demo section,
      rather than editing the primary `.env.example` (which correctly serves real
      self-hosters and isn't the actual root cause — the two flagged-vs-not-flagged
      sets don't cleanly map to any single editable property of that file, so
      chasing SnapDeploy's exact heuristic isn't worth it)
- [x] **Follow-up (done directly on a later SnapDeploy retry, merged back in — not
      part of this session's own pass)**: swapped which file is which — the demo
      vars now live at `.env.example` (the filename SnapDeploy's auto-detection
      actually scans by convention) and the full self-host reference moved to
      `.env.running.example`; `.env.demo.example` no longer exists. `README.md`'s
      setup step (`cp .env.example .env`) and its demo-mode section link were both
      stale after this rename — fixed to `cp .env.running.example .env` and a link
      to the new `.env.example` respectively when merging this back into master
- [x] Verify (mine): the GHCR workflow YAML parses correctly and its exact
      `docker build` step was independently validated many times over via
      `docker compose build` on the NAS throughout 11.1-11.3's verification. Every
      specific host-integration claim above (Koyeb's URL param format, the
      acquisition-banner finding, SnapDeploy's Dockerfile-vs-auto-detect ambiguity)
      came from directly fetching each platform's own docs/live pages in this
      session, not assumption — this is exactly the kind of external claim that
      needed checking rather than guessing, and the checking caught a real, live
      platform-stability issue (Koyeb) before it became the user's problem to debug
      after clicking a broken badge
- [x] **Real attempt on SnapDeploy actually failed to deploy** — after clearing both
      the Postgres false-positive and the env-var gate above, its own deploy step
      returned a fully opaque `"Deployment failed: Something went wrong on our end"`
      with zero build log or diagnostic. Combined with Koyeb's acquisition-transition
      blocker, that's two independent card-free hosts each hitting a real reliability
      problem in the same session — decided, on request, to **stop recommending a
      specific free host** rather than keep chasing platform-specific quirks.
      `README.md`'s "Demo mode" section trimmed to state this plainly: the feature
      itself is fully built and verified (11.1-11.3), `ghcr.io/treddington4/hale` is
      published automatically for whenever a solid free option turns up or for
      self-hosting on your own infra, and no further-hours were spent debugging a
      third-party platform's own opaque backend error
- [x] **Verify**: confirmed the GHCR Action actually runs and publishes — though not
      on the first attempt. `docker-publish.yml` had `branches: [main]` while this
      repo's actual default branch is `master`, so the workflow had likely never
      fired on a real push until that mismatch was found and fixed. Also caught
      (same investigation): a later "try debug?" commit had pushed a Dockerfile
      regression (`USER runlog` conflicting with `docker-entrypoint.sh`'s
      root-required `chown`, plus a reverted `$PORT`-aware HEALTHCHECK) that had
      already published as `ghcr.io/treddington4/hale:latest` once the branch fix
      made the workflow start firing — fixed and republished; `gh run list
      --workflow=docker-publish.yml` now shows a real `completed success` run
      against the corrected Dockerfile. Public demo hosting itself is deliberately
      unresolved — revisit if/when a genuinely reliable free (or cheap) option
      comes up.
- [x] Commit: "Phase 11.4: GHCR automated publishing and 1-click cloud deploy hooks"

---

## Phase 12 — Coach iteration: test-data isolation, timezone, safety-vetting, evaluation, self-review

Triggered by reading the real production chat history (90 messages, pulled directly
from `/api/chat/history`) at the user's request, to ground this in actual frustrations
rather than guessed ones. Two concrete real bugs surfaced immediately: (1) a chat
message I sent during earlier Phase 4 verification caused the coach to log a fake
"shin splint" `HealthNote` that resurfaced as real context days later in a genuine
conversation, and (2) real date/context confusion (the coach contradicted the user
about whether a workout was already done; misattributed a run's date by 2 days) traced
partly to `local_today()` being a single hardcoded `APP_TIMEZONE` env var rather than
tied to where the user actually is.

### 12.1 Test-data isolation
- [x] Header-tagged at the source (confirmed with the user): `X-Hale-Test: 1` on
      `/api/chat/message` threads an `is_test` bool through `assistant.send_message` ->
      `_persist`/`_build_tools` -> `coach.log_health_note`/`coach.create_workout`. New
      `ChatMessage`/`HealthNote`/`Workout.is_test` columns (`Boolean, default=False`).
      `list_health_notes`/`list_workouts`/`find_related_health_history`/
      `get_health_context_block`/`chat_history` all filter `.is_test.isnot(True))`
      (legacy-NULL rows read as "not test," same convention as `owned_by()`) — this is
      what actually stops pollution, not just the tagging itself.
- [x] **Real design catch**: `_get_client`'s SDK-session cache was keyed only by
      `user_id`. Since `_build_tools`' tool closures capture `is_test` at client-
      creation time, a session built once as real and reused for a later test message
      (or vice versa) would silently stamp every row with the wrong value for the rest
      of that session. Fixed by keying `_clients` on `(user_id, is_test)` instead — as
      a side effect, this also keeps test traffic from ever polluting the real
      conversation's own live in-SDK memory, not just the persisted rows.
- [x] **Migration gap found and fixed**: `models.py`'s `_MIGRATABLE_TABLES` was
      missing `health_notes` entirely (a stale gap predating this list, not a
      deliberate choice) — without adding it, `is_test` would never have reached the
      real production `health_notes` table via `ALTER TABLE`. Corrected the stale
      comment above the list at the same time (it claimed `HealthNote`/`Workout` were
      both "whole new tables" not needing migration, while `Workout` was already
      contradicting that by being in the list below it).
- [x] **One-time cleanup, real data**: found and deleted **5** pre-existing test
      `HealthNote` rows already sitting in real production, each self-identifying in
      its own `notes` field ("Test data from build-verification session…" /
      "Test data from workout-subsystem verification…") — created via direct
      `docker exec ... python3 -c "coach.log_health_note(...)"` testing during earlier
      phases, bypassing the HTTP endpoint entirely (so the header fix alone wouldn't
      have caught them — `CLAUDE.md` now calls this out explicitly as its own risk).
      Verified against a real *copy* of the production DB (mounted into a throwaway
      container, never the live file) before touching anything — confirmed the
      migration path adds the new columns correctly to already-existing tables, not
      just fresh ones, then identified the exact 5 IDs before deleting them for real.
- [x] `CLAUDE.md`: new bullet establishing the convention going forward — any manual
      test of the chat endpoint *or* any direct `coach.log_health_note`/
      `coach.create_workout` call against a real deployment must pass
      `X-Hale-Test: 1` / `is_test=True`.

### 12.2 Browser-detected per-user timezone
- [x] New `User.timezone` column (nullable — `None` means "fall back to the global
      `APP_TIMEZONE`," preserving today's behavior for any pre-upgrade account).
      `util.local_today()` signature changed to `local_today(user_id=None)`, looking up
      that user's stored timezone with the same fallback. All ~21 real call sites
      across `coach/core.py`, `coach/generator.py`, `stats.py`, `sync/garmin_sync.py`,
      `routes/wellness.py` updated to pass `user_id` — enumerated via grep, not
      guessed; every one already had `user_id` in scope as a parameter of its
      enclosing function.
- [x] `GET /api/config` gained a `timezone` field; new `PATCH /api/config` (validated
      against `zoneinfo.available_timezones()`) updates `User.timezone`.
- [x] Frontend: new `useTimezoneSync` hook — on app load, reads
      `Intl.DateTimeFormat().resolvedOptions().timeZone` and PATCHes once only if it
      differs from the already-cached `/api/config` value, mounted via a small
      `<TimezoneSync/>` component at the top of `App.tsx`'s router tree (applies
      regardless of route/demo-gating state).
- [x] Verify: confirmed a real invalid timezone 400s, a real valid one round-trips
      through `GET /api/config`; a live Playwright check against production confirmed
      exactly one `GET /api/config` request and zero `PATCH`es fire on a normal page
      load (the dev browser's own zone already matched the stored value) — no
      unwanted PATCH loop, no console errors.
- [x] Commit (12.1 + 12.2 together, same deploy): "Phase 12.1-12.2: test-data
      isolation + browser-detected timezone"

### 12.3 Challenge safety-vetting
- [x] New read-only `get_exercise_progress` assistant tool (exposes the already-
      existing `coach.get_exercise_progress`) so the coach can check whether an
      exercise already has real progression history before deciding how conservative
      a fresh start needs to be — deliberately read-only, respecting
      `upsert_exercise_progress`'s existing "never directly by a chat tool" boundary.
- [x] New `CHALLENGE_SAFETY_PROMPT` (`coach/core.py`, appended in
      `build_system_prompt`): when a user proposes a self-directed daily/frequent
      challenge, don't validate the raw number — check `get_exercise_progress` first,
      propose a conservative starting point with a defined ramp, and actually
      schedule the safe starting session via `schedule_workout` as a
      `strength_exercise` step (not just describe it) so it's a real prescription
      that later feeds the generator's existing double-progression rule once logged
      through the workout runner.
- [x] **Real gap found and fixed during testing, not theoretical**: `schedule_workout`/
      `update_workout`'s `STEPS_SCHEMA` (in `assistant.py`) only ever described the
      legacy generic step shape — even though `coach._validate_steps` has accepted
      the Phase 4.4 `strength_exercise` shape for a while, the chat tool never
      exposed it to the model, so every chat-scheduled strength session used the
      generic shape and could never show a workout-runner "Start" button or feed
      real `ExerciseProgress` tracking. Fixed with a `oneOf` union covering both
      shapes. That first fix had its own bug, also caught live: neither `oneOf`
      branch restricted `additionalProperties`, so a `strength_exercise`-shaped
      object satisfied the generic branch's only requirement (`exercise` present)
      too, violating `oneOf`'s "exactly one match" rule — every real attempt failed
      tool-input validation with no server-side traceback (the rejection happens
      before it reaches Python), and the model's own retry-with-a-different-shape
      recovery masked the failure in its reply text ("Done, scheduled...") even
      though nothing was actually created. Fixed by adding
      `"additionalProperties": false` to both branches, making them mutually
      exclusive.
- [x] Verify: live-tested against a throwaway container with real
      `--env-file .env` credentials (not a mock) — a "100 pushups a day" prompt
      before the `oneOf` fix silently created nothing (confirmed via
      `GET /api/workouts` returning `[]` despite the model's confident-sounding
      reply); after the fix, a fresh identical prompt produced one clean
      `schedule_workout` call, correctly shaped
      (`stepType: "strength_exercise"`, conservative starting reps, real
      `restSeconds`/`sets`), confirmed via direct `GET /api/workouts` inspection.
      Separately confirmed the legacy generic-step path still works unchanged (a
      real mobility-warmup request produced a correctly-shaped generic-step
      workout) — no regression from the schema change.
- [x] Commit: "Phase 12.3: challenge safety-vetting + strength_exercise chat-tool gap"

### 12.5 Self-review → rolling draft GitHub issue
Scope grew mid-implementation: the user hit a real, live example of the exact gap
this sub-phase exists to close — a detailed workout-UI spec sent to chat got met with
*"I'm getting a product spec here instead of a coaching question... what's the actual
ask?"* instead of being captured. That reframed 12.5 from "periodic background review
only" into two sources feeding the same rolling draft: the periodic historical scan,
**and** a live in-chat classification tool for exactly this case.
- [x] New `CoachIssueDraft` table (`user_id` PK, `title`, `body_markdown`,
      `frustration_count`, `updated_at`, `last_reviewed_chat_message_id` checkpoint) —
      one rolling draft per user, appended to (never overwritten) until cleared.
- [x] New `app/coach/self_review.py`: `append_to_draft` (shared upsert both sources
      below call), `run_for_user`/`run_for_all_users` (periodic path — one-shot
      ephemeral Claude client, no HALE tools, reviews real non-test `ChatMessage`
      history since the checkpoint for coach bugs/gaps, drafts a markdown section or
      "NONE"). First run per user is a full historical scan (no checkpoint yet), by
      design, so the very first draft captures already-known real problems.
      Registered on the scheduler at 04:30 local, right after the generator, skipped
      in demo mode.
- [x] New live tool `log_product_feedback` (`assistant.py`) + `PRODUCT_FEEDBACK_PROMPT`
      (`coach/core.py`): the coach now classifies every message — a bug report/
      feature request/product feedback about HALE itself gets summarized and appended
      to the same rolling draft immediately, with a brief acknowledgment, instead of
      deflecting back to the user. Guarded by `is_test` (Phase 12.1) so verification
      traffic never pollutes the real draft.
- [x] New endpoints `GET /api/coach-issue` / `POST /api/coach-issue/clear`
      (`routes/chat.py`); Settings gained a "Coach Feedback" section (pending count +
      last-updated, "Download as .md" client-side blob download, "Clear").
- [x] **Real bugs caught during testing, not theoretical** — three, in sequence:
      (1) the review's one-shot query passed the raw transcript with no framing, so
      the model treated it as an open-ended request ("I need the actual transcript
      file...") instead of data to analyze — fixed by explicitly framing it in the
      query text; (2) against the real ~90-message production transcript, `max_turns=1`
      cut the model off mid-preamble before it produced any analysis — fixed by
      raising to `max_turns=8` (same headroom the main coaching client already uses,
      for the same reason: this is about response room, not tool-call turns, since
      this client has no tools at all); (3) a leftover preamble sentence ran directly
      into the markdown heading with no line break — fixed by explicitly forbidding
      preamble in the prompt.
- [x] **Small related fix, caught by the user in the same live example**: the coach's
      reply had said "I use the runlog tools" — `BASE_PROMPT` literally named the
      internal `mcp__runlog__*` tool prefix, which the model then echoed verbatim.
      Fixed by describing tools generically in the prompt (the internal MCP server
      name itself is unchanged — purely a prompt wording fix, not a rename).
- [x] Verify: every step live-tested against a throwaway container with real
      credentials, including the exact real user message that prompted this scope
      change (confirmed `log_product_feedback` fires, no deflection, correct
      category/summary); confirmed a genuine coaching question does *not* misfire the
      tool; confirmed `is_test`-tagged feedback never reaches the real draft; confirmed
      append-not-overwrite across multiple items; confirmed the periodic job correctly
      returns nothing on a quiet/verification-only transcript (no false positives) and
      correctly finds and quotes real issues against both a synthetic date-confusion
      exchange and the real ~90-message production history (the exact date-confusion
      bugs originally read at the start of this phase, correctly identified and
      quoted). Screenshotted the real Settings section against live production
      showing the genuine first real draft. Only after every fix was throwaway-verified
      was production redeployed and re-run for the real first draft.
- [x] Commit: "Phase 12.5: self-review + live product-feedback classification"

### 12.5 follow-up — Preview refresh, topic-organized document, data-loss fix
Three more real-usage findings after initial ship, each addressed directly:
- [x] **Mobile Preview UX** (user feedback): downloading as `.md` just triggers a
      save on mobile with no easy way to read it. Added a "Preview" dialog rendering
      the same content in place — first as raw pre-wrapped text, then upgraded to a
      small custom lightweight markdown renderer (`web/src/lib/markdownLite.tsx`,
      covering just the narrow subset this document ever uses — headings, bold,
      bullets, blockquotes — not a full markdown library) once the user pointed out
      the raw `##`/`**` syntax wasn't actually formatted. Added a "Copy all" button
      too, which caught a real bug on its own: `navigator.clipboard` is entirely
      undefined (not just permission-denied) on HALE's actual plain-`http://`
      deployment, since the Clipboard API requires a secure context — fixed with a
      textarea+`execCommand` fallback (`web/src/lib/clipboard.ts`), verified working
      specifically in the no-clipboard-API scenario that matches production.
- [x] **On-demand refresh** (user feedback): the draft previously only updated via
      the once-daily 04:30 job. Opening Preview now also fires
      `POST /api/coach-issue/refresh` (reuses `run_for_user` exactly) so anything
      said since the last check is picked up before it's read — cheap on repeat
      clicks since the existing checkpoint short-circuits before any LLM call
      (confirmed ~0.03s, no duplicate sections, in testing).
- [x] **Generalize recurring findings + a real data-loss bug** (user feedback:
      *"if the same type of thing is logged... the specific log could be
      generalized"*): redesigned the document as topic-organized and meant to be
      handed to an LLM to act on, not a chronological log — a new `_merge_finding`
      LLM step folds a new finding into an existing topic section when it's the same
      underlying issue recurring, synthesized in clear language rather than
      preserving the reporter's exact wording or piling up near-duplicate dated
      entries. **Testing this immediately surfaced a real, serious bug**: this
      session's own heavy testing had exhausted the real Claude subscription's usage
      limit, and the resulting "You've hit your session limit" response came back as
      ordinary-looking reply text — with nothing checking for that, it got trusted
      as the new document body and **silently destroyed the real existing draft**.
      Fixed with explicit `msg.error` checking (mirroring `send_message`'s own
      pattern, which `self_review`'s one-shot calls had never had) plus a content
      sanity check (`_looks_like_real_content` — rejects known limit/error phrasing
      and replies drastically shorter than what they replaced) as defense in depth,
      falling back to a safe append on any failure rather than trusting a suspicious
      response. Verified deterministically (no live LLM call needed) that the exact
      failing message is now rejected and that the no-credentials fallback degrades
      cleanly across repeated calls with zero data loss; the merge mechanism itself
      (send prompt, use reply as new body) was already confirmed working end-to-end
      against a real call earlier in this same testing pass, before the limit hit.
- [x] `log_product_feedback` now `await`s `append_to_draft_async` directly instead of
      routing through the sync-only `_db_call` — the first place in this codebase
      running a second, nested `ClaudeSDKClient` from inside an already-active SDK
      tool-call context (confirmed working live before the rate limit hit).
- [x] Commits: "Coach Feedback: add mobile-friendly Preview dialog", "Coach Feedback
      preview: real markdown rendering + working copy button", "Coach Feedback:
      refresh on Preview click, not just the daily job", "Coach Feedback: generalize
      recurring findings, fix real data-loss bug"

---

## Backlog / not designed this phase
Article/file evaluation (bounded `fetch_article_text` tool + a new file-upload chat
endpoint) — see the approved plan for full design, not built. Video scheduling/
casting stays a single backlog bullet, not designed at all.

---

## Phase 16 — Local AI fallback (Ollama) for Claude usage-limit resilience

**Goal:** stop a Claude usage/rate limit from breaking a live demo or the Chat
tab mid-conversation, without giving up anything about how Claude is used today.

**Real motivation:** a live demo broke because the Chat tab hit a Claude usage
limit mid-conversation — corroborated in production logs the same day this was
scoped (`self_review one-shot call returned an error: rate_limit`).

**Confirmed direction, after weighing a larger alternative:** a full
OpenAI-compatible multi-provider gateway (LiteLLM sidecar, generic
`ai_endpoint_url`/`ai_model_name`/`ai_api_key` settings, removing
`claude-agent-sdk` entirely) was considered and explicitly **rejected** — this
deployment authenticates via `CLAUDE_CODE_OAUTH_TOKEN` (a Claude Pro/Max
subscription, included usage, no metered billing — confirmed via the running
container's real `.env`), which is specifically what `claude-agent-sdk`'s
bundled Claude Code CLI unlocks (see CLAUDE.md's Chat Assistant section). A
generic gateway's Anthropic provider only supports `ANTHROPIC_API_KEY` (metered,
pay-per-token) — there is no way to route a subscription's included usage
through it. Trading non-metered subscription usage for pay-per-token billing
runs directly counter to "don't run out of usage," so instead: **keep
`claude-agent-sdk` and its subscription billing exactly as-is for normal
operation; add Ollama as a same-session automatic fallback that only ever
activates on a real Claude rate-limit/quota error**, not a routing preference.

### 16.1 Docker sidecar
- [ ] Add an optional `ollama` service — its own compose file/profile (e.g.
      `docker-compose.ollama.yml`) so it's opt-in, not a hard dependency for
      anyone who doesn't want a local model running — exposing port `11434`,
      with a small CPU-feasible default model (this NAS has no GPU passthrough
      confirmed available; pick a small model like `llama3.2:3b` at
      implementation time based on real hardware constraints, not assumed here)
      pulled at first run.
- [ ] `app/coach/assistant.py` gets a small Ollama client wrapper — a plain
      `httpx`/`openai`-package call to `http://ollama:11434/v1` (Ollama's own
      built-in OpenAI-compatible endpoint). No LiteLLM/gateway layer needed for
      a single, always-known fallback target — that indirection only earns its
      keep with multiple interchangeable providers, which this phase deliberately
      doesn't need.

### 16.2 Fallback trigger + degraded tool access
- [ ] Detect a real Claude rate-limit/quota error from the Agent SDK response
      (mirroring the `msg.error` check Phase 12.5 already added for exactly this
      failure mode) in `send_message` — on that specific condition only (not any
      other error), retry the same user turn against the local Ollama client
      instead of surfacing a raw error to the user.
- [ ] Decide + implement the fallback's tool-access scope — a real design call,
      not fixed here, bounded by two options: (a) no tools at all (degraded,
      text-only; likely acceptable since rate-limit windows are the exception,
      not routine), or (b) a minimal hand-rolled subset of *read-only* `stats.py`
      tools via Ollama's own tool-calling support. Either way, the fallback must
      never be more capable than Claude's existing tool set, and must never be
      given any write-capable tool.
- [ ] Visibly mark any fallback-generated chat response (a small inline note,
      e.g. "answered via local fallback — Claude usage limit reached") so the
      user always knows when output quality may be reduced, rather than
      silently assuming it's Claude's normal answer — matches this app's
      existing "never let generated content pass as something it isn't"
      discipline (e.g. `goal_progress()` never inventing an "on track" verdict).

### 16.3 Self-review job's own rate-limit handling
- [ ] Decide whether the daily self-review job (`app/coach/self_review.py`)
      should also fall back to Ollama on a rate limit, or simply skip that day's
      run with a clear log line instead — self-review isn't demo-facing, so it
      doesn't need the same urgency as the live Chat path. A real design call at
      implementation time, not fixed here.

### Explicitly out of scope this phase (deferred, not dropped)
- The full OpenAI-compatible multi-provider gateway described above — revisit
  only if a genuinely different/non-Anthropic primary provider is ever needed
  for its own sake, not as a side effect of wanting a local fallback.

### Verification
- Force a real (or mocked) rate-limit condition and confirm: Chat falls back to
  a real Ollama response instead of surfacing a raw error; the fallback response
  is visibly marked as such; normal (non-rate-limited) usage is completely
  unaffected — no added latency or behavior change when Claude succeeds
  normally.

### Critical files
- `app/coach/assistant.py` (fallback wrapper, degraded tool scope)
- `app/coach/self_review.py` (rate-limit handling decision)
- `docker-compose.ollama.yml` (new, optional sidecar service)
- `web/src/components/chat/` (fallback-response visual marker)

---

## Phase 15 — Backend test suite + CI (high priority)

### Context
No test suite exists in this repo (see STATUS.md/CLAUDE.md's "no test suite" note)
— every bug this session found (the cold-start budget math defaulting to a flat
20mi ceiling, `day_share` re-slicing an already-single-session cold-start budget
down to 0.3mi, Run/Ride quick-generate silently overwriting each other via a
shared upsert key, the `oneOf` JSON Schema ambiguity that silently broke every
chat-scheduled strength workout, and the `_find_and_link_workout_run` +/-1-day
window that let yesterday's real run get claimed by two different next-day
workouts) was caught by hand, live, often against real production data. A test
suite is the obvious fix for "how many more of these are already sitting
undetected." Confirmed with the user: start backend-only (pytest unit + API
integration tests, no frontend/E2E yet), running via GitHub Actions.

While scoping this, found `.github/workflows/docker-publish.yml` triggers on
`branches: [main]`, but this repo's actual default branch is `master` — that
workflow has likely never fired on a real push. Fixed alongside the new
workflow's own (correct) branch targeting.

`app/models.py`'s `DB_PATH = os.environ.get("DB_PATH", "/data/runlog.db")` is
read once at module-import time to build `engine`/`SessionLocal` — confirmed
(not assumed) this means a test process can point every route/module at an
isolated temp-file SQLite DB just by setting `DB_PATH` before `app.models` is
first imported, no dependency-injection rework needed in `main.py`/`routes/*.py`.

### 15.1 Test infra setup
- [ ] New `requirements-dev.txt` (kept separate from `requirements.txt`/the
      `pyproject.toml` runtime deps, since these never need to ship in the running
      container): `pytest`, `pytest-cov`, `httpx` (FastAPI `TestClient`'s transport
      dependency).
- [ ] `tests/` directory at repo root, mirroring `app/`'s sub-package layout
      (`tests/coach/`, `tests/sync/`, `tests/routes/`, etc.) so a new test's home is
      unambiguous.
- [ ] `conftest.py`: a session/function-scoped fixture that sets `DB_PATH` to a
      fresh temp file *before* importing `app.models`/`app.main`, calls
      `init_db()`, and yields a `TestClient`. Each test function gets a clean DB
      (either a fresh temp file per test, or a transaction-rollback pattern —
      exact choice is an implementation-time call, not fixed here).
- [ ] Mock/stub external services at the boundary — `strava.py`'s HTTP calls,
      `garmin_sync.py`'s `garminconnect` client, `weather.py`'s Open-Meteo calls,
      `coach/assistant.py`'s Claude Agent SDK client — via `unittest.mock`/
      `monkeypatch`. CI must never make real network calls, need real
      credentials, or depend on third-party uptime/quota.

### 15.2 Unit tests — pure logic first (highest ROI, no mocking needed)
- [ ] `util.py`: GAP/Minetti cost calculation, run-type/interval classifier —
      pin specific input/output pairs that also cross-check against
      `web/src/lib/gap.ts`'s independently-duplicated formula (CLAUDE.md already
      flags this pair as hand-sync'd and prone to silent drift).
- [ ] `stats.py`: `weekly_mileage`/`monthly_mileage`/`personal_records`/
      `rolling_pace_trend`/`training_load_trend`/`readiness`/`goal_progress` —
      deterministic aggregations over synthetic `Run`/wellness rows.
- [ ] `generator.py`: explicit regression tests for each of this session's three
      real bugs by name/scenario — cold-start vs. established-athlete budget
      (`_last_nonzero_week_mileage`/`_compute_weekly_budget`), the `day_share`
      cold-start branch, and Run/Ride's separately-keyed upsert
      (`_existing_generator_workout`/`_upsert_generator_workout`) — plus
      `_auto_pick_strength_template`.
- [ ] `coach/core.py`: `_find_and_link_workout_run`'s exact-day matching (the bug
      just fixed) — a synthetic "real run yesterday, not-yet-attempted workout
      today" scenario must never link, and "real run today" must still link
      correctly.

### 15.3 API integration tests (FastAPI `TestClient` + temp SQLite)
- [ ] Workouts: `POST`/`PATCH`/`DELETE /api/workouts`, all four
      `POST /api/generator/quick/{domain}` domains against both a cold-start and
      an established-athlete synthetic account, `POST /api/generator/run`.
- [ ] Goals: create/update/list, `goal_progress()` for all three goal types.
- [ ] Chat: `is_test` flagging round-trip — the exact Phase 12.1 concern; this
      suite can never accidentally pollute real data by construction, since it
      never touches anything but its own temp DB.
- [ ] Recovery: tool/session CRUD + `_generate_recovery`'s level/duration scaling.

### 15.4 GitHub Actions workflow
- [ ] New `.github/workflows/test.yml` — `on: push`/`pull_request` targeting
      `master` (the real default branch), `runs-on: ubuntu-latest`,
      `pip install -r requirements.txt -r requirements-dev.txt`,
      `pytest --cov=app`. No Docker build step needed here (unlike
      `docker-publish.yml`) — tests run directly against the installed package.
- [ ] Fix `docker-publish.yml`'s stale `branches: [main]` → `master`.

### Explicitly out of scope this phase (deferred, not dropped)
- Frontend unit tests (Vitest) for `web/src/lib/` — deferred to a follow-up
  phase; `gap.ts`'s duplicated GAP formula stays only informally guarded by
  CLAUDE.md's warning comment until then.
- E2E/Playwright in CI — deferred; the existing local `scripts/screenshot.py`
  workflow (see `.RUNBOOK.md`) remains the only visual-verification tool.
- Garmin/Strava real-credential integration tests hitting the actual
  third-party APIs — never planned; CI must never depend on live third-party
  accounts, uptime, or spend real API quota.

### Verification
- Every real bug caught by hand this session gets an explicit, named regression
  test — not just generic coverage of the surrounding function.
- The workflow itself gets verified by actually pushing/opening a PR and
  confirming Actions runs and reports pass/fail correctly, not just that the
  YAML parses.

### Critical files
- `requirements-dev.txt` (new), `tests/` (new), `.github/workflows/test.yml`
  (new), `.github/workflows/docker-publish.yml` (branch fix)
- `app/util.py`, `app/stats.py`, `app/coach/generator.py`, `app/coach/core.py`
  (the modules under initial test)

---

## Phase 13 — Coach quality fixes, Settings/Workouts UX, queryable chat memory

Sourced directly from the Phase 12.5 Coach Feedback draft accumulated on 2026-07-23,
captured here before clearing it (the draft itself is meant to be pulled and worked
from, then cleared — see Phase 12.5's design). The three feature requests in that
draft were one-liners too vague to implement as-is; each was scoped further via
direct questions before being written up below. Nothing in this phase is built yet.

### 13.1 Coach bug fixes (from the automated behavior review)
- [ ] **Date/timeline misattribution**: coach repeatedly confuses which day an
      activity happened on (today vs. yesterday vs. N days ago), and once claimed a
      scheduled workout was already completed when the user hadn't gone yet. Phase
      12.2's per-user timezone fix addresses part of the underlying root cause
      (wrong timezone → wrong "today"); this item is about the coach's own date
      reasoning/prompt-level rigor on top of that — worth reconsidering how
      "today"/"yesterday"/relative-day language gets grounded against real tool data
      *before* the coach states something as fact, rather than only correcting
      after the user pushes back.
- [x] ~~Coach used a test health note as real medical context~~ — already fixed by
      Phase 12.1's `is_test` flagging plus the 5-row real-production cleanup; no
      further action needed, kept here only for the historical record.
- [ ] **Body-side confusion**: coach mixed left/right shin references without
      acknowledging the switch, despite the user reporting bilateral soreness.
      Needs care in how `bodyArea` gets tracked/surfaced across a multi-message
      conversation about a genuinely bilateral issue.
- [ ] **Direct data misreading** ("Coach can't count"): a general accuracy gap
      reading tool output correctly. No specific mechanism identified yet — needs
      more real examples before a targeted fix is possible.
- [ ] **Recovery-tool (Normatec) scheduling mismatch**: a scheduled compression
      level didn't match what was actually logged for a nearby date, and the coach
      accepted the mismatch without reconciling it. `recommend_recovery_session`
      should cross-check existing scheduled/logged sessions (`get_recovery_sessions`)
      before accepting new stated info at face value.
- [ ] **Ambiguous input misinterpreted**: user said "Doing 30 min zone boost on 2 (26
      min remain)" and the coach assumed "2" meant compression level without
      confirming — it could just as plausibly have meant zone or something else.
      Coach should ask rather than assume when a bare number's referent is
      genuinely ambiguous.
- [ ] **Within-session context loss**: coach confused workout mileage with Normatec
      compression settings mid-conversation (both get expressed as small integers
      like "4"), losing track of which domain the conversation was actually about.
      Related to but distinct from 13.4 below — this is losing track *within* one
      active session, not *across* separate sessions.

### 13.2 Settings UI: collapsible section grouping
Confirmed with the user: keep the single Settings page (not a split into sub-tabs,
not just a reorder) — group the existing cards under collapsible/accordion headers
so less-used ones can stay closed by default.
- [ ] Design the actual groupings (which existing `SettingsSection` components in
      `SettingsPage.tsx` belong under which header — e.g. Connections, Training,
      Coach, Account) and add a simple accordion/collapsible wrapper. Pure frontend
      reorganization, no backend changes needed.

### 13.3 Goal-tied multi-week training plan view (Workouts tab)
Confirmed with the user: **not** about relocating the existing Settings → Training
card (the flat per-user `UserTrainingConfig` — max HR, ramp %, mesocycle pattern,
etc.) — this is a new structured "plan" concept tied to a specific goal, surfaced
directly on the Workouts tab with a way to start a new one from there.
- **Likely builds on existing infrastructure rather than starting from scratch**:
  Phase 4.3 already has a `WeeklyPlan` table (`user_id, week_start,
  target_tss`/`actual_tss`, `is_deload`, `frozen`) and a generator that derives
  weekly mileage budgets from a race goal's phase (base/build/peak/taper). This
  request is plausibly about surfacing *that* existing data as a real visual plan
  (a week-by-week view showing target vs. actual, current phase, deload weeks)
  rather than inventing a second, competing planning concept.
- [ ] Needs a real design pass at implementation time to confirm that framing and
      work out the actual UI (calendar view? phase timeline? per-week cards?) — not
      scoped further here.

### 13.4 Queryable chat memory (cross-session context continuity)
Confirmed with the user: explicitly **not** a blanket "re-seed everything on session
reset" (too crude) and **not** full semantic/embedding-based recall either —
speed, token cost, reliability, and a bounded context window were all called out as
important, in that order of emphasis.
- **Proposed direction**: SQLite's built-in FTS5 full-text search extension over
  `ChatMessage.content` (real, non-test history only) — zero new dependencies, no
  embeddings API calls (directly addresses the token-cost/reliability concern an
  external embeddings call would introduce), fast via SQLite's native index, and
  naturally bounded (a query returns a handful of matching messages, not the whole
  history dumped into context). Exposed as a new **on-demand** read-only assistant
  tool (e.g. `search_chat_history(query, limit)`) the coach calls only when it
  actually needs older context — not force-injected into every message the way the
  current per-message health/recovery context blocks are, so a typical turn's token
  cost is unaffected.
- **"Linking things together"**: worth designing the search results to reference
  related entities where relevant (e.g. a matched message about a health issue
  surfacing the linked `HealthNote` id, reusable via the existing
  `get_health_history` tool) rather than just returning raw matched text — exact
  shape needs a real design pass, not detailed further here.
- [ ] Design + implementation not started — deliberately its own future phase item
      rather than rushed into this entry, given the real trade-off decisions
      (FTS5 schema/indexing approach, exactly what the tool returns, how
      aggressively the coach gets prompted to use it) it still needs.

### 13.5 Workout runner: progress indicators, mobile landscape, add-set, faster skip
Follow-up from a shared strength-session-logging mockup. Two ideas from the
original read of that mockup didn't survive user follow-up and are **dropped**:
copy-to-clipboard-for-Chat (the actual set-by-set data is already logged to the
`Workout`/`ExerciseProgress` tables and usable for real progression directly —
no manual copy-paste round-trip needed), and per-set qualitative feedback tags
(not raised again once the user clarified what they actually wanted — cut for
now rather than carried forward speculatively). What's actually wanted,
confirmed directly:
- [ ] **Per-exercise progress indicator**: `WorkoutRunnerPage.tsx` currently only
      shows "Set {position+1} of {totalSets}" — `totalSets` is every set across
      *every* exercise flattened together (a real 5-exercise/3-set session reads
      "Set 1 of 15", giving no sense of which exercise you're on or how far
      through it you are). Add a real "Exercise X of N" indicator plus a
      progress bar/dots — closer to the mockup's `exProgressRow` (dots, one per
      exercise, filled as each completes) and `setProgressRow` (a bar for
      progress through the current exercise's sets) — computed from
      `runnerSets`' existing `stepIndex` grouping, no new data needed.
- [ ] **Mobile landscape layout is bad**: confirmed by screenshot (844×390
      viewport) — the session card stays fixed at `max-w-md`, centered, so
      landscape wastes most of the screen width as empty margin on both sides
      instead of using it. Portrait (390×844) looks fine by comparison. Needs a
      real landscape-specific layout (e.g. a wider card, or a two-column
      layout putting the reps/weight inputs beside the exercise name/target
      instead of stacked) rather than just the same portrait layout stretched.
- [ ] **"Add Set" button**: once an exercise's prescribed sets are all logged,
      offer a way to log one more set beyond what was prescribed before moving
      to the next exercise — for a day the user wants to push past the
      generated prescription. Needs a design decision on how an extra set
      affects `apply_strength_progression`'s "did every set hit target" check
      (`generator.py`) — presumably the extra set shouldn't count against/for
      the progression rule at all, since it wasn't part of the actual
      prescription, but not fixed here.
- [ ] **Skip on the "Get ready" countdown**: `hold` and `rest` sub-phases both
      already have a working Skip/Skip Rest button; the `getReady` sub-phase (the
      5-second countdown before a hold-based set begins) is the one place in
      `WorkoutRunnerPage.tsx` that doesn't — add the same skip affordance there
      for consistency, so every timed phase can be fast-forwarded the same way.
- [ ] **Deferred (explicitly, not dropped): per-exercise demo GIFs.** Confirmed
      with the user this needs "building a space for it" first — i.e. some real
      exercise-media asset storage/hosting (keyed by exercise name, matching
      `STRENGTH_TEMPLATES`' names) rather than just adding an `<img>` tag to an
      existing step — a real infra decision (where assets live, how they're
      keyed/served) needed before this is buildable at all. Revisit once that
      exists; not blocking the four items above.

---

## Phase 14 — Workouts UX: icon-driven Quick Generate + calendar view

The current Workouts tab is entirely form-driven: the only way to get a workout is
either wait for the nightly generator or fill out `WorkoutFormDialog`'s text-heavy
manual form (date picker, a `workoutType` dropdown that always shows every type
regardless of activity, a free-text activity field). The user wants a much friendlier
"press a button, get today's workout" flow instead — icon buttons per activity (Run,
Bike, Strength, Recovery this pass; Yoga deferred, it doesn't fit any existing data
shape yet), no future scheduling from these buttons (today only), plus a calendar-
style view of what's already scheduled/done. Training-plan grouping (mentioned by the
user) is explicitly deferred — it depends on Phase 13.3's goal-tied plan concept,
which isn't built yet.

Confirmed directly with the user across several rounds of scoping:
- **Buttons ship now**: Run, Bike, Strength, Recovery.
- **Generation respects real periodization**: pressing a button produces a properly
  periodization-aware prescription (Phase 4.3's weekly-budget/phase/readiness-gate
  logic), not an ad-hoc guess — it's "give me today's, right now," never future dates.
- **Calendar is additive**: a List/Calendar toggle; calendar is the default view.
- **Pace/target units are activity-dependent**: min/mi for Run, mph for Bike (not a
  full metric-vs-imperial app-wide toggle this phase — see 14.5 below).
- **Per-activity historical tracking, and a real cold-start problem**: distance/
  speed/HR baselines must be tracked *per activity type*, and a user experienced at
  one activity (e.g. a marathoner) but brand new to another (their first-ever bike
  ride) must not get a prescription sized for an established athlete in that
  activity — needs a real build-up/beginner ramp, not the existing phase-ceiling math.
- **Strength targeting**: quick-generated strength shouldn't always be the same
  generic full-body rotation — either complementary to the user's other training, or
  an explicit user-chosen focus (their example: "back and legs").

### 14.0 Real bug found while scoping this (not Bike-specific, not theoretical)
`generator._get_or_create_weekly_plan`'s existing budget calc:
```python
ceiling = last_week_mileage * PHASE_CEILING_MULTIPLIER.get(phase, 1.15) if last_week_mileage > 0 else 20.0
budget = min(uncapped, ceiling) if last_week_mileage > 0 else ceiling
```
When `last_week_mileage` is `0` (genuinely no history in that activity — not just a
rest week), the ceiling silently defaults to a **flat 20 miles**, regardless of phase
or actual experience — exactly the "handing a brand-new rider a 20-mile first ride"
failure the user described, and not specific to the new Bike domain either: a
genuinely new HALE user with zero synced running history hits the same fallback
today. `_week_mileage` also hardcodes `Run.activity_type == "Run"`, so it can't
currently distinguish "no run last week but a real running history" from "no history
in this activity at all."
- [x] Distinguish those two cases explicitly: an *established athlete with just no
      mileage last week* (real history exists in this activity_type over a longer
      lookback) keeps today's ceiling-multiplier behavior, based off the most recent
      *nonzero* week instead of a hardcoded 20; a *genuine cold start* (near-zero
      history in this activity_type at all) gets a small fixed conservative starting
      budget (e.g. 2–3 mi or ~20–30 min) with a defined linear weekly increment
      (matching the user's own framing, "should build up or time based increases" —
      the same "start small, ramp by a fixed amount" philosophy Phase 12.3's
      strength challenge-safety logic already established, just as deterministic
      generator math instead of chat/LLM-driven) rather than multiplying off zero.
      Benefits Run and Ride equally and is a prerequisite for Ride even existing as
      a sane quick-generate option. Shipped as `_last_nonzero_week_mileage`/
      `_compute_weekly_budget` in `app/coach/generator.py`. A second real bug was
      caught during live verification of this fix: `day_share`'s weekly-total-slice
      math (`budget * share`) silently produced 0.3mi "first runs" when applied to a
      cold-start budget that's already a single-session distance — fixed by
      branching on `is_cold_start` to use `budget` directly in that case.

### 14.1 Backend: `run_quick_generate` + cold-start fix + endpoint — done
- [x] Generalize `_week_mileage`/`_get_or_create_weekly_plan` to take `activity_type`,
      implementing the cold-start-vs-established distinction from 14.0.
- [x] Thread `activity_type` consistently through the `stats.py` functions the
      endurance path leans on — `weekly_mileage`/`monthly_mileage`/`personal_records`/
      `run_summary` already accept it; `rolling_pace_trend` and `training_load_trend`
      currently don't (confirmed via direct read, not assumed) and need it added so a
      per-activity pace/load baseline is actually possible.
- [x] New `run_quick_generate(db, user_id, domain, date=None) -> dict`, `domain` in
      `{"run", "ride", "strength", "recovery"}`:
      - `"run"`/`"ride"`: calls `_generate_endurance` (generalized to accept
        `activity_type`, using the fixed cold-start-aware budget logic) forcing
        **today** regardless of the day-of-week skeleton — the button overrides
        *which* day gets a session; the actual prescription (type/distance/pace-or-
        speed target) still comes from the real phase/budget/readiness-gate/cold-
        start logic.
      - `"strength"`: calls `_generate_strength`, forcing today's occurrence
        regardless of `WEEKDAY_STRENGTH_SLOTS`, with an optional `template_override`
        param (see 14.2).
      - `"recovery"`: new thin wrapper around `coach.recommend_recovery_session` —
        auto-picks the user's only/most-recently-used `RecoveryTool` (via
        `list_recovery_tools`) and a level/duration scaled by the current
        `stats.readiness()` flag count (more flags → higher level/duration, within
        that tool's supported range/increment), mirroring
        `RECOVERY_GUIDANCE_PROMPT`'s existing escalation logic for the coach itself.
      - Idempotent per (user, date, domain) via the existing
        `_upsert_generator_workout`/domain-keyed pattern — pressing a button twice in
        one day regenerates that domain's entry rather than duplicating it. A real
        bug was caught here too during verification: Run and Ride both used the same
        internal `domain="endurance"` upsert key, so quick-generating Ride right
        after Run silently overwrote the Run row instead of creating a separate one
        — fixed by giving non-Run activities their own suffixed key
        (`endurance_<activity>`) and teaching `_existing_generator_workout` to match
        on `activity_type` for that family, while Run keeps the original unsuffixed
        `"endurance"` key for backward compatibility with the nightly auto-generator.
- [x] New endpoint `POST /api/generator/quick/{domain}` (`routes/workouts.py`) — no
      date param exposed; always today, matching "I don't want to future-schedule it."

### 14.2 Strength targeting — activity-complementary default + explicit override
`STRENGTH_TEMPLATES` (`generator.py`) already supports multiple named templates keyed
by a target area, each exercise already tagged with a `category` (squat/push/pull/
core/hinge, which the existing progression-increment logic already keys off) — this
extends that same, already-bounded-v1 pattern rather than building a new system.
- [x] Add 2–3 more named templates reusing the existing categories (no new increment
      logic needed) — e.g. a runner/rider-complementary template (glute/hip/core/
      hinge-heavy, supporting running/cycling economy and injury prevention) and a
      "back and legs" template (pull + hinge + squat-focused). Exact exercise picks
      are a content decision at implementation time, same as how `full_body_ab`'s
      original 10 exercises were chosen. Shipped as `runner_focus` and
      `back_and_legs` in `STRENGTH_TEMPLATES`.
- [x] `run_quick_generate`'s `"strength"` domain accepts an optional
      `template_override` — when omitted, auto-picks based on the user's recent
      Run/Ride volume (real cardio history in the trailing few weeks → the
      complementary template; otherwise the existing `full_body_ab` default) rather
      than always defaulting to full-body. Shipped as `_auto_pick_strength_template`;
      verified against both a zero-cardio-history account (falls through to
      `full_body_ab`) and a real ~25mi/week runner (auto-picks `runner_focus`).
- [x] Frontend: the Strength quick-generate button offers a lightweight target
      picker (a small chip/dropdown row — Full Body / Runner Focus / Back & Legs /
      …) shown right after tapping it, pre-selected to the auto-picked default, so
      the common case is still nearly one-tap while explicit choice stays available.
      Shipped in `QuickGenerateBar.tsx`; a real gap was caught during live click-
      through verification: the endpoint didn't expose `template_override` as a
      query param yet (an uncommitted edit hadn't been deployed), so every chip
      click silently kept re-generating the same auto-picked template — fixed by
      redeploying, then re-verified live that "Back & Legs" actually changes the
      persisted workout.

### 14.3 Frontend: `QuickGenerateBar` — done
- [x] New `web/src/components/workouts/QuickGenerateBar.tsx` — icon+label buttons
      (lucide-react, already in use elsewhere: `Footprints` Run, `Bike` Ride,
      `Dumbbell` Strength, an icon for Recovery), each POSTs to the new endpoint for
      today and invalidates the workouts/recovery-sessions queries on success.
      Per-button loading state. No second manual "which type" step for Run/Ride —
      the backend already picks easy/tempo/interval/long via the real periodization
      logic; overriding the result still goes through the existing
      `WorkoutFormDialog` edit flow. Verified live against production (via the
      NAS-hosted Vite dev server, port 5173, since the sandboxed browser tool can't
      reach the LAN): auto-pick, explicit override, and idempotent re-press all
      confirmed correct on real account data.

### 14.4 Frontend: `WorkoutsCalendar` + List/Calendar toggle — done
- [x] New `web/src/components/workouts/WorkoutsCalendar.tsx` — month-grid view, each
      day cell showing small activity icons (same icon set as 14.3) colored by
      `WORKOUT_STATUS_COLORS`. Clicking a day expands that day's items, reusing the
      existing `WorkoutCard`/`RecoverySessionCard` and `WorkoutsPage.tsx`'s existing
      workout-vs-recovery-session `Item` union type. A List/Calendar segmented
      toggle sits above it; Calendar is the default. Verified live against
      production data (NAS-hosted Vite dev server + Playwright click-through):
      month prev/next navigation, day selection, today's default selection, and
      the List toggle all confirmed working.
- [ ] Training-plan grouping (collapsible dropdown per plan): **not built this
      phase** — deferred until Phase 13.3 ships.

### 14.5 Frontend: `WorkoutFormDialog` activity-conditional fields — done
- [x] `activityType` becomes a small fixed `Select` (Run/Ride/Strength/Recovery/
      Other) instead of free text, so downstream logic has something reliable to
      key off. The `workoutType` dropdown's options become conditional on it
      (`easy`/`tempo`/`interval`/`long` for Run/Ride; `strength` only for Strength;
      `rest`/`cross_train` otherwise). "Strength"/"Recovery" are UI-only categories
      (mapped to/from the real `activityType`+`workoutType` at the form's edges) —
      strength workouts still persist `activityType="Other"`, matching the existing
      generator convention. Verified live: editing a real Strength workout (Deadlift/
      Bulgarian Split Squat/... with real logged sets) correctly derives category
      "Strength" and round-trips the exercise editor; editing a real Garmin-synced
      Run correctly derives "Run".
- [x] The pace/target field becomes unit-aware: `min:sec/mi` for Run, `mph` for
      Ride — stored internally however is simplest (e.g. keep
      `targetPaceSecPerMi`'s existing semantics for Run; for Ride, convert the
      entered mph to the equivalent sec-per-mile before saving, so the backend
      keeps one consistent unit and only the *display/entry* layer is
      activity-aware) rather than adding a second backend field. Verified live:
      switching Activity to Ride relabels the field to "Target speed (mph)".
- [ ] **Deferred, explicitly out of scope this phase**: full metric-vs-imperial unit
      preference (km, km/h, kg, °C) was raised but is a much larger cross-cutting
      change — this app hardcodes imperial units everywhere today (miles, mph, lb,
      °F), not just in Workouts. This phase stays imperial (mph for Ride); the
      metric toggle is its own future backlog item, not silently dropped.

### 14.6 New Workout flow: unify entry points, preview before confirm — done
The old always-visible `QuickGenerateBar` fired immediately on click (nothing to
back out of), and the separate "+ New Workout" button jumped straight to the
free-form manual dialog — two different entry points for "create a workout."
Confirmed with the user: one flow instead — pick a type (or Custom) → preview
the real computed prescription → Confirm actually saves it, Cancel/Back discards
nothing-was-written.
- [x] **Backend**: threaded `dry_run: bool = False` through `run_quick_generate`
      → `_generate_endurance`/`_generate_strength`/`_generate_recovery` →
      `_upsert_generator_workout`. `dry_run=True` skips the DB entirely (no
      existing-row lookup, no create/update) and returns the same dict shape a
      real call would, so the frontend renders an identical preview either way.
      `POST /api/generator/quick/{domain}?dry_run=true` added as a query param on
      the existing endpoint — no new endpoint needed. Verified: all 4 domains'
      dry-run preview writes nothing (confirmed via `GET /api/workouts`/
      `/api/recovery-sessions` staying empty), and a dry-run preview followed
      immediately by a real call produces byte-identical field values (minus
      `id`/`createdAt`), proving the preview isn't lying about what Confirm will do.
- [x] **`WorkoutCard`/`RecoverySessionCard`** gained an optional `preview` prop
      (default `false`, fully backward-compatible) that hides the status badge
      and all actions (Start/Edit/Delete/Mark Done/Skip) — reused for rendering
      the preview step instead of duplicating their step-rendering logic in a
      separate component.
- [x] **New `NewWorkoutDialog.tsx`** replaces `QuickGenerateBar.tsx` (deleted)
      and the old direct "+ New Workout" → `WorkoutFormDialog` wiring. Two steps:
      "pick" (Run/Ride/Strength/Recovery buttons; Strength expands the existing
      template chip row inline, any chip pick — including "Auto" — fires the
      preview fetch; a "Custom" button closes this dialog and opens the existing
      `WorkoutFormDialog` unchanged, skipping preview entirely since the manual
      form's own Save button *is* the confirm step) and "preview" (renders the
      dry-run result via `WorkoutCard`/`RecoverySessionCard` with `preview`,
      Back returns to "pick" preserving the chosen type/template, Confirm fires
      the real `dry_run=false` call and closes on success).
- [x] Verified live end-to-end against production via Playwright: pick→preview
      for Run and for Strength-with-template-chips, Back preserving selection,
      a real Confirm (via the idempotent Recovery domain, low-risk to test for
      real) actually saving and refreshing the calendar, and Custom correctly
      opening the manual form.

### Verification (all sub-phases)
- 14.1: force-call the new endpoint for each of the 4 domains against a throwaway
  container across synthetic states — a true cold-start account (no Ride history at
  all) must get the small conservative starting distance, not a flat 20mi; an
  established-runner account pressing "Run" must still get real phase/budget-driven
  output unchanged from before this change; confirm idempotency (pressing twice same
  day doesn't duplicate).
- 14.2-14.5: `tsc -b`/`oxlint`/`npm run build`; Playwright click-through of each
  Quick Generate button + the resulting workout appearing correctly; calendar view
  screenshotted at desktop+mobile; confirm the activity-conditional dropdown and
  mph-vs-min/mi field behave correctly per activity in `WorkoutFormDialog`.
- Standard discipline throughout: throwaway container first, never touch the real
  production container until verified; update this section as each sub-phase lands.

### Critical files
- `app/coach/generator.py` (`_week_mileage`/`_get_or_create_weekly_plan` cold-start
  fix, generalized `_generate_endurance`, new `run_quick_generate`, new strength
  templates)
- `app/stats.py` (`activity_type` added to `rolling_pace_trend`/`training_load_trend`)
- `app/coach/core.py` (small adjustment for the Recovery auto-default wrapper, if any)
- `app/routes/workouts.py` (new endpoint)
- `web/src/components/workouts/QuickGenerateBar.tsx` (new),
  `web/src/components/workouts/WorkoutsCalendar.tsx` (new),
  `web/src/components/workouts/WorkoutFormDialog.tsx`,
  `web/src/pages/WorkoutsPage.tsx`, `web/src/lib/api.ts`, `web/src/hooks/useWorkouts.ts`

---

## Phase 6 — Training-load analytics

Picked as the next feature specifically for visual impact (user's own framing) —
a PMC (fitness/fatigue/form) chart is the centerpiece; gear tracking (6.3) was
bundled in on request even though it isn't itself a training-load/visual feature.

**Real finding that shapes 6.1's design**: this account's `UserTrainingConfig` has
`thresholdHr`/`maxHr` both null (confirmed via `GET /api/training-config`), which
would normally block hrTSS entirely. But `garminconnect.Garmin.get_lactate_threshold()`
— already available in the pinned `garminconnect` version, no new OAuth scope,
purely read-only — returned **real, genuine data for this account**: running LTHR
173 bpm (estimated 2026-06-30, cycling 172 bpm), confirmed by an actual authenticated
call during scoping, not assumed. The endpoint's paired "speed" field (0.347, units
unclear — converts to an implausible ~1.2 km/h) looks unreliable and is **not** used;
only the heart-rate values are trusted. This means hrTSS can be the primary metric
from day one via an **auto-populate-if-unset** step (never silently overwrites a
value the user has manually entered in Settings' existing Threshold HR field),
rather than requiring the user to fill it in first or falling back to a fully
pace-based rTSS formula for every run.

Real data coverage checked before designing the fallback: 461/530 runs (87%) have
`avg_hr`; only 136/530 have `avg_power_watts`, and those that do mix real cycling
power with Garmin's running-power estimates (confirmed via `get_lactate_threshold`'s
paired power query being sport-scoped to `RUNNING` specifically — a running "FTP"
of 357W is not a cycling FTP and the two aren't interchangeable). Given this, NP/IF/
variability-index/aerobic-decoupling (the power-based ride metrics) are **scoped
down to opportunistic-only** — computed for a Ride only when it has real
`avg_power_watts`, no attempt to derive/estimate power from pace, and not blocking
anything else in this phase if a cycling FTP is never set.

### 6.1 Per-activity metrics (sync-time, stored on Run)
- [ ] **Auto-populate threshold HR from Garmin**: during `garmin_sync.py`'s sync
      flow (or a small standalone one-shot call, whichever fits the existing sync
      structure better at implementation time), call `get_lactate_threshold()` and,
      **only if `UserTrainingConfig.threshold_hr` is currently null**, set it from
      the response's running heart rate value. Never overwrites a value the user
      already set manually in Settings.
- [ ] `tss` column on `Run`: hrTSS (`(duration_hr * avg_hr_relative_to_threshold^2) * 100`
      pattern, standard TrainingPeaks-style formula) when both `avg_hr` and a real
      `threshold_hr` exist; otherwise a simpler fallback using the existing
      `classify_run_type` heuristic's easy/tempo/interval/long bucket as a fixed
      per-type intensity factor against duration (documented v1 approximation, not
      a full grade-adjusted-pace TSS model — avoids inventing a second unverified
      "threshold pace" reference given the Garmin speed field's unreliability noted
      above). `efficiency_factor` (avg pace or power / avg HR) wherever HR exists.
- [ ] For rides with real `avg_power_watts`: `normalized_power` (30s rolling
      4th-power mean from streams, where available), `intensity_factor` (needs a
      cycling FTP — from `UserTrainingConfig.ftp_watts` if the user has set it,
      otherwise skipped, not estimated), `variability_index`, `aerobic_decoupling`.
- [ ] Backfill command for existing activities (one-shot, container-run) — all 530
      real runs, so the PMC chart has real historical depth from day one rather
      than starting from a flat line.
- [ ] Verify: hand-check the hrTSS formula against a couple of real runs with known
      HR/duration before backfilling everything; confirm the Garmin auto-populate
      step doesn't fire (and doesn't overwrite) once threshold_hr is already set.
- [ ] Commit: "Phase 6.1: per-activity TSS/EF (+ NP/IF/VI/decoupling where power
      exists) + Garmin LTHR auto-populate"

### 6.2 PMC pipeline
- [ ] `DailyMetrics` table: `(user_id, date) PK, trimp, ctl, atl, tsb,
      hrv_baseline_ms, readiness_score, time_in_zone_json, computed_at`
- [ ] `app/pipeline.py` nightly job: TRIMP→CTL (42d) / ATL (7d) / TSB; weekly
      actual_tss into `weekly_plan`; `stats.readiness` switches acuteChronicRatio
      to ATL/CTL; strength tonnage → TRIMP via fixed intensity factor (documented
      v1 approximation)
- [ ] `GET /api/metrics?days=` + Insights CTL/ATL/TSB chart — the actual
      visual-impact payoff this phase was picked for; a real multi-line trend
      chart (fitness/fatigue/form over months) using `dataviz` skill guidance for
      styling/palette, backed by the real backfilled TSS history from 6.1.
- [ ] Verify: hand-check CTL/ATL recursion on a known 5-day window
- [ ] Commit: "Phase 6.2: PMC pipeline (CTL/ATL/TSB)"

### 6.3 Gear tracking
- [ ] `Gear` table (`id, user_id, name, kind shoe|bike|bike_component,
      parent_gear_id?, start_date, retired_date?, replace_at_mi?`) +
      `Run.gear_id`; default-gear-per-type rule at sync; `stats.gear_summary`
      (read-time mileage); CRUD + Settings UI + wear on dashboard
- [ ] Commit: "Phase 6.3: gear lifecycle tracking"

---

## Phase 7 — Geospatial pipeline

- [ ] 7.1 `h3` dep + `RouteHex` table (`(user_id, hex_id, res) PK, sport, first_visited,
      visit_count, sum_speed/sum_hr/sum_sec/n`); sync-time hex upsert (run→res 9,
      ride→res 7, both→res 8) + one-shot backfill over existing activities
- [ ] 7.2 `GET /api/spatial/heatmap?sport&year&metric&bbox&zoom` → GeoJSON from
      aggregates (precomputed = fast; no tile server)
- [ ] 7.3 Map layers: separate toggleable Run (crimson/orange) vs Ride (cyan/blue)
      heatmaps; weight = speed (ride) / time-in-cell or HR (run)
- [ ] 7.4 Fog of War: `GET /api/spatial/exploration?region` (unique res-9 hexes / region
      bbox) + cleared-fog map layer + dashboard stat
- [ ] 7.5 Climb detection at sync: smoothed elevation, ≥3% sustained ≥300m segments,
      length×grade → Cat 4…HC → `Run.climbs_json`; rolling-grade histogram ×
      speed/HR/power → `Run.grade_analysis_json`; surface in run expand + Insights
- [ ] 7.6 OSM surface tags (Overpass, throttled + cached, degrade-to-null) →
      `Run.surface_json`
- [ ] 7.7 Wind: extend existing Open-Meteo call with wind speed/direction; mean
      route bearing vs wind → `Run.wind_json {headwindPct, avgHeadwindMph}`
- [ ] 7.8 Privacy zones: table + Settings CRUD; **read-time** redaction in route
      output (raw stays stored)
- [ ] Verify each: backfill on DB copy; screenshot heatmap layers; spot-check a known
      hilly run's climbs against Strava's segment data
- [ ] Commit per sub-task

---

## Phase 8 — Configurable dashboard

- [ ] Layout config in `sync_meta` (`user_key(uid,"dashboard_config")`) —
      `{widgets:[{id, pos, visible}]}`; `GET/PUT /api/dashboard/config`
- [ ] Extend `/api/dashboard/summary` with per-widget keys (readiness, pmc,
      todayWorkout+push state, weeklyRamp, gear, exploration, wellness, goals,
      records) — compute only active widgets
- [ ] Frontend: widget rendering from config, visibility toggles + reorder (up/down
      v1, no drag-grid)
- [ ] Verify: toggle/reorder round-trip; screenshot
- [ ] Commit: "Phase 8: configurable widget dashboard"

---

## Phase 9 — Credentials & nutrition

- [ ] 9.1 `app/crypto.py` (AESGCM, `ENCRYPTION_KEY` env, plaintext fallback with
      startup warning); migrate `ProviderCredential.password` to encrypted-at-rest
- [ ] 9.2 Per-user LLM keys (`provider="anthropic"|"openai"` rows, encrypted);
      `assistant.py` prefers user key over system env; Settings UI (masked)
- [ ] 9.3 Nutrition schema: `NutritionLog (id, user_id, ts, meal_name, calories,
      protein_g, carbs_g, fat_g, source)`, `MacroTarget (user_id PK, …)`,
      `DeliveryImport (id, user_id, provider, imported_at, item_manifest_json)`
- [ ] 9.4 `POST /api/nutrition/import` manifest upload parser (CSV/HTML — best-effort,
      Garmin-ZIP-import pattern) + manual log CRUD + daily macro summary in stats
- [ ] 9.5 LEA flag in `stats.readiness`: 7d intake < 0.85 × (BMR est + activity kcal),
      only when logging coverage ≥5/7 days; generator treats as one flag; two
      consecutive weeks → cap freeze
- [ ] Commit per sub-task

---

## Phase 10 — Vitals & biomarkers

- [ ] 10.1 (done in 2.1/2.2 + 3.3 — glucose ingest end-to-end; verify here and mark)
- [ ] 10.2 `LabPanel` table (`id, user_id, lab_date, source, markers_json`); manual
      CRUD + Settings UI (PDF parsing explicitly deferred)
- [ ] 10.3 Sticky lab flags in readiness (`ferritin_low`, `crp_elevated`,
      `glucose_instability` TIR<70% 7d) — act as ramp-cap ceilings (0% increase),
      not daily downgrades; persist until next panel; rationale named in notes
- [ ] Commit per sub-task

---

## Phase 2 — Telemetry ingest API

### 2.1 Schema
- [ ] `HealthSample` table: `id (client "{device}:{record_id}" → idempotent), user_id,
      kind (steps|sleep_session|hrv|resting_hr|heart_rate|blood_glucose), start_ts,
      end_ts, value_json, device_id, received_at` — raw kept forever
- [ ] `daily_steps` adds `hrv_last_night_avg_ms`, `glucose_tir_pct`,
      `field_sources_json` (per-field provenance; precedence garmin > health_connect)
- [ ] Commit: "Phase 2.1: health_samples schema + wellness provenance columns"

### 2.2 Endpoint
- [ ] `app/ingest.py` + `POST /api/ingest/health-connect`: batch INSERT OR IGNORE,
      rollup touched dates into daily wellness respecting precedence; device-token auth
- [ ] Glucose rollup: link readings to overlapping Run windows → `Run.glucose_json`;
      daily time-in-range (70–180 default) → `glucose_tir_pct`
- [ ] Verify: curl a synthetic batch twice → second reports duplicates, rollup correct
- [ ] Commit: "Phase 2.2: Health Connect ingest endpoint + rollup"

---

## Phase 5 — Garmin workout push

- [ ] `app/garmin_push.py`: endurance steps → garminconnect 0.3.6 workout model
      (hr_zone→HR target via UserTrainingConfig, pace→m/s, repeat blocks); reuse
      `garmin_sync._login` + cooldown wrapper; `push_workout` (upload + schedule,
      store `garmin_workout_uuid`), `unpush_workout`; 429 → `Workout.push_error`
      (new column), never crashes the scheduler. All garminconnect workout types
      isolated in this one module (FIT-file generation is the documented escape hatch)
- [ ] `POST /api/workouts/{id}/push`; `User.auto_push_garmin` flag (default false)
      auto-pushes generator output; "Push to Garmin" button on workout cards
- [ ] Verify: real push of one workout; confirm on watch/Connect; unpush cleans up
- [ ] Commit: "Phase 5: Garmin workout push pipeline"

---

## Phase 3 — Android client (`android/`, after ingest contract freezes)

- [ ] 3.1 Gradle scaffold: minimal Compose single-activity (server URL, device token,
      HC permission grant, last-sync status) — headless-first, no dashboards
- [ ] 3.2 Room: `QueuedSample(id PK, kind, startTs, endTs, valueJson, queuedAt,
      uploadedAt?)`, `ChangesToken(recordType PK, token)`
- [ ] 3.3 Health Connect source — **read-only** (READ_STEPS/SLEEP/HRV/RESTING_HR/
      BLOOD_GLUCOSE, never WRITE): Changes API loop per type, token persisted
      transactionally with its batch; expired-token fallback = 30-day re-baseline
- [ ] 3.4 WorkManager: 15-min periodic (network-required, exponential backoff) —
      drain HC → Room, upload batches ≤500 to `/api/ingest/health-connect`
      (X-Api-Token), prune uploaded >7d
- [ ] 3.5 `SensorSource` interface (future BLE) — interface only
- [ ] Verify: end-to-end real phone → NAS: steps/sleep/HRV land in daily wellness
- [ ] Commit per sub-task; final: "Phase 3: Android Health Connect client"

---

## Infra: `app/` restructured into a real installable package

Not tied to a numbered phase — triggered while trying to deploy the Phase 11 demo
container on a third-party free-tier PaaS (SnapDeploy). Its build pipeline kept
misbehaving against this app's original flat-directory `app/` layout in ways that
had nothing to do with this app's own code: a false "requires PostgreSQL" gate (a
naive text scan matched a *rejected*-migration mention in this very file), an
over-eager env-var gate treating every optional credential in `.env.example` as
required, and — the one that prompted this restructuring — its dependency
auto-detection inventing a phantom pip package called `garmin_sync` from a plain
`import garmin_sync` statement in the source, overriding the real, correct
`requirements.txt` it was supposedly building from.

- [x] `app/__init__.py` + root `pyproject.toml` (mirrors `requirements.txt`'s pins
      as `dependencies`, `[tool.setuptools] packages = ["app"]`) — makes `app` a
      real installable package, `pip install .` registers it in site-packages
      rather than relying on implicit CWD-based top-level-module resolution
- [x] Every internal cross-module reference across all 12 backend modules
      converted from a bare `import coach`/`from models import X` to a relative
      `from . import coach`/`from .models import X` — including the many
      function-local lazy imports scattered through `main.py` (dozens of them, at
      varying indentation levels). Done via a small regex script rather than by
      hand, given the volume — verified afterward that zero bare internal-module
      imports remained anywhere in `app/*.py`
- [x] Caught and fixed a **real latent bug** this surfaced, independent of the
      restructuring's own correctness: `main.py` mixed `__file__`-relative
      (`WEB_DIST_DIR`) and CWD-relative (`directory="static"`, twice) path
      resolution for its two static-file mounts. This only ever worked by
      coincidence, because the old Dockerfile's `WORKDIR`/`COPY` structure happened
      to keep CWD and the module's own directory identical — a latent fragility,
      not something this restructuring introduced. Fixed by making both
      `__file__`-relative (`STATIC_DIR`), which is correct regardless of process CWD
- [x] `Dockerfile`: Python stage `WORKDIR`s at `/srv` (the package's parent, not the
      package itself), copies `requirements.txt`+`pyproject.toml`+`app/` in,
      installs deps then `pip install --no-deps .`; `web-dist` now copies to
      `./app/web-dist` (a sibling of `main.py` *inside* the package, matching its
      `__file__`-relative resolution). `docker-entrypoint.sh`'s uvicorn target
      changed from `main:app` to `app.main:app`
- [x] Verify: **never touched the real running production container until fully
      verified separately** — built the image (build-only, doesn't restart the
      live container), ran a throwaway container from it on a different port,
      confirmed clean startup, home/legacy/SPA-fallback routes all 200, and the
      deep `main → generator → coach → stats → models` relative-import chain
      resolving correctly end-to-end (a real `POST /api/generator/run` call
      against the throwaway instance). Only after that passed was the real
      production container recreated with the same verified image — confirmed
      real data untouched (144 runs, 5 goals, correct `/api/config`) and a Home
      tab screenshot rendering exactly as before
- [ ] **Not yet confirmed**: whether this actually fixes SnapDeploy's specific
      build pipeline — that requires an actual redeploy attempt there, which is
      the user's own next step, not something verifiable from this environment

---

## Infra: backend reorg — domain sub-packages + `main.py` router split

Follow-up to the flat→package restructuring above: the user asked for the
*internal* organization to also "make sense for understandability and
maintenance as well as addition and classification of new features," not just
"it's a package now." Two-stage pass, done in full rather than deferred
(explicit call: "full pass now, we have git if we mess up, although we
shouldnt rely on it").

**Stage 1 — domain sub-packages (done):**
- [x] `app/sync/` (`strava.py`, `garmin_sync.py`, `garmin_import.py`,
      `weather.py` — external ingestion), `app/coach/` (`core.py` — renamed
      from the old top-level `coach.py` to avoid a `coach/coach.py` stutter —
      plus `generator.py`, `assistant.py`), `app/accounts/` (`auth.py`,
      `demo.py`, `seed_engine.py`). `models.py`/`util.py`/`stats.py`/`push.py`/
      `main.py` stay top-level as cross-cutting concerns, not owned by one
      domain. Moved via `git mv` to preserve history.
- [x] Every cross-reference updated by hand per the exact new relative depth
      (same-package sibling vs. `..` up to a top-level module vs. a different
      sub-package) — `coach.py`'s old call sites keep working unchanged via
      `from .coach import core as coach` / `from . import core as coach`
      aliasing, so no call site needed renaming. Verified afterward: zero
      remaining references to the old flat module paths anywhere in `app/` or
      `scripts/`.
- [x] `pyproject.toml`'s `packages` list extended to `["app", "app.sync",
      "app.coach", "app.accounts"]`.
- [x] Verify: same never-touch-prod-first discipline as the flat→package
      restructuring — build-only, throwaway container on a different port,
      curled one endpoint per domain (Strava status, Garmin status, generator
      run exercising the full `main → coach.generator → coach.core/stats`
      import chain, workouts, demo status, dashboard summary, training-config)
      all green, clean startup logs with both scheduler jobs registered, only
      then recreated the real production container and reconfirmed real data
      (144 runs, 5 goals, Strava still connected) untouched.

**Stage 2 — split `main.py` into `routes/` (done):**
- [x] `main.py`'s 1311 lines / 55 route decorators (54 API endpoints + the SPA
      catch-all) split into 9 `app/routes/*.py` files per the approved mapping
      table (auth, sync, settings, wellness, chat, health, workouts, goals,
      dashboard) — confirmed byte-for-byte identical path coverage via a
      before/after diff of every `@app.`/`@router.` decorator across the old
      file and the new routers + `main.py`'s remaining catch-all.
- [x] `_record_sync`/`_refresh_dashboard_cache`/`DASHBOARD_CACHE_KEY`/
      `DASHBOARD_CACHE_UPDATED_AT_KEY` moved into `stats.py` (renamed to public
      `record_sync`/`refresh_dashboard_cache` since they're now called from
      `routes/sync.py` and `routes/dashboard.py` across a module boundary) —
      the cross-cutting exception the plan called for, since dashboard-cache
      state belongs next to `dashboard_summary()`, not stranded in either
      individual router.
- [x] `main.py` shrank from 1311 lines to ~105 — app instantiation, both
      middlewares, the `startup()` event (now importing `routes.sync`'s
      `_auto_sync`/`_next_auto_sync_time` and `coach.generator`), and the
      `/legacy` + `/assets` + SPA-fallback static serving are all that's left.
- [x] `pyproject.toml`'s `packages` list extended once more, to add
      `"app.routes"`.
- [x] Verify: same throwaway-container-first discipline — build-only, curled
      every endpoint category (auth, sync trigger+status, settings/config,
      wellness/runs, chat, health/recovery, a full workout CRUD round-trip, a
      full goal CRUD round-trip, generator run, dashboard summary, SPA
      fallback on a client-routed path, legacy mount) all green on a throwaway
      instance, only then recreated the real production container and
      reconfirmed real data (144 runs, 5 goals, Strava connected, dashboard
      cache serving real mileage numbers) untouched, plus a Home tab
      screenshot rendering exactly as before.

---

## Cross-cutting features (slot in any time after the listed dependency)

- [ ] **Daily AI insight card** (after 0.3): Sonnet one-shot (separate short-lived SDK
      client, same persona prompt), cached per day in sync_meta, Home widget —
      existing backlog item
- [ ] **Weekly coach report** (after 6.2): Sonnet one-shot every Sunday evening —
      week's load vs plan, readiness trend, next week rationale; persona-toned;
      stored + surfaced on Home, push notification
- [ ] **Workout critique** (after 4.3): coach compares completed run vs prescription
      (existing `record_workout_completion` path) — existing backlog item
- [ ] **Calendar view** (after 0.4): month grid, planned vs completed workouts +
      recovery; click-through to day detail
- [ ] **Demo mode** (after 0.10): `DEMO_MODE=1` seeds a synthetic-but-plausible
      dataset (generator script, ~1yr of runs/rides/strength/wellness); screenshot-rich
      README for the public repo
- [ ] **Race-day pack** (after 4.1): pacing plan from current fitness + race-day
      weather forecast + taper countdown, driven by the race Goal
- [ ] **Backups/export** (any time): nightly SQLite `VACUUM INTO` snapshot to the data
      volume (rotate 14), `GET /api/export` full-data zip; Settings section
- [ ] **Year-in-review** (after 7.1): annual summary page (totals, PRs, exploration,
      consistency), shareable image
- [ ] **Standing backlog** (fold in opportunistically): unified sync coordinator with
      per-source backoff; verify Garmin auto-retry/batch-pause on a real streak;
      deeper strength-training tracking (progression charts per exercise from
      `exercise_sets_json`)

---

## Deferred / explicitly out of scope

- PostGIS/PostgreSQL migration (rejected at current scale — see ROADMAP)
- MVT vector tiles / Mapbox (precomputed GeoJSON + Leaflet instead)
- Lab-panel PDF parsing (manual entry first)
- Meal-delivery live API sync (no official APIs; manifest import only)
- BLE sensors (interface reserved in 3.5)
- Local path / container / volume renames to `hale` (maintenance window; ROADMAP)

---
