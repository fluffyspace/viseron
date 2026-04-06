# Viseron Test Runner & Auto-Tune

Viseron includes a built-in test harness for validating motion and object
detection against pre-recorded video clips. Tests live at
`https://<your-host>/#/tests` and are completely isolated from live events.

## Overview

The test system lets you:

1. **Create test cases** from real recorded footage via the "Use for test"
   button on the events timeline.
2. **Run tests** to verify that your detection config produces the expected
   results (motion detected / not detected, specific objects found / not found).
3. **Auto-tune** detection parameters so tests pass automatically.

---

## Architecture

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| Test Runner | `viseron/components/test_runner/` | Core engine: loads cases, creates synthetic cameras, runs tests, evaluates results |
| Auto-Tuner | `viseron/components/test_runner/auto_tuner.py` | Analyzes failures, proposes parameter adjustments, applies them to `config.yaml` |
| Evaluator | `viseron/components/test_runner/evaluator.py` | Compares expected vs actual detections (motion counts, object labels) |
| Tests API | `viseron/components/webserver/api/v1/tests.py` | REST endpoints for runs, cases, clips, auto-tune |
| Frontend | `frontend/src/pages/Tests.tsx` | UI: run history, results, auto-tune controls, guidance tips |
| Dialog | `frontend/src/components/tests/UseForTestDialog.tsx` | Create test cases from event footage |

### How it works

1. Test cases define a **video clip**, a **kind** (motion/object), a
   **polarity** (positive = should detect, negative = should NOT detect),
   and **expected outcomes** (specific labels, or just detected/not-detected).

2. For each test case, the runner creates a **synthetic ffmpeg camera** that
   plays the clip file instead of connecting to a live stream. These cameras
   are tagged with `test_mode=true` so detections are stored in the DB with
   a `test` flag and never mix with live events.

3. **Test cameras are dormant at boot.** They are registered in the config
   but the NVR does not start them. No ffmpeg processes, no frame readers,
   and no memory consumed until a user explicitly triggers a test run.

4. **Tests run sequentially, one source camera at a time.** The runner
   groups cases by source camera, then for each group:
   - Starts the test cameras (`camera.start_camera()`)
   - Waits for the observation window (max duration of that group's cases)
   - Evaluates the cases
   - **Stops the cameras** (`camera.stop_camera()`), freeing all ffmpeg
     processes and memory before moving to the next group

5. After all groups finish, the runner queries the `Motion` and `Objects`
   tables for test-flagged rows, compares against expectations, and
   generates **parameter adjustment recommendations** from any failures.

### Database models

- `TestRun` — one row per execution (status, pass/fail counts, timestamps)
- `TestResult` — one row per case per run (expected, actual, passed, video/snapshot paths)
- `TestCase` — catalog of saved test cases (from "Use for test" dialog)

---

## Creating test cases

### From the UI (recommended)

1. Go to the **Events** timeline for any camera.
2. Select a time range that contains the event you want to test.
3. Click the **"Use for test"** floating action button.
4. Fill in the dialog:
   - **Camera**: auto-populated
   - **Start/End**: the time range (adjust to include lead-in/tail)
   - **Case name**: descriptive, e.g. "person walks across driveway"
   - **Kind**: motion or object
   - **Polarity**: positive (should detect) or negative (should NOT detect)
   - **Labels** (object/positive only): comma-separated, e.g. "person, car"
5. Click **Save clip**. The system extracts the video, stores it under
   `config/test_videos/<camera>/<kind>/<polarity>/`, and adds it to the catalog.

### From tests.yaml (declarative)

Create `tests.yaml` next to `config.yaml`:

```yaml
cameras:
  front_porch:
    motion:
      positive:
        - /config/test_videos/front_porch/motion/positive/person_walks_by.mp4
      negative:
        - /config/test_videos/front_porch/motion/negative/empty_scene.mp4
    object:
      positive:
        person:
          - /config/test_videos/front_porch/object/positive/person_detected.mp4
      negative:
        - /config/test_videos/front_porch/object/negative/tree_shadows.mp4

settings:
  default_duration: 15
  camera_ready_timeout: 60
```

### Video structure best practices

Each test clip should follow this pattern:

```
[10-30s calm scene] -> [event happens] -> [5-15s calm scene]
```

**Lead-in (10-30 seconds):** The motion detector (MOG2/background subtractor)
needs time to build its background model. Without a calm lead-in, the entire
first frame is treated as "motion" and causes false positives.

**Tail (5-15 seconds):** Lets the system confirm the event ended and the
scene returned to normal.

### Tips for effective test cases

- **Create paired positive + negative cases** for the same camera. This lets
  the auto-tuner find a threshold that separates real events from noise.
- **Use real recordings** from the events timeline. Real footage includes
  compression artifacts, shadows, and lighting changes that synthetic clips miss.
- **Keep clips short** (15-60s including lead/tail). Shorter clips mean
  faster auto-tune iterations.
- **Include hard negatives** — scenes where detection should NOT trigger but
  easily could: tree shadows moving, car headlight sweeps, rain, wind-blown objects.
- **One phenomenon per clip** — don't mix multiple events in one test case.
- **Vary conditions** — different times of day, weather, object sizes and
  distances to cover edge cases.
- **Label precisely** for object tests — the expected object must be clearly
  visible and large enough to pass size filters.

---

## Running tests

### From the UI

Go to `/#/tests` and click **Run tests**. The page shows sequential
progress — which camera group is currently running, pending, or done.
Results show pass/fail per case with expandable details (snapshot, video,
expected vs actual JSON).

After a run with failures, a **Recommendations** panel appears showing
suggested parameter adjustments (thresholds, confidence, etc.).

#### Auto-correct mode

Check the **Auto-correct** checkbox before clicking "Run tests" to
automatically apply recommended parameter changes and re-run. Set
**Max repetitions** to limit how many correction cycles to attempt
(default 3). Each cycle applies adjustments to `config.yaml` and
restarts Viseron so the new parameters take effect. Tests always run
one camera group at a time to minimize memory usage.

### From the REST API

```bash
# Trigger a run
curl -X POST http://localhost:8888/api/v1/tests/runs

# Trigger with auto-correct (max 5 repetitions)
curl -X POST http://localhost:8888/api/v1/tests/runs \
  -H 'Content-Type: application/json' \
  -d '{"auto_correct": true, "max_repetitions": 5}'

# Poll for results (includes progress and recommendations)
curl http://localhost:8888/api/v1/tests/runs/latest

# List all runs
curl http://localhost:8888/api/v1/tests/runs?limit=20
```

---

## Auto-tune / Auto-correct

The auto-correct system iteratively adjusts detection parameters to make
failing tests pass. It uses binary-search stepping (bisects toward the
optimal boundary each iteration).

### What it adjusts

**Motion detection:**

| Parameter | False negative (missed) | False positive (unwanted) |
|-----------|------------------------|--------------------------|
| `threshold` | Lower (more sensitive) | Raise (less sensitive) |
| `area` | Lower (smaller motion triggers) | Raise (ignore small noise) |

**Object detection:**

| Parameter | False negative (missed) | False positive (unwanted) |
|-----------|------------------------|--------------------------|
| `confidence` per label | Lower | Raise |
| `height_min` / `width_min` | Relax toward 0 | — |
| `scan_on_motion_only` | Disable (scan always) | — |
| Missing labels | Add with confidence 0.5 | — |

When positive and negative cases conflict on the same parameter (one wants
to lower, the other wants to raise), the tuner averages the proposals as
a compromise.

### Typical workflow

1. Record a few events on your cameras (let Viseron run for a day or two).
2. From the events timeline, create 2-4 test cases per camera: at least one
   positive (real event) and one negative (calm scene or hard negative).
3. Run tests once to see which fail and review the recommendations.
4. Enable **Auto-correct** and run again. 3-5 repetitions usually suffice.
5. After auto-correct completes, verify by running tests one final time.

---

## REST API reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/tests/runs` | GET | List recent test runs |
| `/tests/runs/latest` | GET | Latest run with results |
| `/tests/runs/<id>` | GET | Specific run with results |
| `/tests/runs` | POST | Trigger a new test run (accepts `auto_correct`, `max_repetitions`) |
| `/tests/clips` | POST | Create test case from recorded footage |
| `/tests/cases` | GET | List all catalogued test cases |
| `/tests/cases/<id>` | DELETE | Delete a test case and its clip |
| `/tests/cases/<id>/clip` | GET | Stream test case video |
| `/tests/results/<id>/clip` | GET | Stream test result video |
| `/tests/auto-tune` | POST | Start auto-tune session |
| `/tests/auto-tune` | GET | Get auto-tune progress |
| `/tests/auto-tune/cancel` | POST | Cancel running auto-tune |
| `/tests/restart` | POST | Restart Viseron (admin only, legacy) |

---

## Config reference

### config.yaml

```yaml
test_runner:
  shutdown_on_complete: false # Exit after run (for CI)
  camera_ready_timeout: 60   # Seconds to wait for test cameras
  default_duration: 15       # Default observation window (seconds)
```

Tests are always triggered manually (via UI or API). Test cameras are
dormant at boot and only started when a run begins.

### tests.yaml

```yaml
cameras:
  <camera_identifier>:
    motion:
      positive:
        - /path/to/clip.mp4
        - {from: "2024-01-01T10:00:00", to: "2024-01-01T10:05:00", name: "optional"}
      negative:
        - /path/to/calm_scene.mp4
    object:
      positive:
        person:
          - /path/to/person_clip.mp4
        any:
          - /path/to/any_object.mp4
      negative:
        - /path/to/no_objects.mp4

settings:
  default_duration: 15
  camera_ready_timeout: 60
  shutdown_on_complete: false
```

Sources can be file paths (string) or time ranges (mapping with `from`/`to`
ISO timestamps). Time ranges are materialized from recorded fragments and
cached under `config/test_videos/_timeline/`.
