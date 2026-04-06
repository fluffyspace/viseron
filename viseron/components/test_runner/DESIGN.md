# Test Harness — Design & Implementation Notes

End-to-end harness that lets users validate motion and object detection
against pre-recorded video clips, without mixing test events into the
live Events view.

This document describes the system as shipped on branch
`feature/test-harness` (commit `b12ca9e0`). It is intended for developers
touching the `test_runner` component, its REST API, or the Tests page in
the frontend.

## Goals

Users had no way to trust that tweaking detector configuration wouldn't
silently break recordings. The harness lets them:

1. Turn a range of live footage into a reusable test clip with a
   single click from the events timeline.
2. Declare what each clip should produce — motion detected / not
   detected, object labels present / absent.
3. Run those cases on demand from the UI and see pass/fail counts,
   with drill-down to the snapshot + clip that produced each result.
4. Keep all of the above completely separate from the live Events
   view.

Non-goals: replacing the unit test suite, exercising the encoder path,
or asserting on detection timing/bounding-box coordinates. v1 only
checks presence/absence.

## High-level architecture

```
    ┌────────────────────┐
    │  events timeline   │  "Use for test" FAB
    │  (existing UI)     │
    └──────────┬─────────┘
               │ POST /api/v1/tests/clips
               ▼
    ┌────────────────────┐          ┌──────────────────┐
    │  tests.py handler  │  writes  │ test_cases table │
    │                    │─────────▶│  (catalog)       │
    └──────────┬─────────┘          └──────────────────┘
               │ clip file
               ▼
    /config/test_videos/<cam>/<kind>/<polarity>/<slug>.mp4

    on Viseron startup (PRE_PARALLEL tier):
               │
    ┌──────────▼─────────────────────────────────────────┐
    │ test_runner.setup() → inject_db_cases()            │
    │  reads test_cases, clones detector config from     │
    │  target cameras, injects synthetic entries into    │
    │  config["ffmpeg"]["camera"]                         │
    └──────────┬─────────────────────────────────────────┘
               │
    ┌──────────▼─────────┐
    │ ffmpeg component    │  registers the synthetic
    │ (parallel tier)     │  test-mode cameras like
    └──────────┬─────────┘  any other cameras
               │
    ┌──────────▼─────────┐   ┌───────────────────────┐
    │  NVR + detectors    │──▶│ motion/objects tables │
    │  (normal path)      │   │  with test=true flag  │
    └────────────────────┘   └──────────┬────────────┘
                                         │
    ┌────────────────────┐                │
    │ POST /tests/runs   │                │
    │  → TestRunner      │ evaluates rows │
    └──────────┬─────────┘                │
               │ writes                    │
               ▼                           │
    ┌─────────────────────────────────────┴─┐
    │ test_runs + test_results tables       │
    └───────────────┬───────────────────────┘
                    │
    ┌───────────────▼──────────────┐
    │  /tests page                  │  big pass/fail card,
    │  (new React route)            │  per-result drill-down,
    └──────────────────────────────┘  catalog management
```

The key insight: test cameras flow through the **canonical camera +
NVR pipeline**. The test harness never touches detectors directly. It
just injects synthetic cameras that happen to read from MP4 files
instead of RTSP, and tags their output rows with `test=true`.

## Database schema

Four new tables and a `test` boolean added to four existing tables.
Two Alembic migrations:

- `c1a5e7e57f1a_add_test_flag_and_test_run_tables.py` — adds
  `test BOOLEAN NOT NULL DEFAULT false` + index to `motion`,
  `objects`, `recordings`, `events`; creates `test_runs` and
  `test_results`.
- `d4f09a7b3e12_add_test_cases_table.py` — creates the
  `test_cases` catalog with a unique constraint on
  `(camera_identifier, kind, polarity, slug)`.

### `test` flag on existing tables

Default `false`, cheap index. Written by:

- `viseron/domains/motion_detector/__init__.py` — `_insert_motion`
- `viseron/domains/object_detector/__init__.py` — `_insert_object`
- `viseron/domains/camera/recorder.py` — `start_recording`

Each pulls `self._camera.is_test_camera`, a new property on
`AbstractCamera` that defaults to `False`. The ffmpeg `Camera`
subclass overrides it to read `CONFIG_TEST_MODE` from its config
block.

Read-side filtering happens in
`viseron/components/webserver/api/v1/events.py` — every query that
feeds the live Events tab adds `.where(Motion.test.is_(False))` (or
the equivalent). Detail endpoints (`/recordings/:id`, `/hls/...`)
are **not** filtered so that test results can still be played back
via shared infrastructure.

### `test_runs` and `test_results`

One `test_runs` row per invocation of the runner. One `test_results`
row per case. `expected` and `actual` are JSONB so the evaluator can
store structured data without schema churn.

### `test_cases` catalog

```sql
CREATE TABLE test_cases (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  camera_identifier TEXT NOT NULL,
  kind TEXT NOT NULL,            -- 'motion' | 'object'
  polarity TEXT NOT NULL,        -- 'positive' | 'negative'
  expected JSONB NOT NULL,
  video_path TEXT NOT NULL,
  duration INTEGER NOT NULL DEFAULT 15,
  ...
  UNIQUE (camera_identifier, kind, polarity, slug)
);
```

Only rows created via the `Use for test` dialog live here. Cases
declared inline in `config.yaml` are **not** mirrored to this table;
the runner unions both sources at trigger time.

## ffmpeg integration

Two new keys on the camera config schema
(`viseron/components/ffmpeg/const.py`, `.../camera.py`):

- `file_source: /absolute/path.mp4` — when set, `Stream.get_stream_url`
  returns the path verbatim and `stream_command` uses `-re` for
  realtime pacing and skips `-rtsp_transport`. `ffprobe` also skips
  rtsp-specific flags. Network fields (`host`, `port`, `path`,
  `stream_format`) are ignored.
- `test_mode: bool` — cameras flagged this way propagate
  `is_test_camera=True` through to the insert sites, so every row
  they produce is tagged `test=true`.

`save_snapshot` in `viseron/domains/camera/__init__.py` checks
`is_test_camera` and skips the zoom-to-bounding-box crop for object
detections, so test snapshots preserve the full frame with the
detection box drawn on top. Live snapshots are unchanged.

## The `test_runner` component

Lives at `viseron/components/test_runner/`.

```
test_runner/
├── __init__.py      # setup() + CONFIG_SCHEMA
├── __main__.py      # CLI entrypoint
├── const.py         # config keys, kinds, statuses
├── evaluator.py     # pure pass/fail logic
├── runner.py        # TestRunner, TestRunnerComponent, inject_db_cases
└── DESIGN.md        # this file
```

### Setup ordering

Ordinary Viseron components load in parallel after `CORE` and
`DEFAULT`. That would cause a race: `test_runner` can't inject
entries into `config["ffmpeg"]["camera"]` if ffmpeg has already read
it. Slice D solved this by adding a new sequential tier in
`viseron/components/__init__.py`:

```python
PRE_PARALLEL_COMPONENTS = {"test_runner"}
# ...
for component in components_in_config & PRE_PARALLEL_COMPONENTS:
    setup_component(vis, get_component(vis, component, config))
```

Only loads when present in config. Runs after `storage` (so the
DB is available) but before ffmpeg (so mutations land). The parallel
pool below excludes it.

### `inject_db_cases(vis, config)`

Called from `setup()`. Reads the `test_cases` table and for each
row whose target camera lives under `ffmpeg`:

1. Deep-copies the target's `motion_detector` and `object_detector`
   config (so later mutations can't leak back into the real camera).
2. Builds a synthetic entry:
   ```python
   {
     "name": "Test <camera> — <case name>",
     "host": "localhost", "port": 554, "path": "/",   # placeholders
     "test_mode": True,
     "file_source": case.video_path,
     "motion_detector": <cloned>,
     "object_detector": <cloned>,
   }
   ```
3. Writes it to `config["ffmpeg"]["camera"][f"test_{target}_{slug}"]`
   if that identifier isn't already present (idempotent — users who
   pasted a YAML snippet manually aren't clobbered).

Cases whose target camera isn't under ffmpeg are skipped with a
warning. Cases whose target doesn't exist at all are skipped with a
warning. Neither is treated as an error — they stay in the catalog
and simply aren't runnable until the user fixes their config.

The function returns a list of runner-compatible case dicts, one per
DB row that **could** be picked up (even if the synthetic camera was
pre-existing). The `TestRunnerComponent` holds these in memory and
merges them with `config.yaml` cases at trigger time.

### `TestRunnerComponent`

Holder object stored on `vis.data[COMPONENT]` for the lifetime of
the process. Owns:

- `config` — validated `test_runner` block
- `db_cases` — list of DB-backed case dicts (from `inject_db_cases`)
- `current_runner` — the most recently started `TestRunner`, may be
  finished or in-flight
- A threading lock guarding `trigger_run`

`trigger_run()` is the entry point for the REST API and the CLI. It:

1. Refuses if a run is already in flight.
2. Calls `_collect_runnable_cases()` which unions
   `config["cases"]` with `db_cases`, filtering out DB cases whose
   synthesized camera isn't registered yet (pending restart).
3. Raises `RuntimeError` if zero cases are runnable — better than
   silently claiming success.
4. Spawns a fresh `TestRunner(vis, merged_config)` in a daemon
   thread.

### `TestRunner` (single run)

Runs once and exits. Sequence:

1. **Clear stale detections.** Deletes any `test=true` rows on the
   referenced cameras' `motion`/`objects` tables. This is the
   crucial line that lets the evaluator treat "all test-mode rows
   for camera X" as "this run's detections" without needing a
   time window (which would race with camera startup).
2. **Insert `test_runs` row** with `status='running'`.
3. **Resolve cameras.** Polls `vis.get_registered_domain` for each
   referenced camera with a 60s deadline, verifying each has
   `is_test_camera=True`.
4. **Wait.** Sleeps for `max(case[duration] for case in cases)`
   wall-clock seconds — cameras run in parallel, so this is the
   bound, not the sum.
5. **Evaluate each case** via `evaluator.evaluate_case`, collect
   `CaseOutcome`s.
6. **Persist `test_results` rows**, one per case.
7. **Finalize `test_runs`** with pass/fail counts and
   `status='complete'` (or `'error'` on exception).
8. **Set `vis.exit_code`** — 0 if all passed, 1 otherwise. Lets the
   CLI propagate pass/fail as an exit status.
9. **Signal `completion_event`** so the CLI / REST layer can unblock.
10. If `shutdown_on_complete`, call `vis.shutdown()`.

### Evaluator

`evaluator.py` is pure logic. No Viseron imports beyond the models.
Three public entry points:

```python
def evaluate_motion(get_session, camera_identifier, expected) -> CaseOutcome
def evaluate_object(get_session, camera_identifier, expected) -> CaseOutcome
def evaluate_case(kind, ...)  # dispatches by kind
```

Expectations v1:

| kind   | expected              | passes when                          |
|--------|-----------------------|--------------------------------------|
| motion | `{detected: true}`    | at least one motion row              |
| motion | `{detected: false}`   | zero motion rows                     |
| object | `{labels: [...]}`     | every listed label appeared ≥ 1 time |
| object | `{detected: false}`   | zero object rows                     |
| object | `{detected: true}`    | at least one object row (any label)  |

Snapshot path picked from the first row seen so the UI can render
something even for multi-detection cases.

### CLI

```
python -m viseron.components.test_runner
```

Starts Viseron normally (reads `$VISERON_CONFIG_DIR/config.yaml`),
triggers a run via the holder, waits on `completion_event`, and
exits with `vis.exit_code`. Suitable as a CI entrypoint.

## REST API

`viseron/components/webserver/api/v1/tests.py`, routes self-register
via Viseron's handler convention.

| Method | Path                                    | Purpose                                       |
|--------|-----------------------------------------|-----------------------------------------------|
| GET    | `/api/v1/tests/runs[?limit=N]`          | List runs (newest first, 1 ≤ N ≤ 200)         |
| GET    | `/api/v1/tests/runs/latest`             | Latest run + its results                      |
| GET    | `/api/v1/tests/runs/:id`                | One run + its results                         |
| POST   | `/api/v1/tests/runs`                    | Trigger (202 / 409 conflict / 404 no-config)  |
| POST   | `/api/v1/tests/clips`                   | Clip a timespan → catalog entry + MP4 on disk |
| GET    | `/api/v1/tests/cases[?camera=...]`      | List catalog with pending_restart decoration  |
| DELETE | `/api/v1/tests/cases/:id`               | Remove row + on-disk file (sandboxed)         |
| GET    | `/api/v1/tests/cases/:id/clip`          | Stream the catalog clip                       |
| GET    | `/api/v1/tests/results/:id/clip`        | Stream the clip a given result came from      |
| POST   | `/api/v1/tests/restart`                 | Graceful restart (admin-only)                 |

### Clip extraction (`POST /tests/clips`)

Reuses the existing recording-export path:

```python
files = get_time_period_fragments([camera.identifier], start, end, get_session)
fragments = [Fragment(f.filename, f.path, f.duration, f.orig_ctime) for f in files]
tmp_path = camera.fragmenter.concatenate_fragments(fragments)
shutil.move(tmp_path, <test_videos_root>/<cam>/<kind>/<polarity>/<slug>.mp4)
```

Then upserts a `test_cases` row via Postgres
`ON CONFLICT DO UPDATE` keyed on the unique constraint, so re-saving
a clip with the same name overwrites cleanly. The response includes
`case_id`, the final on-disk path, and a YAML snippet the user can
paste into `config.yaml` as a fallback (v1 leaves the snippet in
place even though the catalog now handles persistence — it's useful
for users who prefer declaring cases in YAML).

### Path traversal guard

Both clip-serving endpoints and the delete endpoint run every
`video_path` through `_is_within_test_videos`:

```python
def _is_within_test_videos(path: str) -> bool:
    root = os.path.realpath(_test_videos_root())
    candidate = os.path.realpath(path)
    return candidate == root or candidate.startswith(root + os.sep)
```

`realpath` resolution means symlink tricks don't help. Rows pointing
outside the sandbox get:
- `403` on clip serving
- Preserved on disk during `DELETE` (DB row goes, file stays)

### `pending_restart` flag

`GET /tests/cases` decorates each entry with `pending_restart: bool`
computed server-side by calling
`vis.get_registered_domain(CAMERA_DOMAIN, _synth_test_camera_id(...))`.
`True` means the case was added after this Viseron started and
needs a restart to become runnable. The frontend uses this to drive
the Restart Viseron banner.

### Restart endpoint

`POST /tests/restart` mirrors the existing
`restart_viseron` websocket command (`commands.py:315`):

```python
self._vis.exit_code = RESTART_EXIT_CODE   # = 100
# ... respond 202 ...
os.kill(os.getpid(), signal.SIGINT)
```

The 202 is flushed before SIGINT so the client sees the response
before the process tears down. The supervisor (systemd, s6, docker
restart policy — see `rootfs/etc/services.d/viseron/finish`) is
responsible for bringing Viseron back up. Admin-only.

## Frontend

React 19 + MUI 7 + React Query + Zustand + React Router v7. All
additions follow existing patterns — no new dependencies.

### New files

- `frontend/src/pages/Tests.tsx` — top-level page
- `frontend/src/components/tests/UseForTestDialog.tsx` — dialog
  launched from the events timeline FAB
- `frontend/src/lib/api/tests.ts` — React Query hooks + URL builders

### Tests page structure

1. **Restart banner** (`<RestartBanner />`) — shown whenever any
   catalog case has `pending_restart: true`. Confirms before calling
   `POST /tests/restart`, then reloads the window after 3 seconds
   to let React Query reconnect cleanly to the restarted process.
2. **Summary card** — latest run headline (`X failed / Y total`),
   pass/fail chips, started/finished timestamps, big `Run tests`
   button. Surfaces 409 conflicts via inline `Alert`.
3. **Run detail table** — one row per result, click to expand:
   snapshot thumbnail, inline `<video>` player (streams from
   `/tests/results/:id/clip`), expected and actual JSON side by
   side.
4. **Catalog section** (`<TestCasesSection />`) — one card per
   stored case: video preview, camera + kind + polarity chips,
   `pending restart` chip when applicable, copy-YAML and delete
   buttons.
5. **Run history table** — click a row to inspect that run instead
   of the latest.

React Query polls `/runs` and `/runs/latest` every 5 seconds so
in-flight runs reflect in the UI without manual refresh.

### Use for test dialog

Launched from a new FAB on `FloatingMenu.tsx` next to the existing
download button. Reuses `useFilteredCameras` + the same DateTimePicker
range UI as `ExportDialog.tsx`. Collects:

- Camera (single select)
- Time range (start + end)
- Case name
- Kind (motion | object)
- Polarity (should detect | should not detect)
- Object labels (only when kind=object and polarity=positive)

Submits to `POST /tests/clips`, displays the returned snippet in a
monospace read-only textarea with a copy-to-clipboard button, and
guides the user to the Restart Viseron banner on the Tests page.

### Routing + nav

- `App.tsx` — new `/tests` route under `PrivateLayout`
- `Drawer.tsx` — new `Tests` link with the `FactCheckIcon`

## Test coverage

Pytest. Three new test modules (plus additions to an existing one):

### `tests/components/test_runner/test_evaluator.py`

Unit tests for the pass/fail logic. Uses `get_db_session` fixture
to insert Motion/Objects rows directly, then calls the evaluator.
Covers:

- Motion detected when expected, missing when expected, wrong-way for both cases
- Ignores rows with `test=false`
- Ignores rows for other cameras
- Object: all labels present → pass; any missing label → fail with
  diagnostic
- Object: `detected: false` positive and negative
- Snapshot path plucked from the first row

### `tests/components/test_runner/test_schema.py`

Voluptuous config validation. Positive and negative cases.

### `tests/components/test_runner/test_runner.py`

Integration tests for the runner orchestration. Uses a `_FakeViseron`
stand-in that returns real SQLAlchemy sessions from a test DB but
mocks `get_registered_domain`. Classes:

- **`TestClearStaleDetections`** — wipes referenced-camera test rows
  without touching live rows or other cameras
- **`TestFullRun`** — mixed pass/fail run, all-passing exit code,
  refusing non-test-mode cameras, missing camera timeout
- **`TestInjectDbCases`** — detector deep-clone (mutation on the
  copy doesn't leak), skipping non-ffmpeg targets, idempotency with
  pre-existing synthetic entries, no-op without ffmpeg config
- **`TestRunnerComponentCaseUnioning`** — config cases + DB cases
  unioned, DB cases with unregistered synth cameras skipped,
  `trigger_run` raises on empty runnable set

`_wait_for_observation_window` is monkeypatched to a no-op so tests
run instantly.

### `tests/components/webserver/api/v1/test_tests.py`

REST handler tests. Uses the existing `TestAppBaseNoAuth` fixture
and patches `ViseronRequestHandler._get_session` / `_get_camera`.
Classes:

- **`TestListAndDetail`** — list ordering, limit honouring, detail
  endpoint, 404 on missing run, latest endpoint
- **`TestTriggerRun`** — 404 when no holder, 202 happy path, 409
  concurrent-run conflict
- **`TestCreateClip`** — happy path persists + returns snippet,
  idempotent on duplicate submission, 400 on invalid range, 404
  on missing fragments or missing camera
- **`TestCasesCatalog`** — list with snippet, camera filter, delete
  removes row + file, delete preserves rogue file outside sandbox,
  clip serving 200/403/404
- **`TestResultClipEndpoint`** — stream bytes by result id, 404 on
  missing
- **`TestPendingRestartFlag`** — patches `get_registered_domain` to
  simulate registered vs. pending synth cameras, asserts decoration
- **`TestRestartEndpoint`** — patches `os.kill`, asserts `202`,
  `exit_code == RESTART_EXIT_CODE`, and one `kill` call

## End-to-end user workflow

1. Open the events timeline for the camera you want to validate.
2. Pick a time range (use the existing DateTimePicker or drag on
   the timeline).
3. Click the new ✓ **Use for test** FAB. Fill in name / kind /
   polarity / labels. Click **Save clip**.
4. Dialog confirms: *"Case #N saved to the catalog. Click Restart
   Viseron on the Tests page."* Close it.
5. Navigate to **Tests**. Yellow banner at the top: *"1 test case
   is waiting for a restart to become runnable."* Click **Restart
   Viseron**. The browser auto-reloads after 3 seconds.
6. Banner is gone. The new case shows in the catalog with its video
   preview. Click **Run tests**. Results appear in the summary
   card.
7. Click a result row to expand — see snapshot with bounding box,
   inline clip playback, expected vs. actual JSON.

No YAML editing at any step.

## Known limitations / future work

- **~~True zero-restart iteration.~~** Resolved: `refresh_db_cases()`
  now registers detection and NVR domains at runtime in addition to
  the camera domain. `inject_db_cases()` / `inject_yaml_cases()` also
  inject into detection component and NVR configs at PRE_PARALLEL so
  the full pipeline is wired on startup.
- **Gstreamer support.** `inject_db_cases` only injects into the
  `ffmpeg` component. Users with gstreamer cameras can create cases
  but they're silently skipped with a warning.
- **Schema pre-validation.** `inject_db_cases` mutates the ffmpeg
  config block but doesn't run ffmpeg's voluptuous schema against
  its injected entries. A malformed clip path or detector config
  surfaces later as a confusing ffmpeg setup error. A lightweight
  pre-flight check would help.
- **Motion contour overlays.** Object detection snapshots get
  bounding boxes; motion snapshots are still raw frames. Passing
  `motion_contours` through `save_snapshot` for test cameras would
  close the gap.
- **Clip serving memory usage.** Clips are read into memory on every
  request. Long files or many concurrent players would benefit from
  a streamed Tornado response with `Range:` support.
- **Events table `test` flag.** The column exists and is indexed,
  but the write site in `viseron/__init__.py` doesn't populate it —
  the dispatch layer has no camera context. Low priority; the UI
  doesn't consult the `events` table for test results.
- **Alembic migration coverage.** Two new migrations, neither
  exercised by pytest. The existing suite uses
  `Base.metadata.create_all` which bypasses migrations. A dedicated
  upgrade/downgrade test would give confidence before release.
- **Role-aware UI.** `POST /tests/restart` is admin-only but the
  frontend doesn't hide / disable the button for non-admin roles.
- **Single-camera dialog.** `UseForTestDialog` always targets the
  first filtered camera even when multiple are selected on the
  timeline. Clipping from N cameras in one gesture is rare enough
  to defer.

## File manifest

Everything in this feature lives under one of these paths.

### Backend

- `viseron/components/__init__.py` — `PRE_PARALLEL_COMPONENTS` tier
- `viseron/components/storage/models.py` — `test` flag, `TestRun`,
  `TestResult`, `TestCase`
- `viseron/components/storage/alembic/versions/c1a5e7e57f1a_*.py`
- `viseron/components/storage/alembic/versions/d4f09a7b3e12_*.py`
- `viseron/components/test_runner/` — the whole component
- `viseron/components/webserver/api/v1/tests.py` — REST handler
- `viseron/components/webserver/api/v1/events.py` — `test=false`
  filters on live queries
- `viseron/components/ffmpeg/const.py` — `CONFIG_FILE_SOURCE`,
  `CONFIG_TEST_MODE`
- `viseron/components/ffmpeg/camera.py` — schema extensions +
  `is_test_camera` override
- `viseron/components/ffmpeg/stream.py` — file source handling
- `viseron/domains/camera/__init__.py` — `is_test_camera` property,
  `save_snapshot` no-zoom for test cameras
- `viseron/domains/camera/recorder.py` — `test` flag on inserts
- `viseron/domains/motion_detector/__init__.py` — `test` flag on
  inserts
- `viseron/domains/object_detector/__init__.py` — `test` flag on
  inserts

### Frontend

- `frontend/src/pages/Tests.tsx`
- `frontend/src/components/tests/UseForTestDialog.tsx`
- `frontend/src/components/events/FloatingMenu.tsx` — new FAB
- `frontend/src/components/header/Drawer.tsx` — new nav entry
- `frontend/src/App.tsx` — new `/tests` route
- `frontend/src/lib/api/tests.ts`
- `frontend/src/lib/types.ts` — test-related types

### Tests

- `tests/components/test_runner/test_evaluator.py`
- `tests/components/test_runner/test_schema.py`
- `tests/components/test_runner/test_runner.py`
- `tests/components/webserver/api/v1/test_tests.py`

### Misc

- `.gitignore` — comment about `/config/test_videos/` layout

## Development history

Built in four slices across one working session:

- **Slice A** — foundation: schema, ffmpeg file input, minimal
  CLI-only runner. No UI.
- **Slice B** — REST endpoints, `/tests` page, "Use for test"
  dialog. Requires YAML paste + restart to iterate.
- **Slice C** — `test_cases` catalog, cases management UI, inline
  clip playback, bounding-box overlays. Still requires YAML paste.
- **Slice D** — `PRE_PARALLEL` component tier, config injection,
  `pending_restart` flag, Restart Viseron banner. Kills the YAML
  step.

The slice boundaries are preserved in the commit only as a narrative
inside this document; the `feature/test-harness` branch carries a
single squashed commit.
