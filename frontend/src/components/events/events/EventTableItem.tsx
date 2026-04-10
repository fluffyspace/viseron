import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import CardMedia from "@mui/material/CardMedia";
import Grid from "@mui/material/Grid";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { memo, useMemo } from "react";

import { SnapshotIcon } from "components/events/SnapshotEvent";
import {
  extractUniqueLabels,
  extractUniqueTypes,
  getEventTime,
  getSrc,
  useEventStore,
  useSelectEvent,
} from "components/events/utils";
import { useFirstRender } from "hooks/UseFirstRender";
import {
  BLANK_IMAGE,
  formatDuration,
  getCameraNameFromQueryCache,
  getTimeFromDate,
} from "lib/helpers";
import * as types from "lib/types";

const getEventDurationSeconds = (event: types.CameraEvent): number | null => {
  if (event.type === "motion" || event.type === "recording") {
    return event.duration;
  }
  return null;
};

type EventTableItemIconsProps = {
  sortedEvents: types.CameraEvent[];
};

function EventTableItemIcons({ sortedEvents }: EventTableItemIconsProps) {
  const uniqueEvents = extractUniqueTypes(sortedEvents);
  // Use the recording in the group as the headline event when present, so the
  // card's metadata (time, duration, thumbnail) describes the playable clip
  // rather than an arbitrary detection inside it.
  const headlineEvent =
    sortedEvents.find((e) => e.type === "recording") ?? sortedEvents[0];
  const cameraName = getCameraNameFromQueryCache(
    headlineEvent.camera_identifier,
  );
  const durationSeconds = getEventDurationSeconds(headlineEvent);
  const timeStr = getTimeFromDate(new Date(getEventTime(headlineEvent)));

  return (
    <div>
      <Typography fontSize=".75rem" fontWeight="bold" align="center">
        {cameraName}
      </Typography>
      <Typography fontSize=".75rem" color="text.secondary" align="center">
        {timeStr}
        {durationSeconds !== null && ` (${formatDuration(durationSeconds)})`}
      </Typography>
      <Grid container justifyContent="center" alignItems="center">
        {Object.keys(uniqueEvents).map((key) => {
          // For object detection we want to group by label
          if (key === "object") {
            const uniqueLabels = extractUniqueLabels(
              uniqueEvents[key] as Array<types.CameraObjectEvent>,
            );
            return Object.keys(uniqueLabels).map((label) => (
              <Grid key={`icon-${key}-${label}`}>
                <SnapshotIcon
                  events={uniqueLabels[label]}
                  parentGroup={sortedEvents}
                />
              </Grid>
            ));
          }
          return (
            <Grid key={`icon-${key}`}>
              <SnapshotIcon
                events={uniqueEvents[key]}
                parentGroup={sortedEvents}
              />
            </Grid>
          );
        })}
      </Grid>
    </div>
  );
}

type EventTableItemProps = {
  events: types.CameraEvent[];
  isScrolling: boolean;
  virtualRowIndex: number;
  measureElement: (element: HTMLElement | null) => void;
  setElementHeight: React.Dispatch<React.SetStateAction<number | null>>;
};
export const EventTableItem = memo(
  ({
    events,
    isScrolling,
    virtualRowIndex,
    measureElement,
    setElementHeight,
  }: EventTableItemProps) => {
    const theme = useTheme();
    const firstRender = useFirstRender();

    const { selectedEvent } = useEventStore();

    // Show the oldest event first in the list, API returns latest first
    const sortedEvents = useMemo(
      () =>
        events
          .slice()
          .sort((a, b) => a.created_at_timestamp - b.created_at_timestamp),
      [events],
    );
    const handleEventClick = useSelectEvent();

    // Headline event = the recording in the group if present, otherwise the
    // chronologically first event. The card thumbnail and the "is selected"
    // highlight both follow the headline so the row visually represents the
    // same thing the click selects.
    const headlineEvent =
      sortedEvents.find((e) => e.type === "recording") ?? sortedEvents[0];

    const src = useMemo(
      () => (isScrolling && firstRender ? BLANK_IMAGE : getSrc(headlineEvent)),
      [isScrolling, firstRender, headlineEvent],
    );

    const selected = !!selectedEvent && selectedEvent.id === headlineEvent.id;

    return (
      <Card
        data-index={virtualRowIndex}
        ref={(node) => {
          measureElement(node);
          if (node) {
            setElementHeight(node.offsetHeight);
          }
        }}
        variant="outlined"
        square
        sx={[
          {
            boxShadow: "none",
          },
          selected
            ? {
                borderRadius: 1, // theme.shape.borderRadius * 1
                border: `2px solid ${theme.palette.primary[400]}`,
                padding: "0px",
              }
            : {
                border: "2px solid transparent",
                borderBottom: `1px solid ${theme.palette.divider}`,
                paddingBottom: "1px",
              },
        ]}
      >
        <CardActionArea onClick={() => handleEventClick(headlineEvent)}>
          <Grid
            container
            direction="row"
            justifyContent="flex-end"
            alignItems="center"
          >
            <Grid size={8}>
              <EventTableItemIcons sortedEvents={sortedEvents} />
            </Grid>
            <Grid size={4}>
              <CardMedia
                sx={{
                  borderRadius: 1, // theme.shape.borderRadius * 1
                  overflow: "hidden",
                }}
              >
                <img
                  src={src}
                  alt="Event snapshot"
                  style={{
                    aspectRatio: "1/1",
                    width: "100%",
                    height: "100%",
                    objectFit: "contain",
                    background: theme.palette.background.default,
                  }}
                />
              </CardMedia>
            </Grid>
          </Grid>
        </CardActionArea>
      </Card>
    );
  },
);
