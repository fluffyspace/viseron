import {
  UseMutationResult,
  UseQueryResult,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { viseronAPI } from "lib/api/client";
import * as types from "lib/types";

const playbackKey = (camera_identifier: string) =>
  ["playback", camera_identifier] as const;

async function fetchPlaybackState(
  camera_identifier: string,
): Promise<types.PlaybackState> {
  const response = await viseronAPI.get<types.PlaybackState>(
    `playback/${camera_identifier}`,
  );
  return response.data;
}

async function postPlay(
  camera_identifier: string,
  recording_id: number,
): Promise<types.PlaybackPlayResponse> {
  const response = await viseronAPI.post<types.PlaybackPlayResponse>(
    `playback/${camera_identifier}/play`,
    { recording_id },
  );
  return response.data;
}

async function postStop(
  camera_identifier: string,
): Promise<types.PlaybackStopResponse> {
  const response = await viseronAPI.post<types.PlaybackStopResponse>(
    `playback/${camera_identifier}/stop`,
  );
  return response.data;
}

export function usePlaybackState(
  camera_identifier: string | null,
): UseQueryResult<types.PlaybackState, types.APIErrorResponse> {
  return useQuery({
    queryKey: playbackKey(camera_identifier ?? ""),
    queryFn: () => fetchPlaybackState(camera_identifier as string),
    enabled: !!camera_identifier,
    refetchInterval: 2000,
  });
}

type PlayVariables = {
  camera_identifier: string;
  recording_id: number;
};

export function usePlayRecording(): UseMutationResult<
  types.PlaybackPlayResponse,
  types.APIErrorResponse,
  PlayVariables
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ camera_identifier, recording_id }: PlayVariables) =>
      postPlay(camera_identifier, recording_id),
    onSuccess: async (_data, variables) => {
      await queryClient.invalidateQueries({
        queryKey: playbackKey(variables.camera_identifier),
      });
    },
  });
}

export function useStopPlayback(): UseMutationResult<
  types.PlaybackStopResponse,
  types.APIErrorResponse,
  string
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (camera_identifier: string) => postStop(camera_identifier),
    onSuccess: async (_data, camera_identifier) => {
      await queryClient.invalidateQueries({
        queryKey: playbackKey(camera_identifier),
      });
    },
  });
}
