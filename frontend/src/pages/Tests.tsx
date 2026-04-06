import CancelIcon from "@mui/icons-material/Cancel";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DeleteIcon from "@mui/icons-material/Delete";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Collapse from "@mui/material/Collapse";
import Container from "@mui/material/Container";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useMemo, useState } from "react";

import { useTitle } from "hooks/UseTitle";
import {
  testCaseClipUrl,
  testResultClipUrl,
  useDeleteTestCase,
  useLatestTestRun,
  useRestartViseron,
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
  if (!ts) return "—";
  return getDayjsFromUnixTimestamp(ts).format("YYYY-MM-DD HH:mm:ss");
}

function SummaryCard({
  run,
  onRunTests,
  isTriggering,
  triggerError,
}: {
  run: types.TestRunDetail | null | undefined;
  onRunTests: () => void;
  isTriggering: boolean;
  triggerError: string | null;
}) {
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
              onClick={onRunTests}
              disabled={isTriggering}
            >
              Run tests
            </Button>
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
        <TableCell>{result.message || "—"}</TableCell>
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

function RestartBanner({ pendingCount }: { pendingCount: number }) {
  const restart = useRestartViseron();

  if (pendingCount === 0) return null;

  const handleRestart = () => {
    // eslint-disable-next-line no-alert
    if (
      !window.confirm(
        `Restart Viseron to pick up ${pendingCount} pending test case(s)? ` +
          "The UI will be unavailable for a few seconds while Viseron comes back up.",
      )
    ) {
      return;
    }
    restart.mutate(undefined, {
      onSuccess: () => {
        // Give the supervisor a moment, then bounce the page so React Query
        // reconnects to the fresh process cleanly.
        setTimeout(() => window.location.reload(), 3000);
      },
    });
  };

  return (
    <Alert
      severity="warning"
      sx={{ mb: 3 }}
      action={
        <Button
          color="inherit"
          size="small"
          startIcon={
            restart.isPending ? (
              <CircularProgress size={16} color="inherit" />
            ) : (
              <RestartAltIcon />
            )
          }
          onClick={handleRestart}
          disabled={restart.isPending}
        >
          Restart Viseron
        </Button>
      }
    >
      {pendingCount} test case
      {pendingCount === 1 ? " is" : "s are"} waiting for a restart to become
      runnable. Viseron needs to reload its config before new cases can
      execute.
    </Alert>
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
      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
        No saved test cases yet. Use the <strong>Use for test</strong> button
        on the events timeline to turn a range of footage into a test fixture.
      </Typography>
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
                  {testCase.pending_restart && (
                    <Chip
                      size="small"
                      color="warning"
                      label="pending restart"
                    />
                  )}
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
  const pendingRestartCount = (casesList.data?.cases || []).filter(
    (testCase) => testCase.pending_restart,
  ).length;

  return (
    <Container maxWidth="lg" sx={{ py: 3 }}>
      <Typography variant="h4" gutterBottom>
        Tests
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Tests live in a separate tab so they never mix with your live events.
        Run cases declared under the <code>test_runner</code> section of your
        config or created via <em>Use for test</em> to verify motion and
        object detection against pre-recorded footage.
      </Typography>

      <RestartBanner pendingCount={pendingRestartCount} />

      <SummaryCard
        run={effectiveRun}
        onRunTests={() => trigger.mutate()}
        isTriggering={trigger.isPending}
        triggerError={triggerError}
      />

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
        copy the matching YAML snippet into your <code>config.yaml</code>,
        and delete cases you no longer need.
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
            <Typography variant="caption">← back to latest</Typography>
          </IconButton>
        </Box>
      )}
    </Container>
  );
}

export default Tests;
