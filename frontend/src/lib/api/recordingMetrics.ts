import { useQuery } from "@tanstack/react-query";

import { viseronAPI } from "lib/api/client";

export interface MotionMetrics {
  samples: [number, number][]; // [offset_ms, level_pct]
  events: [number, number][]; // [start_ms, end_ms]
}

export interface ObjectMetrics {
  samples: [number, string, number][]; // [offset_ms, label, confidence]
}

export interface DetectionMetrics {
  version: number;
  motion: MotionMetrics;
  objects: ObjectMetrics;
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
