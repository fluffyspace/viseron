import { CalendarHeatMap, Download, VideoAdd } from "@carbon/icons-react";
import FactCheckIcon from "@mui/icons-material/FactCheck";
import Box from "@mui/material/Box";
import Fab from "@mui/material/Fab";
import Tooltip from "@mui/material/Tooltip";
import { Dayjs } from "dayjs";
import { memo, useState } from "react";

import { CameraPickerDialog } from "components/camera/CameraPickerDialog";
import { DatePickerDialog } from "components/events/DatePickerDialog";
import { ExportDialog } from "components/events/ExportDialog";
import { useEventStore } from "components/events/utils";
import { UseForTestDialog } from "components/tests/UseForTestDialog";
import { getDayjsFromUnixTimestamp } from "lib/helpers/dates";
import * as types from "lib/types";

type FloatingMenuProps = {
  date: Dayjs;
  setDate: (date: Dayjs) => void;
};

function getEventTimes(event: types.CameraEvent | null) {
  if (!event) return { start: null, end: null, camera: "" };
  switch (event.type) {
    case "motion":
    case "recording":
      return {
        start: getDayjsFromUnixTimestamp(event.start_timestamp),
        end: event.end_timestamp
          ? getDayjsFromUnixTimestamp(event.end_timestamp)
          : null,
        camera: event.camera_identifier,
      };
    case "object":
    case "face_recognition":
    case "license_plate_recognition":
      return {
        start: getDayjsFromUnixTimestamp(event.timestamp),
        end: null,
        camera: event.camera_identifier,
      };
    default:
      return { start: null, end: null, camera: "" };
  }
}

export const FloatingMenu = memo(({ date, setDate }: FloatingMenuProps) => {
  const [cameraDialogOpen, setCameraDialogOpen] = useState(false);
  const [dateDialogOpen, setDateDialogOpen] = useState(false);
  const [exportDialogOpen, setExportDialogOpen] = useState(false);
  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const selectedEvent = useEventStore((state) => state.selectedEvent);

  const eventTimes = getEventTimes(selectedEvent);

  return (
    <>
      <CameraPickerDialog
        open={cameraDialogOpen}
        setOpen={setCameraDialogOpen}
      />
      <DatePickerDialog
        open={dateDialogOpen}
        setOpen={setDateDialogOpen}
        date={date}
        onChange={(value) => {
          setDateDialogOpen(false);
          if (value) {
            setDate(value);
          }
        }}
      />
      <ExportDialog open={exportDialogOpen} setOpen={setExportDialogOpen} />
      <UseForTestDialog
        key={selectedEvent?.id ?? "none"}
        open={testDialogOpen}
        setOpen={setTestDialogOpen}
        initialStart={eventTimes.start}
        initialEnd={eventTimes.end}
        initialCamera={eventTimes.camera}
      />
      <Box sx={{ position: "absolute", bottom: 16, right: 24 }}>

        <Tooltip title="Select Cameras">
          <Fab
            size="small"
            color="primary"
            onClick={() => setCameraDialogOpen(true)}
          >
            <VideoAdd size={20} />
          </Fab>
        </Tooltip>
        <Tooltip title="Select Date">
          <Fab
            size="small"
            color="primary"
            sx={{ marginLeft: 1 }}
            onClick={() => setDateDialogOpen(true)}
          >
            <CalendarHeatMap size={20} />
          </Fab>
        </Tooltip>
        <Tooltip title="Download">
          <Fab
            size="small"
            color="primary"
            sx={{ marginLeft: 1 }}
            onClick={() => setExportDialogOpen(true)}
          >
            <Download size={20} />
          </Fab>
        </Tooltip>
        <Tooltip title="Use for test">
          <Fab
            size="small"
            color="primary"
            sx={{ marginLeft: 1 }}
            onClick={() => setTestDialogOpen(true)}
          >
            <FactCheckIcon />
          </Fab>
        </Tooltip>
      </Box>
    </>
  );
});
