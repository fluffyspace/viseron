import {
  PlayFilledAlt,
  StarFilled,
  Star,
  StopFilledAlt,
} from "@carbon/icons-react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import Chip from "@mui/material/Chip";
import Container from "@mui/material/Container";
import Divider from "@mui/material/Divider";
import FormControl from "@mui/material/FormControl";
import IconButton from "@mui/material/IconButton";
import InputLabel from "@mui/material/InputLabel";
import LinearProgress from "@mui/material/LinearProgress";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Select, { SelectChangeEvent } from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { DatePicker } from "@mui/x-date-pickers/DatePicker";
import { Dayjs } from "dayjs";
import { useContext, useEffect, useMemo, useState } from "react";

import { Loading } from "components/loading/Loading";
import { ViseronContext } from "context/ViseronContext";
import { useTitle } from "hooks/UseTitle";
import { useCameras } from "lib/api/cameras";
import { BASE_PATH } from "lib/api/client";
import { useEventsMultiple } from "lib/api/events";
import {
  usePlayRecording,
  usePlaybackState,
  useStopPlayback,
} from "lib/api/playback";
import { subscribeEvent } from "lib/commands";
import {
  BLANK_IMAGE,
  formatDuration,
  getTimeFromDate,
  objHasValues,
} from "lib/helpers";
import { getDateStringFromDayjs, getDayjs } from "lib/helpers/dates";
import { usePlaybackFavorites } from "lib/playbackFavorites";
import * as types from "lib/types";

const MAX_LOG_ENTRIES = 200;

type LogEntry = {
  id: string;
  timestamp: number;
  label: string;
  detail: string;
  color: "primary" | "secondary" | "success" | "warning" | "info" | "default";
};

function describeEvent(name: string, data: any): LogEntry | null {
  // name is like "<camera_id>/camera_event/<type>/<sub>" or
  // "<camera_id>/recorder/start" / "/stop"
  const parts = name.split("/");
  if (parts.length < 2) return null;
  const kind = parts[1];
  const now = Date.now();
  const id = `${now}-${Math.random().toString(36).slice(2, 8)}`;

  if (kind === "recorder") {
    const action = parts[2] ?? "?";
    return {
      id,
      timestamp: now,
      label: `recorder/${action}`,
      detail: data?.recording?.id ? `recording #${data.recording.id}` : "",
      color: action === "start" ? "warning" : "info",
    };
  }
  if (kind === "camera_event") {
    const type = parts[2] ?? "?";
    const sub = parts[3] ?? "";
    let detail = "";
    if (type === "object" && data?.label) {
      detail = `${data.label} ${
        data.confidence ? `(${(data.confidence * 100).toFixed(0)}%)` : ""
      }`;
    } else if (type === "motion") {
      detail = sub === "start" ? "started" : sub === "end" ? "ended" : sub;
    } else if (type === "recording") {
      detail = sub === "start" ? "started" : sub === "end" ? "ended" : sub;
    }
    return {
      id,
      timestamp: now,
      label: `${type}${sub ? `/${sub}` : ""}`,
      detail,
      color:
        type === "motion"
          ? "primary"
          : type === "object"
            ? "success"
            : type === "recording"
              ? "warning"
              : "default",
    };
  }
  return null;
}

function PlaybackTargetSelector({
  cameras,
  selectedId,
  onChange,
}: {
  cameras: types.Camera[];
  selectedId: string;
  onChange: (id: string) => void;
}) {
  if (cameras.length === 0) {
    return (
      <Alert severity="warning" sx={{ mb: 2 }}>
        No playback cameras configured. Add a camera with{" "}
        <code>playback_mode: true</code> to <code>config.yaml</code>.
      </Alert>
    );
  }
  if (cameras.length === 1) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Target: <strong>{cameras[0].name}</strong> (
        <code>{cameras[0].identifier}</code>)
      </Typography>
    );
  }
  return (
    <FormControl size="small" sx={{ mb: 2, minWidth: 240 }}>
      <InputLabel id="playback-target-label">Playback target</InputLabel>
      <Select
        labelId="playback-target-label"
        label="Playback target"
        value={selectedId}
        onChange={(e: SelectChangeEvent) => onChange(e.target.value)}
      >
        {cameras.map((camera) => (
          <MenuItem key={camera.identifier} value={camera.identifier}>
            {camera.name} ({camera.identifier})
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  );
}

function RecordingPickerRow({
  event,
  cameraName,
  busy,
  isFavorite,
  onPlay,
  onToggleFavorite,
}: {
  event: types.CameraRecordingEvent;
  cameraName: string;
  busy: boolean;
  isFavorite: boolean;
  onPlay: (event: types.CameraRecordingEvent) => void;
  onToggleFavorite: (event: types.CameraRecordingEvent) => void;
}) {
  const theme = useTheme();
  const startTime = getTimeFromDate(new Date(event.start_time));
  const dateStr = event.start_time.slice(0, 10);
  const duration = event.duration ? formatDuration(event.duration) : "—";

  // The star button intentionally lives outside the CardActionArea so MUI's
  // ripple/click handler on the row doesn't fire when toggling favorites.
  return (
    <Card
      variant="outlined"
      sx={{ mb: 0.5, display: "flex", alignItems: "stretch" }}
    >
      <CardActionArea
        disabled={busy}
        onClick={() => onPlay(event)}
        sx={{ display: "flex", alignItems: "stretch", flex: 1 }}
      >
        <Box
          sx={{
            width: 96,
            minWidth: 96,
            height: 64,
            background: theme.palette.background.default,
          }}
        >
          <img
            src={event.thumbnail_path || BLANK_IMAGE}
            alt="thumbnail"
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              display: "block",
            }}
          />
        </Box>
        <Box
          sx={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            px: 1,
            py: 0.5,
            minWidth: 0,
          }}
        >
          <Typography
            variant="body2"
            sx={{
              fontWeight: 600,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {cameraName}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {dateStr} {startTime} · {duration}
          </Typography>
          <Stack direction="row" spacing={0.5} sx={{ mt: 0.25 }}>
            {event.trigger_type && (
              <Chip
                size="small"
                label={event.trigger_type}
                color={event.trigger_type === "object" ? "success" : "primary"}
                sx={{ height: 18, fontSize: "0.65rem" }}
              />
            )}
            <Chip
              size="small"
              label={`#R${event.id}`}
              variant="outlined"
              sx={{ height: 18, fontSize: "0.65rem", fontFamily: "monospace" }}
            />
          </Stack>
        </Box>
        <Box sx={{ display: "flex", alignItems: "center", px: 1 }}>
          <PlayFilledAlt size={20} />
        </Box>
      </CardActionArea>
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          borderLeft: `1px solid ${theme.palette.divider}`,
          px: 0.25,
        }}
      >
        <Tooltip
          title={isFavorite ? "Remove from favorites" : "Add to favorites"}
        >
          <IconButton
            size="small"
            onClick={(e) => {
              e.stopPropagation();
              onToggleFavorite(event);
            }}
            sx={{
              color: isFavorite
                ? theme.palette.warning.main
                : theme.palette.text.disabled,
            }}
          >
            {isFavorite ? <StarFilled size={18} /> : <Star size={18} />}
          </IconButton>
        </Tooltip>
      </Box>
    </Card>
  );
}

function EventLogPanel({
  cameraIdentifier,
}: {
  cameraIdentifier: string | null;
}) {
  const theme = useTheme();
  const { connection } = useContext(ViseronContext);
  const [entries, setEntries] = useState<LogEntry[]>([]);

  useEffect(() => {
    if (!connection || !cameraIdentifier) {
      return undefined;
    }
    let cancelled = false;
    const unsubs: Array<() => Promise<void>> = [];

    const append = (name: string, data: any) => {
      const entry = describeEvent(name, data);
      if (!entry) return;
      setEntries((prev) => [entry, ...prev].slice(0, MAX_LOG_ENTRIES));
    };

    (async () => {
      try {
        const sub1 = await subscribeEvent<any>(
          connection,
          `${cameraIdentifier}/camera_event/*/*`,
          (msg: any) => append(msg?.name ?? "", msg?.data ?? {}),
        );
        if (cancelled) {
          await sub1();
        } else {
          unsubs.push(sub1);
        }
        const sub2 = await subscribeEvent<any>(
          connection,
          `${cameraIdentifier}/recorder/start`,
          (msg: any) => append(msg?.name ?? "", msg?.data ?? {}),
        );
        if (cancelled) {
          await sub2();
        } else {
          unsubs.push(sub2);
        }
        const sub3 = await subscribeEvent<any>(
          connection,
          `${cameraIdentifier}/recorder/stop`,
          (msg: any) => append(msg?.name ?? "", msg?.data ?? {}),
        );
        if (cancelled) {
          await sub3();
        } else {
          unsubs.push(sub3);
        }
      } catch (err) {
        // Subscription failed; ignore for now (panel just stays empty).
      }
    })();

    return () => {
      cancelled = true;
      unsubs.forEach((u) => {
        u().catch(() => {
          // Ignore unsubscribe errors on teardown.
        });
      });
    };
  }, [connection, cameraIdentifier]);

  return (
    <Paper
      variant="outlined"
      sx={{
        flex: 1,
        minHeight: 200,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <Box
        sx={{
          px: 1.5,
          py: 0.75,
          borderBottom: `1px solid ${theme.palette.divider}`,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Typography variant="subtitle2">Live event log</Typography>
        <Typography variant="caption" color="text.secondary">
          {entries.length} {entries.length === 1 ? "event" : "events"}
        </Typography>
      </Box>
      <Box
        sx={{
          flex: 1,
          overflowY: "auto",
          fontFamily: "monospace",
          fontSize: "0.75rem",
          px: 1.5,
          py: 0.5,
        }}
      >
        {entries.length === 0 ? (
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ fontStyle: "italic" }}
          >
            Waiting for events…
          </Typography>
        ) : (
          entries.map((entry) => (
            <Box
              key={entry.id}
              sx={{
                display: "flex",
                gap: 1,
                py: 0.25,
                borderBottom: `1px solid ${theme.palette.divider}`,
              }}
            >
              <span style={{ color: theme.palette.text.secondary }}>
                {getTimeFromDate(new Date(entry.timestamp))}
              </span>
              <Chip
                size="small"
                label={entry.label}
                color={entry.color === "default" ? undefined : entry.color}
                sx={{ height: 16, fontSize: "0.65rem" }}
              />
              <span>{entry.detail}</span>
            </Box>
          ))
        )}
      </Box>
    </Paper>
  );
}

function PlaybackPreview({
  camera,
  isPlaying,
  currentFile,
}: {
  camera: types.Camera;
  isPlaying: boolean;
  currentFile: string | null;
}) {
  const theme = useTheme();
  // Bump key whenever a new file starts so the <img> reconnects to the new
  // mjpeg stream rather than reusing a stale connection.
  const streamKey = useMemo(
    () => `${camera.identifier}-${currentFile ?? "idle"}`,
    [camera.identifier, currentFile],
  );

  return (
    <Box
      sx={{
        position: "relative",
        width: "100%",
        aspectRatio: `${camera.width} / ${camera.height}`,
        background: theme.palette.background.default,
        borderRadius: 1,
        overflow: "hidden",
      }}
    >
      {isPlaying ? (
        <img
          key={streamKey}
          src={`${BASE_PATH}/${camera.identifier}/mjpeg-stream`}
          alt="Playback preview"
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
            display: "block",
          }}
        />
      ) : (
        <Box
          sx={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: theme.palette.text.secondary,
            fontSize: "0.875rem",
          }}
        >
          Idle — pick a recording to play
        </Box>
      )}
    </Box>
  );
}

function Playback() {
  useTitle("Playback");
  const theme = useTheme();
  const cameras = useCameras({});

  const playbackCameras = useMemo<types.Camera[]>(
    () =>
      cameras.data
        ? Object.values(cameras.data).filter((c) => c.is_playback_camera)
        : [],
    [cameras.data],
  );
  const sourceCameras = useMemo<types.Camera[]>(
    () =>
      cameras.data
        ? Object.values(cameras.data).filter((c) => !c.is_playback_camera)
        : [],
    [cameras.data],
  );

  // The user can explicitly pick a target, but until they do (or after their
  // pick disappears from config) we derive a default. Done as a useMemo so we
  // never have to call setState from inside an effect — the derived value
  // simply updates whenever cameras change.
  const [explicitTarget, setExplicitTarget] = useState<string | null>(null);
  const targetId = useMemo(() => {
    if (playbackCameras.length === 0) return "";
    if (
      explicitTarget &&
      playbackCameras.find((c) => c.identifier === explicitTarget)
    ) {
      return explicitTarget;
    }
    return playbackCameras[0].identifier;
  }, [playbackCameras, explicitTarget]);

  const targetCamera = useMemo(
    () => playbackCameras.find((c) => c.identifier === targetId) ?? null,
    [playbackCameras, targetId],
  );

  const playbackState = usePlaybackState(targetId || null);
  const playMutation = usePlayRecording();
  const stopMutation = useStopPlayback();
  const {
    favorites,
    isFavorite,
    toggle: toggleFavorite,
  } = usePlaybackFavorites();

  // Tab state — "recent" browses by date, "favorites" shows the persisted
  // localStorage list regardless of date.
  const [tab, setTab] = useState<"recent" | "favorites">("recent");

  // Date state for the "recent" tab. Defaulted to today; the user can pick
  // any date with the inline DatePicker in the panel header.
  const [date, setDate] = useState<Dayjs>(() => getDayjs());
  const dateStr = useMemo(() => getDateStringFromDayjs(date), [date]);

  const sourceIds = useMemo(
    () => sourceCameras.map((c) => c.identifier),
    [sourceCameras],
  );
  const eventQueries = useEventsMultiple({
    camera_identifiers: sourceIds,
    date: dateStr,
  });

  const recordings = useMemo<types.CameraRecordingEvent[]>(() => {
    const data = (eventQueries as unknown as { data: types.CameraEvent[] })
      .data;
    if (!data) return [];
    return data.filter(
      (e): e is types.CameraRecordingEvent => e.type === "recording",
    );
  }, [eventQueries]);

  // The server returns favorites already sorted newest-first; the cast is
  // safe because PlaybackFavorite is a structural superset of
  // CameraRecordingEvent (same fields RecordingPickerRow reads).
  const favoriteList = useMemo<types.CameraRecordingEvent[]>(
    () => favorites as unknown as types.CameraRecordingEvent[],
    [favorites],
  );

  const cameraNameById = useMemo(() => {
    const map: Record<string, string> = {};
    sourceCameras.forEach((c) => {
      map[c.identifier] = c.name;
    });
    return map;
  }, [sourceCameras]);

  const handlePlay = (event: types.CameraRecordingEvent) => {
    if (!targetId) return;
    // Always pass the source camera identifier so the backend can prefer
    // the persisted favorite blob (under /favorites/<src>/R<id>.mp4) when
    // it exists. For non-favorited recordings the favorite check is a
    // cheap miss and the call falls through to the normal DB lookup.
    playMutation.mutate({
      camera_identifier: targetId,
      recording_id: event.id,
      source_camera_identifier: event.camera_identifier,
    });
  };

  const handleStop = () => {
    if (!targetId) return;
    stopMutation.mutate(targetId);
  };

  if (cameras.isPending) {
    return <Loading text="Loading Cameras" />;
  }
  if (!objHasValues<typeof cameras.data>(cameras.data)) {
    return <Loading text="Waiting for cameras to register" />;
  }

  const isPlaying = playbackState.data?.is_playing ?? false;
  const currentFile = playbackState.data?.current_file ?? null;
  const busy = playMutation.isPending || stopMutation.isPending;

  return (
    <Container
      maxWidth={false}
      sx={{
        paddingX: { xs: 1, md: 2 },
        paddingY: 1,
        height: `calc(100dvh - ${theme.headerHeight}px - ${theme.headerMargin})`,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <PlaybackTargetSelector
        cameras={playbackCameras}
        selectedId={targetId}
        onChange={setExplicitTarget}
      />

      {playbackCameras.length === 0 ? null : (
        <Box
          sx={{
            flex: 1,
            display: "grid",
            gridTemplateColumns: { xs: "1fr", md: "minmax(280px, 38%) 1fr" },
            gap: 1.5,
            minHeight: 0,
          }}
        >
          {/* Left: recordings picker — tabs switch between date-browsable
              recent recordings and the persisted favorites list. */}
          <Paper
            variant="outlined"
            sx={{
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
              overflow: "hidden",
            }}
          >
            <Tabs
              value={tab}
              onChange={(_, v: "recent" | "favorites") => setTab(v)}
              variant="fullWidth"
              sx={{ borderBottom: `1px solid ${theme.palette.divider}` }}
            >
              <Tab value="recent" label="Recent" />
              <Tab
                value="favorites"
                label={`Favorites${
                  favoriteList.length ? ` (${favoriteList.length})` : ""
                }`}
              />
            </Tabs>

            {tab === "recent" && (
              <Box
                sx={{
                  px: 1.5,
                  py: 1,
                  borderBottom: `1px solid ${theme.palette.divider}`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 1,
                }}
              >
                <DatePicker
                  label="Date"
                  value={date}
                  onChange={(value) => {
                    if (value) setDate(value);
                  }}
                  format="YYYY-MM-DD"
                  slotProps={{
                    textField: { size: "small", sx: { maxWidth: 180 } },
                  }}
                />
                <Stack
                  direction="row"
                  spacing={0.5}
                  alignItems="center"
                  sx={{ flexShrink: 0 }}
                >
                  <Button
                    size="small"
                    variant="text"
                    onClick={() => setDate(getDayjs())}
                  >
                    Today
                  </Button>
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ minWidth: 24, textAlign: "right" }}
                  >
                    {recordings.length}
                  </Typography>
                </Stack>
              </Box>
            )}

            {tab === "recent" &&
              (eventQueries as unknown as { isPending: boolean }).isPending && (
                <LinearProgress />
              )}

            <Box sx={{ flex: 1, overflowY: "auto", p: 1 }}>
              {tab === "recent" ? (
                recordings.length === 0 ? (
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ fontStyle: "italic", textAlign: "center", mt: 2 }}
                  >
                    No recordings on {dateStr}.
                  </Typography>
                ) : (
                  recordings.map((event) => (
                    <RecordingPickerRow
                      key={`R${event.id}`}
                      event={event}
                      cameraName={
                        cameraNameById[event.camera_identifier] ??
                        event.camera_identifier
                      }
                      busy={busy}
                      isFavorite={isFavorite(event)}
                      onPlay={handlePlay}
                      onToggleFavorite={toggleFavorite}
                    />
                  ))
                )
              ) : favoriteList.length === 0 ? (
                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{ fontStyle: "italic", textAlign: "center", mt: 2 }}
                >
                  No favorites yet. Star a recording on the Recent tab to save
                  it here.
                </Typography>
              ) : (
                favoriteList.map((event) => (
                  <RecordingPickerRow
                    key={`fav-R${event.camera_identifier}-${event.id}`}
                    event={event}
                    cameraName={
                      cameraNameById[event.camera_identifier] ??
                      event.camera_identifier
                    }
                    busy={busy}
                    isFavorite
                    onPlay={handlePlay}
                    onToggleFavorite={toggleFavorite}
                  />
                ))
              )}
            </Box>
          </Paper>

          {/* Right: preview + log */}
          <Box
            sx={{
              display: "flex",
              flexDirection: "column",
              gap: 1.5,
              minHeight: 0,
            }}
          >
            {targetCamera && (
              <Paper variant="outlined" sx={{ p: 1 }}>
                <Stack
                  direction="row"
                  alignItems="center"
                  justifyContent="space-between"
                  sx={{ mb: 1 }}
                >
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography variant="subtitle2">
                      {targetCamera.name}
                    </Typography>
                    <Chip
                      size="small"
                      label={isPlaying ? "playing" : "idle"}
                      color={isPlaying ? "success" : "default"}
                      sx={{ height: 20 }}
                    />
                  </Stack>
                  <Stack direction="row" spacing={1}>
                    <Button
                      size="small"
                      variant="outlined"
                      color="error"
                      startIcon={<StopFilledAlt />}
                      disabled={!isPlaying || busy}
                      onClick={handleStop}
                    >
                      Stop
                    </Button>
                  </Stack>
                </Stack>
                <PlaybackPreview
                  camera={targetCamera}
                  isPlaying={isPlaying}
                  currentFile={currentFile}
                />
                {currentFile && (
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{
                      mt: 0.5,
                      display: "block",
                      fontFamily: "monospace",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {currentFile}
                  </Typography>
                )}
                {playMutation.isError && (
                  <Alert severity="error" sx={{ mt: 1 }}>
                    {playMutation.error.response?.data?.error ??
                      playMutation.error.message}
                  </Alert>
                )}
                {stopMutation.isError && (
                  <Alert severity="error" sx={{ mt: 1 }}>
                    {stopMutation.error.response?.data?.error ??
                      stopMutation.error.message}
                  </Alert>
                )}
              </Paper>
            )}
            <Divider />
            <EventLogPanel
              key={targetId || "none"}
              cameraIdentifier={targetId || null}
            />
          </Box>
        </Box>
      )}
    </Container>
  );
}

export default Playback;
