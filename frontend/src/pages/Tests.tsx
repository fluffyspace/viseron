import CancelIcon from "@mui/icons-material/Cancel";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DeleteIcon from "@mui/icons-material/Delete";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RadioButtonUncheckedIcon from "@mui/icons-material/RadioButtonUnchecked";
import Accordion from "@mui/material/Accordion";
import AccordionDetails from "@mui/material/AccordionDetails";
import AccordionSummary from "@mui/material/AccordionSummary";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Collapse from "@mui/material/Collapse";
import Container from "@mui/material/Container";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useMemo, useState } from "react";

import { useTitle } from "hooks/UseTitle";
import {
  testCaseClipUrl,
  testResultClipUrl,
  useDeleteTestCase,
  useLatestTestRun,
  useTestCases,
  useTestRunDetail,
  useTestRuns,
  useTriggerTestRun,
} from "lib/api/tests";
import { getDayjsFromUnixTimestamp } from "lib/helpers/dates";
import * as types from "lib/types";

function statusColor(
  passed: number,
  failed: number,
  status: string,
): "success" | "warning" | "error" | "default" {
  if (status === "error") return "error";
  if (status === "running") return "warning";
  if (failed > 0) return "error";
  if (passed > 0) return "success";
  return "default";
}

function formatTimestamp(ts: number | null): string {
  if (!ts) return "\u2014";
  return getDayjsFromUnixTimestamp(ts).format("YYYY-MM-DD HH:mm:ss");
}

// ---------------------------------------------------------------------------
// Guidance panel — tips on creating effective test cases
// ---------------------------------------------------------------------------

function GuidancePanel() {
  return (
    <Accordion
      defaultExpanded={false}
      sx={{ mb: 3, bgcolor: "background.paper" }}
    >
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1} alignItems="center">
          <InfoOutlinedIcon color="info" fontSize="small" />
          <Typography variant="subtitle1">
            How to create effective test cases
          </Typography>
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={2}>
          <Box>
            <Typography variant="subtitle2" gutterBottom>
              Video structure
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Each test clip should follow this pattern:
            </Typography>
            <Paper
              variant="outlined"
              sx={{ p: 1.5, my: 1, fontFamily: "monospace", fontSize: 13 }}
            >
              [10-30s calm scene] &rarr; [event happens] &rarr; [5-15s calm
              scene]
            </Paper>
            <Typography variant="body2" color="text.secondary">
              <strong>Lead-in (10-30s):</strong> The motion detector needs time
              to build its background model. Without a calm lead-in, the entire
              first frame is treated as &ldquo;motion&rdquo; and causes false
              positives.
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              <strong>Tail (5-15s):</strong> Lets the system confirm the event
              ended and the scene returned to normal.
            </Typography>
          </Box>

          <Divider />

          <Box>
            <Typography variant="subtitle2" gutterBottom>
              Best practices
            </Typography>
            <Typography
              variant="body2"
              color="text.secondary"
              component="ul"
              sx={{ pl: 2, m: 0 }}
            >
              <li>
                <strong>Create paired positive + negative cases</strong> for the
                same camera. This lets the auto-tuner find a threshold that
                separates real events from noise.
              </li>
              <li>
                <strong>Use real recordings</strong> from the{" "}
                <em>Use for test</em> button on events &mdash; real footage
                includes compression artifacts, shadows, and lighting changes
                that synthetic clips miss.
              </li>
              <li>
                <strong>Keep clips short</strong> (15-60s including lead/tail).
                Shorter clips mean faster auto-tune iterations.
              </li>
              <li>
                <strong>Include hard negatives</strong> &mdash; scenes where
                detection should NOT trigger but easily could: tree shadows,
                headlight sweeps, rain, wind-blown objects.
              </li>
              <li>
                <strong>One phenomenon per clip</strong> &mdash; don&apos;t mix
                multiple events. Keep each test case focused on a single
                scenario.
              </li>
              <li>
                <strong>Vary conditions</strong> &mdash; different times of day,
                weather, object sizes and distances to cover edge cases.
              </li>
              <li>
                <strong>Label precisely</strong> for object tests &mdash; the
                expected object must be clearly visible and large enough to pass
                size filters.
              </li>
            </Typography>
          </Box>

          <Divider />

          <Box>
            <Typography variant="subtitle2" gutterBottom>
              What the auto-tuner adjusts
            </Typography>
            <Typography
              variant="body2"
              color="text.secondary"
              component="ul"
              sx={{ pl: 2, m: 0 }}
            >
              <li>
                <strong>Motion detection:</strong> <code>threshold</code>{" "}
                (pixel-diff sensitivity) and <code>area</code> (minimum contour
                size). Lower values = more sensitive.
              </li>
              <li>
                <strong>Object detection:</strong> <code>confidence</code> per
                label, <code>height_min/width_min</code> size filters, and{" "}
                <code>scan_on_motion_only</code>.
              </li>
              <li>
                Each iteration bisects toward the optimal value, so 3-5
                iterations usually suffice.
              </li>
            </Typography>
          </Box>
        </Stack>
      </AccordionDetails>
    </Accordion>
  );
}

// ---------------------------------------------------------------------------
// Sequential progress
// ---------------------------------------------------------------------------

function SequentialProgress({
  progress,
}: {
  progress: types.CameraGroupProgress[];
}) {
  if (progress.length === 0) return null;

  return (
    <Stack spacing={0.5} sx={{ mt: 2 }}>
      <Typography variant="subtitle2">Camera progress</Typography>
      {progress.map((group) => (
        <Stack
          key={group.source_camera}
          direction="row"
          spacing={1}
          alignItems="center"
        >
          {group.status === "done" && <CheckCircleIcon color="success" fontSize="small" />}
          {group.status === "running" && <CircularProgress size={16} />}
          {group.status === "pending" && (
            <RadioButtonUncheckedIcon color="disabled" fontSize="small" />
          )}
          <Typography variant="body2">{group.source_camera}</Typography>
          {group.status !== "pending" && (
            <Typography variant="body2" color="text.secondary">
              {group.passed}/{group.total} passed
            </Typography>
          )}
        </Stack>
      ))}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Recommendations panel
// ---------------------------------------------------------------------------

function RecommendationsPanel({
  recommendations,
}: {
  recommendations: types.Recommendation[];
}) {
  if (recommendations.length === 0) return null;

  return (
    <Card sx={{ mb: 3 }}>
      <CardContent>
        <Typography variant="h6" gutterBottom>
          Recommended parameter adjustments
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Based on test failures, these config changes may improve detection
          accuracy. Enable <em>Auto-correct</em> to apply them automatically.
        </Typography>
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Camera</TableCell>
                <TableCell>Domain</TableCell>
                <TableCell>Parameter</TableCell>
                <TableCell align="right">Current</TableCell>
                <TableCell align="right">Suggested</TableCell>
                <TableCell>Reason</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {recommendations.map((rec) => (
                <TableRow
                  key={`${rec.source_camera}-${rec.param_path.join(".")}`}
                >
                  <TableCell>{rec.source_camera}</TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      variant="outlined"
                      label={`${rec.component}.${rec.domain}`}
                    />
                  </TableCell>
                  <TableCell>
                    <code>{rec.param_path.join(".")}</code>
                  </TableCell>
                  <TableCell align="right">
                    <code>{rec.old_value}</code>
                  </TableCell>
                  <TableCell align="right">
                    <code>{rec.new_value}</code>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2" sx={{ maxWidth: 280 }}>
                      {rec.reason}
                    </Typography>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Existing components (summary, results, cases, history)
// ---------------------------------------------------------------------------

function SummaryCard({
  run,
  onRunTests,
  isTriggering,
  triggerError,
}: {
  run: types.TestRunDetail | null | undefined;
  onRunTests: (autoCorrect: boolean, maxRepetitions: number) => void;
  isTriggering: boolean;
  triggerError: string | null;
}) {
  const [autoCorrect, setAutoCorrect] = useState(false);
  const [maxRepetitions, setMaxRepetitions] = useState(3);
  const hasRun = run !== null && run !== undefined;
  const color = hasRun
    ? statusColor(run.passed, run.failed, run.status)
    : "default";

  return (
    <Card sx={{ mb: 3 }}>
      <CardContent>
        <Stack
          direction={{ xs: "column", sm: "row" }}
          spacing={2}
          alignItems={{ sm: "center" }}
          justifyContent="space-between"
        >
          <Box>
            <Typography variant="overline" color="text.secondary">
              Latest test run
            </Typography>
            {hasRun ? (
              <>
                <Typography variant="h4" component="div" sx={{ mt: 0.5 }}>
                  {run.failed} failed / {run.total} total
                </Typography>
                <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
                  <Chip
                    size="small"
                    color={color}
                    label={`${run.passed} passed`}
                  />
                  <Chip
                    size="small"
                    color={run.failed > 0 ? "error" : "default"}
                    label={`${run.failed} failed`}
                  />
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`status: ${run.status}`}
                  />
                </Stack>
                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{ mt: 1 }}
                >
                  Run #{run.id} · started{" "}
                  {formatTimestamp(run.started_timestamp)} · finished{" "}
                  {formatTimestamp(run.finished_timestamp)}
                </Typography>
              </>
            ) : (
              <Typography variant="body1" sx={{ mt: 1 }}>
                No test runs yet. Click <em>Run tests</em> to execute your
                configured cases.
              </Typography>
            )}
            {hasRun && run.progress && run.progress.length > 0 && (
              <SequentialProgress progress={run.progress} />
            )}
          </Box>
          <Stack spacing={1} alignItems="flex-end">
            <Button
              variant="contained"
              size="large"
              startIcon={
                isTriggering ? (
                  <CircularProgress size={18} color="inherit" />
                ) : (
                  <PlayArrowIcon />
                )
              }
              onClick={() => onRunTests(autoCorrect, maxRepetitions)}
              disabled={isTriggering}
            >
              Run tests
            </Button>
            <FormControlLabel
              control={
                <Checkbox
                  size="small"
                  checked={autoCorrect}
                  onChange={(e) => setAutoCorrect(e.target.checked)}
                />
              }
              label={
                <Typography variant="body2">Auto-correct</Typography>
              }
            />
            {autoCorrect && (
              <TextField
                label="Max repetitions"
                type="number"
                size="small"
                value={maxRepetitions}
                onChange={(e) =>
                  setMaxRepetitions(
                    Math.max(1, Math.min(10, Number(e.target.value) || 1)),
                  )
                }
                slotProps={{
                  htmlInput: { min: 1, max: 10, style: { width: 60 } },
                }}
              />
            )}
            {triggerError && (
              <Alert severity="error" sx={{ maxWidth: 320 }}>
                {triggerError}
              </Alert>
            )}
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}

function TestResultRow({ result }: { result: types.TestCaseResult }) {
  const [expanded, setExpanded] = useState(false);
  const icon = result.passed ? (
    <CheckCircleIcon color="success" />
  ) : (
    <CancelIcon color="error" />
  );

  return (
    <>
      <TableRow
        hover
        sx={{ cursor: "pointer" }}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <TableCell>{icon}</TableCell>
        <TableCell>{result.case_name}</TableCell>
        <TableCell>{result.camera_identifier}</TableCell>
        <TableCell>{result.kind}</TableCell>
        <TableCell>{result.message || "\u2014"}</TableCell>
      </TableRow>
      <TableRow>
        <TableCell
          colSpan={5}
          sx={{ p: 0, borderBottom: expanded ? undefined : 0 }}
        >
          <Collapse in={expanded} timeout="auto" unmountOnExit>
            <Box sx={{ p: 2, bgcolor: "background.default" }}>
              <Stack
                direction={{ xs: "column", md: "row" }}
                spacing={2}
                alignItems="flex-start"
              >
                <Stack spacing={1} sx={{ minWidth: 0 }}>
                  {result.snapshot_path && (
                    <Box
                      component="img"
                      src={result.snapshot_path}
                      alt={`snapshot for ${result.case_name}`}
                      sx={{
                        maxWidth: 360,
                        maxHeight: 240,
                        borderRadius: 1,
                        border: 1,
                        borderColor: "divider",
                      }}
                    />
                  )}
                  {result.video_path && (
                    <Box
                      component="video"
                      controls
                      preload="metadata"
                      src={testResultClipUrl(result.id)}
                      sx={{
                        maxWidth: 360,
                        borderRadius: 1,
                        border: 1,
                        borderColor: "divider",
                      }}
                    />
                  )}
                </Stack>
                <Box flex={1}>
                  <Typography variant="subtitle2">Expected</Typography>
                  <Paper
                    variant="outlined"
                    sx={{ p: 1, my: 0.5, fontFamily: "monospace" }}
                  >
                    {JSON.stringify(result.expected, null, 2)}
                  </Paper>
                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Actual
                  </Typography>
                  <Paper
                    variant="outlined"
                    sx={{ p: 1, my: 0.5, fontFamily: "monospace" }}
                  >
                    {JSON.stringify(result.actual, null, 2)}
                  </Paper>
                </Box>
              </Stack>
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </>
  );
}

function TestCasesSection() {
  const casesQuery = useTestCases();
  const deleteCase = useDeleteTestCase();

  const cases = casesQuery.data?.cases || [];

  const handleCopy = (snippet: string) => {
    navigator.clipboard.writeText(snippet);
  };

  if (casesQuery.isLoading) {
    return (
      <Box sx={{ py: 2 }}>
        <CircularProgress size={20} />
      </Box>
    );
  }

  if (cases.length === 0) {
    return (
      <Stack spacing={1.5} sx={{ py: 2 }}>
        <Typography variant="body2" color="text.secondary">
          No saved test cases yet. Use the <strong>Use for test</strong> button
          on the events timeline to turn a range of footage into a test fixture.
        </Typography>
        <Alert severity="info" variant="outlined">
          <strong>Tip:</strong> For best results, create both a positive case
          (should detect) and a negative case (should not detect) for each
          camera. Include 10-30 seconds of calm footage before the event so the
          motion detector can establish its background model.
        </Alert>
      </Stack>
    );
  }

  return (
    <Stack spacing={2}>
      {cases.map((testCase) => (
        <Card key={testCase.id} variant="outlined">
          <CardContent>
            <Stack
              direction={{ xs: "column", md: "row" }}
              spacing={2}
              alignItems="flex-start"
            >
              <Box
                component="video"
                controls
                preload="metadata"
                src={testCaseClipUrl(testCase.id)}
                sx={{
                  maxWidth: 320,
                  borderRadius: 1,
                  border: 1,
                  borderColor: "divider",
                }}
              />
              <Box flex={1} sx={{ minWidth: 0 }}>
                <Stack
                  direction="row"
                  spacing={1}
                  alignItems="center"
                  sx={{ mb: 1 }}
                  flexWrap="wrap"
                >
                  <Typography variant="h6">{testCase.name}</Typography>
                  <Chip size="small" label={testCase.kind} />
                  <Chip
                    size="small"
                    color={
                      testCase.polarity === "positive" ? "success" : "default"
                    }
                    label={testCase.polarity}
                  />
                </Stack>
                <Typography variant="body2" color="text.secondary">
                  Camera: {testCase.camera_identifier} · Duration:{" "}
                  {testCase.duration}s
                </Typography>
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{ display: "block", wordBreak: "break-all" }}
                >
                  {testCase.video_path}
                </Typography>
              </Box>
              <Stack spacing={0.5}>
                <Tooltip title="Copy YAML snippet">
                  <IconButton
                    size="small"
                    onClick={() => handleCopy(testCase.snippet)}
                  >
                    <ContentCopyIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
                <Tooltip title="Delete case + clip">
                  <IconButton
                    size="small"
                    color="error"
                    onClick={() => deleteCase.mutate(testCase.id)}
                    disabled={deleteCase.isPending}
                  >
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
            </Stack>
          </CardContent>
        </Card>
      ))}
    </Stack>
  );
}

function RunDetailTable({ run }: { run: types.TestRunDetail }) {
  if (run.results.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
        This run has no results yet.
      </Typography>
    );
  }
  return (
    <TableContainer component={Paper} variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell sx={{ width: 48 }} />
            <TableCell>Case</TableCell>
            <TableCell>Camera</TableCell>
            <TableCell>Kind</TableCell>
            <TableCell>Message</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {run.results.map((result) => (
            <TestResultRow key={result.id} result={result} />
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function RunHistoryTable({
  runs,
  selectedId,
  onSelect,
}: {
  runs: types.TestRunSummary[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  if (runs.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
        No previous runs.
      </Typography>
    );
  }
  return (
    <TableContainer component={Paper} variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Run</TableCell>
            <TableCell>Started</TableCell>
            <TableCell>Result</TableCell>
            <TableCell>Status</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {runs.map((run) => (
            <TableRow
              key={run.id}
              hover
              selected={run.id === selectedId}
              onClick={() => onSelect(run.id)}
              sx={{ cursor: "pointer" }}
            >
              <TableCell>#{run.id}</TableCell>
              <TableCell>{formatTimestamp(run.started_timestamp)}</TableCell>
              <TableCell>
                {run.passed}/{run.total} passed
              </TableCell>
              <TableCell>
                <Chip
                  size="small"
                  label={run.status}
                  color={statusColor(run.passed, run.failed, run.status)}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

function Tests() {
  useTitle("Tests");
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);

  const latestQuery = useLatestTestRun();
  const runsQuery = useTestRuns(50);
  const selectedRunQuery = useTestRunDetail(selectedRunId);
  const trigger = useTriggerTestRun();

  const triggerError = useMemo(() => {
    if (!trigger.error) return null;
    const response = trigger.error.response;
    if (response?.data?.error) return response.data.error;
    return trigger.error.message;
  }, [trigger.error]);

  const effectiveRun =
    selectedRunId !== null
      ? selectedRunQuery.data?.run || null
      : latestQuery.data?.run || null;

  const runs = runsQuery.data?.runs || [];
  const casesList = useTestCases();
  const componentEnabled = casesList.data?.component_enabled ?? true;

  return (
    <Container maxWidth="lg" sx={{ py: 3 }}>
      <Typography variant="h4" gutterBottom>
        Tests
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Tests live in a separate tab so they never mix with your live events.
        Run cases declared under the <code>test_runner</code> section of your
        config or created via <em>Use for test</em> to verify motion and
        object detection against pre-recorded footage. Tests run sequentially,
        one camera at a time.
      </Typography>

      {!componentEnabled && (
        <Alert severity="warning" sx={{ mb: 3 }}>
          The <code>test_runner</code> component is not enabled. Add{" "}
          <code>test_runner: {"{}"}</code> to your <code>config.yaml</code> and
          restart Viseron to enable test execution.
        </Alert>
      )}

      {componentEnabled && (
        <>
          <GuidancePanel />

          <SummaryCard
            run={effectiveRun}
            onRunTests={(autoCorrect, maxRepetitions) =>
              trigger.mutate({
                auto_correct: autoCorrect,
                max_repetitions: maxRepetitions,
              })
            }
            isTriggering={trigger.isPending}
            triggerError={triggerError}
          />

          {effectiveRun &&
            effectiveRun.recommendations &&
            effectiveRun.recommendations.length > 0 && (
              <RecommendationsPanel
                recommendations={effectiveRun.recommendations}
              />
            )}

          {effectiveRun && (
            <Box sx={{ mb: 3 }}>
              <Typography variant="h6" gutterBottom>
                Run #{effectiveRun.id} results
              </Typography>
              <RunDetailTable run={effectiveRun} />
            </Box>
          )}

          <Divider sx={{ my: 3 }} />

          <Typography variant="h6" gutterBottom>
            Test case catalog
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Cases saved via <em>Use for test</em> live here. Watch the clip,
            copy the matching YAML snippet into your{" "}
            <code>config.yaml</code>, and delete cases you no longer need.
          </Typography>
          <TestCasesSection />

          <Divider sx={{ my: 3 }} />

          <Typography variant="h6" gutterBottom>
            Run history
          </Typography>
          <RunHistoryTable
            runs={runs}
            selectedId={selectedRunId}
            onSelect={setSelectedRunId}
          />

          {selectedRunId !== null && (
            <Box sx={{ mt: 1 }}>
              <IconButton
                size="small"
                onClick={() => setSelectedRunId(null)}
                aria-label="Back to latest run"
              >
                <Typography variant="caption">
                  &larr; back to latest
                </Typography>
              </IconButton>
            </Box>
          )}
        </>
      )}
    </Container>
  );
}

export default Tests;
