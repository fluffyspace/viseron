import { useQuery } from "@tanstack/react-query";

import { viseronAPI } from "lib/api/client";

export interface TrackedObject {
  track_id?: number;
  label: string;
  confidence: number;
  box: [number, number, number, number]; // [x1, y1, x2, y2] relative
}

export interface EventFrame {
  t: number; // offset_ms from recording start
  m?: number; // motion area 0–100
  o?: TrackedObject[]; // objects detected in this frame
}

export interface DetectionMetrics {
  version: number;
  frames: EventFrame[];
}

interface RecordingMetricsResponse {
  metrics: DetectionMetrics | null;
}

async function fetchRecordingMetrics(
  cameraIdentifier: string,
  recordingId: number,
): Promise<DetectionMetrics | null> {
  const response = await viseronAPI.get<RecordingMetricsResponse>(
    `recordings/${cameraIdentifier}/${recordingId}/metrics`,
  );
  return response.data.metrics;
}

export function useRecordingMetrics(
  cameraIdentifier: string | null,
  recordingId: number | null,
) {
  return useQuery({
    queryKey: ["recording-metrics", cameraIdentifier, recordingId],
    queryFn: () =>
      fetchRecordingMetrics(cameraIdentifier!, recordingId!),
    enabled: !!cameraIdentifier && !!recordingId,
    staleTime: Infinity,
  });
}
