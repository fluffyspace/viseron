import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControl from "@mui/material/FormControl";
import FormControlLabel from "@mui/material/FormControlLabel";
import FormLabel from "@mui/material/FormLabel";
import IconButton from "@mui/material/IconButton";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Radio from "@mui/material/Radio";
import RadioGroup from "@mui/material/RadioGroup";
import Select, { SelectChangeEvent } from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { DateTimePicker } from "@mui/x-date-pickers/DateTimePicker";
import { Dayjs } from "dayjs";
import { useEffect, useMemo, useState } from "react";

import { useFilteredCameras } from "components/camera/useCameraStore";
import { useCreateTestClip } from "lib/api/tests";
import { is12HourFormat } from "lib/helpers";
import * as types from "lib/types";

type UseForTestDialogProps = {
  open: boolean;
  setOpen: (open: boolean) => void;
  initialStart?: Dayjs | null;
  initialEnd?: Dayjs | null;
};

type Kind = "motion" | "object";
type Polarity = "positive" | "negative";

function buildExpected(
  kind: Kind,
  polarity: Polarity,
  labels: string,
): Record<string, unknown> {
  if (kind === "motion") {
    return { detected: polarity === "positive" };
  }
  // kind === "object"
  if (polarity === "negative") {
    return { detected: false };
  }
  const trimmedLabels = labels
    .split(",")
    .map((label) => label.trim())
    .filter((label) => label.length > 0);
  if (trimmedLabels.length === 0) {
    return { detected: true };
  }
  return { labels: trimmedLabels };
}

export function UseForTestDialog({
  open,
  setOpen,
  initialStart,
  initialEnd,
}: UseForTestDialogProps) {
  const [startDate, setStartDate] = useState<Dayjs | null>(
    initialStart || null,
  );
  const [endDate, setEndDate] = useState<Dayjs | null>(initialEnd || null);
  const [cameraIdentifier, setCameraIdentifier] = useState<string>("");
  const [name, setName] = useState<string>("");
  const [kind, setKind] = useState<Kind>("object");
  const [polarity, setPolarity] = useState<Polarity>("positive");
  const [labels, setLabels] = useState<string>("person");

  const filteredCameras = useFilteredCameras();
  const createClip = useCreateTestClip();

  const cameraOptions = useMemo(
    () => Object.keys(filteredCameras),
    [filteredCameras],
  );

  // Default camera to the first selected one when the dialog opens.
  useEffect(() => {
    if (open && !cameraIdentifier && cameraOptions.length > 0) {
      setCameraIdentifier(cameraOptions[0]);
    }
  }, [open, cameraIdentifier, cameraOptions]);

  const handleClose = () => {
    setOpen(false);
    // Reset snippet view so next open starts fresh.
    createClip.reset();
  };

  const handleSubmit = () => {
    if (!startDate || !endDate || !cameraIdentifier || !name.trim()) return;
    const payload: types.TestClipCreateRequest = {
      camera_identifier: cameraIdentifier,
      start: startDate.unix(),
      end: endDate.unix(),
      name: name.trim(),
      kind,
      polarity,
      expected: buildExpected(kind, polarity, labels),
    };
    createClip.mutate(payload);
  };

  const handleCopy = () => {
    if (createClip.data?.snippet) {
      navigator.clipboard.writeText(createClip.data.snippet);
    }
  };

  const errorMessage = useMemo(() => {
    if (!createClip.error) return null;
    const response = createClip.error.response;
    if (response?.data?.error) return response.data.error;
    return createClip.error.message;
  }, [createClip.error]);

  const disabled =
    !startDate ||
    !endDate ||
    !cameraIdentifier ||
    !name.trim() ||
    endDate.isBefore(startDate) ||
    createClip.isPending;

  return (
    <Dialog fullWidth maxWidth="sm" open={open} onClose={handleClose}>
      <DialogTitle>Use footage for test</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            Clip the selected time range from this camera and save it as a
            test fixture. The clip is stored under{" "}
            <code>/config/test_videos/&lt;camera&gt;/&lt;kind&gt;/&lt;polarity&gt;/</code>{" "}
            and added to the test case catalog. Click <em>Restart Viseron</em>{" "}
            on the Tests page to pick up the new case — no YAML editing
            required.
          </Typography>

          <FormControl fullWidth size="small">
            <InputLabel>Camera</InputLabel>
            <Select
              label="Camera"
              value={cameraIdentifier}
              onChange={(event: SelectChangeEvent) =>
                setCameraIdentifier(event.target.value)
              }
            >
              {cameraOptions.map((identifier) => (
                <MenuItem key={identifier} value={identifier}>
                  {identifier}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          <DateTimePicker
            label="Start"
            views={["year", "month", "day", "hours", "minutes", "seconds"]}
            value={startDate}
            onAccept={setStartDate}
            onChange={setStartDate}
            closeOnSelect={false}
            ampm={is12HourFormat()}
          />
          <DateTimePicker
            label="End"
            views={["year", "month", "day", "hours", "minutes", "seconds"]}
            value={endDate}
            onAccept={setEndDate}
            onChange={setEndDate}
            closeOnSelect={false}
            ampm={is12HourFormat()}
            minDateTime={startDate || undefined}
          />

          <TextField
            label="Case name"
            size="small"
            value={name}
            onChange={(event) => setName(event.target.value)}
            helperText="e.g. 'person walks by', 'empty room'"
            fullWidth
          />

          <FormControl>
            <FormLabel>Kind</FormLabel>
            <RadioGroup
              row
              value={kind}
              onChange={(event) => setKind(event.target.value as Kind)}
            >
              <FormControlLabel
                value="object"
                control={<Radio size="small" />}
                label="Object"
              />
              <FormControlLabel
                value="motion"
                control={<Radio size="small" />}
                label="Motion"
              />
            </RadioGroup>
          </FormControl>

          <FormControl>
            <FormLabel>Expected outcome</FormLabel>
            <RadioGroup
              row
              value={polarity}
              onChange={(event) =>
                setPolarity(event.target.value as Polarity)
              }
            >
              <FormControlLabel
                value="positive"
                control={<Radio size="small" />}
                label="Should detect"
              />
              <FormControlLabel
                value="negative"
                control={<Radio size="small" />}
                label="Should NOT detect"
              />
            </RadioGroup>
          </FormControl>

          {kind === "object" && polarity === "positive" && (
            <TextField
              label="Expected labels (comma separated)"
              size="small"
              value={labels}
              onChange={(event) => setLabels(event.target.value)}
              helperText="e.g. 'person, car'"
              fullWidth
            />
          )}

          {errorMessage && <Alert severity="error">{errorMessage}</Alert>}

          {createClip.data && (
            <Stack spacing={1}>
              <Alert severity="success">
                Case #{createClip.data.case_id} saved to the catalog. Open
                the <strong>Tests</strong> page and click{" "}
                <em>Restart Viseron</em> to make the case runnable. The YAML
                snippet below is only needed if you prefer declaring cases
                manually in <code>config.yaml</code>:
              </Alert>
              <Stack
                direction="row"
                alignItems="flex-start"
                spacing={1}
              >
                <TextField
                  multiline
                  fullWidth
                  minRows={6}
                  maxRows={16}
                  value={createClip.data.snippet}
                  slotProps={{
                    htmlInput: { readOnly: true },
                  }}
                  sx={{
                    "& textarea": {
                      fontFamily: "monospace",
                      fontSize: 12,
                    },
                  }}
                />
                <Tooltip title="Copy YAML">
                  <IconButton onClick={handleCopy} size="small">
                    <ContentCopyIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
            </Stack>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>Close</Button>
        <Button
          variant="contained"
          disabled={disabled}
          onClick={handleSubmit}
        >
          {createClip.isPending ? "Saving…" : "Save clip"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
