import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import { DateTimePicker } from "@mui/x-date-pickers/DateTimePicker";
import { Dayjs } from "dayjs";
import { useState } from "react";

import { useFilteredCameras } from "components/camera/useCameraStore";
import { useEventStore } from "components/events/utils";
import { useExportTimespan } from "hooks/UseExportTimespan";
import {
  getDayjsFromUnixTimestamp,
  is12HourFormat,
} from "lib/helpers/dates";
import * as types from "lib/types";

const getEventRange = (
  event: types.CameraEvent,
): { start: number; end: number } => {
  if (event.type === "motion" || event.type === "recording") {
    return {
      start: event.start_timestamp,
      end: event.end_timestamp ?? event.start_timestamp,
    };
  }
  return { start: event.timestamp, end: event.timestamp };
};

type ExportDialogProps = {
  open: boolean;
  setOpen: (open: boolean) => void;
};

type ExportDialogBodyProps = {
  setOpen: (open: boolean) => void;
};

// Body is only mounted while the dialog is open, so useState initializers
// run fresh each time and snapshot the currently selected event for prefill.
function ExportDialogBody({ setOpen }: ExportDialogBodyProps) {
  const selectedEvent = useEventStore.getState().selectedEvent;
  const initialRange = selectedEvent ? getEventRange(selectedEvent) : null;

  const [startDate, setStartDate] = useState<Dayjs | null>(
    initialRange ? getDayjsFromUnixTimestamp(initialRange.start) : null,
  );
  const [endDate, setEndDate] = useState<Dayjs | null>(
    initialRange ? getDayjsFromUnixTimestamp(initialRange.end) : null,
  );

  const filteredCameras = useFilteredCameras();
  const exportTimespan = useExportTimespan();

  const handleClose = () => {
    setOpen(false);
  };

  const handleExport = () => {
    if (!startDate || !endDate) return;
    exportTimespan(
      Object.keys(filteredCameras),
      startDate.unix(),
      endDate.unix(),
    );
    handleClose();
  };

  const isExportDisabled =
    !startDate || !endDate || endDate.isBefore(startDate);

  return (
    <>
      <DialogTitle>Download Recording</DialogTitle>
      <DialogContent>
        <Stack spacing={3} sx={{ mt: 1 }}>
          <DateTimePicker
            label="Start Date & Time"
            views={["year", "month", "day", "hours", "minutes", "seconds"]}
            value={startDate}
            onAccept={setStartDate}
            onChange={setStartDate}
            closeOnSelect={false}
            ampm={is12HourFormat()}
          />
          <DateTimePicker
            label="End Date & Time"
            views={["year", "month", "day", "hours", "minutes", "seconds"]}
            value={endDate}
            onAccept={setEndDate}
            onChange={setEndDate}
            closeOnSelect={false}
            ampm={is12HourFormat()}
            minDateTime={startDate || undefined}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>Cancel</Button>
        <Button
          onClick={handleExport}
          disabled={isExportDisabled}
          variant="contained"
        >
          Download
        </Button>
      </DialogActions>
    </>
  );
}

export function ExportDialog({ open, setOpen }: ExportDialogProps) {
  return (
    <Dialog
      fullWidth
      maxWidth="xs"
      open={open}
      onClose={() => setOpen(false)}
    >
      {open && <ExportDialogBody setOpen={setOpen} />}
    </Dialog>
  );
}
