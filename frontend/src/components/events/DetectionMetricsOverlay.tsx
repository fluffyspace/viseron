import Box from "@mui/material/Box";
import { useTheme } from "@mui/material/styles";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from "react";

import { DetectionMetrics } from "lib/api/recordingMetrics";

const OVERLAY_HEIGHT = 60;
const CURSOR_COLOR = "rgba(255, 255, 255, 0.9)";

const LABEL_COLORS: Record<string, string> = {
  person: "#42A5F5",
  car: "#EF5350",
  truck: "#EF5350",
  vehicle: "#EF5350",
  dog: "#AB47BC",
  cat: "#AB47BC",
  animal: "#AB47BC",
  bird: "#AB47BC",
  bicycle: "#FFA726",
  motorcycle: "#FFA726",
};

function getLabelColor(label: string): string {
  if (LABEL_COLORS[label]) return LABEL_COLORS[label];
  let hash = 0;
  for (let i = 0; i < label.length; i++) {
    hash = label.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 70%, 60%)`;
}

interface DetectionMetricsOverlayProps {
  metrics: DetectionMetrics;
  durationMs: number;
  startTimestamp: number; // Unix seconds of recording start_time
  playingDateRef: React.MutableRefObject<number>; // Unix seconds, updated by SyncManager
  onSeek: (timestamp: number) => void; // Seeks to Unix timestamp
}

export function DetectionMetricsOverlay({
  metrics,
  durationMs,
  startTimestamp,
  playingDateRef,
  onSeek,
}: DetectionMetricsOverlayProps) {
  const theme = useTheme();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const offscreenRef = useRef<HTMLCanvasElement | null>(null);
  const animFrameRef = useRef<number>(0);
  const sizeRef = useRef({ width: 0, height: 0 });

  const objectLabels = useMemo(() => {
    const labels = new Set<string>();
    for (const sample of metrics.objects.samples) {
      labels.add(sample[1]);
    }
    return Array.from(labels);
  }, [metrics.objects.samples]);

  // Draw static elements to offscreen canvas
  const drawStatic = useCallback(
    (width: number, height: number) => {
      const offscreen = document.createElement("canvas");
      offscreen.width = width;
      offscreen.height = height;
      const ctx = offscreen.getContext("2d");
      if (!ctx || durationMs <= 0) return offscreen;

      const dpr = window.devicePixelRatio || 1;
      const logicalW = width / dpr;
      const logicalH = height / dpr;
      ctx.scale(dpr, dpr);

      // Motion event regions
      ctx.fillStyle = "rgba(255, 152, 0, 0.15)";
      for (const [startMs, endMs] of metrics.motion.events) {
        const x1 = (startMs / durationMs) * logicalW;
        const x2 = (endMs / durationMs) * logicalW;
        ctx.fillRect(x1, 0, x2 - x1, logicalH);
      }

      // Motion waveform
      const motionSamples = metrics.motion.samples;
      if (motionSamples.length > 1) {
        ctx.beginPath();
        ctx.moveTo(0, logicalH);
        for (const [offsetMs, levelPct] of motionSamples) {
          const x = (offsetMs / durationMs) * logicalW;
          const y = logicalH - (levelPct / 100) * (logicalH * 0.8);
          ctx.lineTo(x, y);
        }
        const lastX =
          (motionSamples[motionSamples.length - 1][0] / durationMs) * logicalW;
        ctx.lineTo(lastX, logicalH);
        ctx.closePath();
        ctx.fillStyle = "rgba(76, 175, 80, 0.45)";
        ctx.fill();

        ctx.beginPath();
        for (let i = 0; i < motionSamples.length; i++) {
          const [offsetMs, levelPct] = motionSamples[i];
          const x = (offsetMs / durationMs) * logicalW;
          const y = logicalH - (levelPct / 100) * (logicalH * 0.8);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = "rgba(76, 175, 80, 0.8)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      // Object markers
      const markerY = 8;
      const markerRadius = 3;
      for (const [offsetMs, label] of metrics.objects.samples) {
        const x = (offsetMs / durationMs) * logicalW;
        ctx.beginPath();
        ctx.arc(x, markerY, markerRadius, 0, Math.PI * 2);
        ctx.fillStyle = getLabelColor(label as string);
        ctx.fill();
      }

      return offscreen;
    },
    [metrics, durationMs],
  );

  // Handle resize
  useLayoutEffect(() => {
    const container = containerRef.current;
    const canvas = canvasRef.current;
    if (!container || !canvas) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width } = entry.contentRect;
        const dpr = window.devicePixelRatio || 1;
        const pixelW = Math.floor(width * dpr);
        const pixelH = Math.floor(OVERLAY_HEIGHT * dpr);
        if (
          pixelW !== sizeRef.current.width ||
          pixelH !== sizeRef.current.height
        ) {
          sizeRef.current = { width: pixelW, height: pixelH };
          canvas.width = pixelW;
          canvas.height = pixelH;
          offscreenRef.current = drawStatic(pixelW, pixelH);
        }
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [drawStatic]);

  // Animation loop — reads playingDateRef directly, no re-renders needed
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || durationMs <= 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let running = true;
    const loop = () => {
      if (!running) return;
      const { width, height } = sizeRef.current;
      if (width === 0 || height === 0) {
        animFrameRef.current = requestAnimationFrame(loop);
        return;
      }
      const dpr = window.devicePixelRatio || 1;
      const logicalW = width / dpr;
      const logicalH = height / dpr;

      ctx.clearRect(0, 0, width, height);
      ctx.setTransform(1, 0, 0, 1, 0, 0);

      // Background
      ctx.fillStyle =
        theme.palette.mode === "dark"
          ? "rgba(0, 0, 0, 0.5)"
          : "rgba(0, 0, 0, 0.35)";
      ctx.fillRect(0, 0, width, height);

      // Static layer
      if (offscreenRef.current) {
        ctx.drawImage(offscreenRef.current, 0, 0);
      }

      // Playback cursor
      const currentOffsetMs =
        (playingDateRef.current - startTimestamp) * 1000;
      const proportion = Math.max(
        0,
        Math.min(currentOffsetMs / durationMs, 1),
      );
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cursorX = proportion * logicalW;
      ctx.beginPath();
      ctx.moveTo(cursorX, 0);
      ctx.lineTo(cursorX, logicalH);
      ctx.strokeStyle = CURSOR_COLOR;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.setTransform(1, 0, 0, 1, 0, 0);

      animFrameRef.current = requestAnimationFrame(loop);
    };
    animFrameRef.current = requestAnimationFrame(loop);
    return () => {
      running = false;
      cancelAnimationFrame(animFrameRef.current);
    };
  }, [durationMs, startTimestamp, playingDateRef, theme.palette.mode]);

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current;
      if (!canvas || durationMs <= 0) return;
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const proportion = x / rect.width;
      const offsetMs = Math.max(
        0,
        Math.min(proportion * durationMs, durationMs),
      );
      const timestamp = startTimestamp + offsetMs / 1000;
      onSeek(timestamp);
    },
    [durationMs, startTimestamp, onSeek],
  );

  if (durationMs <= 0) return null;

  return (
    <Box
      ref={containerRef}
      sx={{
        position: "absolute",
        bottom: 0,
        left: 0,
        right: 0,
        height: OVERLAY_HEIGHT,
        zIndex: 2,
        pointerEvents: "auto",
      }}
    >
      <canvas
        ref={canvasRef}
        onClick={handleClick}
        style={{
          width: "100%",
          height: "100%",
          cursor: "pointer",
          display: "block",
        }}
      />
      {objectLabels.length > 0 && (
        <Box
          sx={{
            position: "absolute",
            top: 2,
            right: 4,
            display: "flex",
            gap: 0.5,
            pointerEvents: "none",
          }}
        >
          {objectLabels.map((label) => (
            <Box
              key={label}
              sx={{
                display: "flex",
                alignItems: "center",
                gap: 0.3,
                fontSize: "9px",
                color: "rgba(255,255,255,0.8)",
                lineHeight: 1,
              }}
            >
              <Box
                sx={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  backgroundColor: getLabelColor(label),
                  flexShrink: 0,
                }}
              />
              {label}
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
}
