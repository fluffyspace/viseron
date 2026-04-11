import {
  UseMutationResult,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { viseronAPI } from "lib/api/client";
import * as types from "lib/types";

export type FavoriteRecording = types.PlaybackFavorite;

const favoritesKey = ["playback", "favorites"] as const;

export const favoriteKey = (
  camera_identifier: string,
  recording_id: number,
): string => `${camera_identifier}:${recording_id}`;

async function fetchFavorites(): Promise<FavoriteRecording[]> {
  const response = await viseronAPI.get<{ favorites: FavoriteRecording[] }>(
    "playback/favorites",
  );
  return response.data.favorites ?? [];
}

async function postFavorite(recording_id: number): Promise<FavoriteRecording> {
  const response = await viseronAPI.post<FavoriteRecording>(
    "playback/favorites",
    { recording_id },
  );
  return response.data;
}

async function deleteFavorite(
  camera_identifier: string,
  recording_id: number,
): Promise<void> {
  await viseronAPI.delete(
    `playback/favorites/${camera_identifier}/${recording_id}`,
  );
}

/**
 * Hook returning the playback favorites list plus mutators. Backed by the
 * server-side ``/api/v1/playback/favorites`` endpoints — the actual video
 * file is copied into a host-mounted directory outside any storage tier so
 * favorites stay playable indefinitely (even after the original recording
 * is purged from tier 3).
 */
export function usePlaybackFavorites() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: favoritesKey,
    queryFn: fetchFavorites,
    staleTime: 30_000,
  });

  const favorites: FavoriteRecording[] = query.data ?? [];
  const favoriteSet = new Set(
    favorites.map((f) => favoriteKey(f.camera_identifier, f.id)),
  );

  const isFavorite = (event: types.CameraRecordingEvent): boolean =>
    favoriteSet.has(favoriteKey(event.camera_identifier, event.id));

  const addMutation: UseMutationResult<
    FavoriteRecording,
    types.APIErrorResponse,
    types.CameraRecordingEvent
  > = useMutation({
    mutationFn: (event: types.CameraRecordingEvent) => postFavorite(event.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: favoritesKey });
    },
  });

  const removeMutation: UseMutationResult<
    void,
    types.APIErrorResponse,
    { camera_identifier: string; recording_id: number }
  > = useMutation({
    mutationFn: ({ camera_identifier, recording_id }) =>
      deleteFavorite(camera_identifier, recording_id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: favoritesKey });
    },
  });

  const toggle = (event: types.CameraRecordingEvent) => {
    if (isFavorite(event)) {
      removeMutation.mutate({
        camera_identifier: event.camera_identifier,
        recording_id: event.id,
      });
    } else {
      addMutation.mutate(event);
    }
  };

  const remove = (camera_identifier: string, recording_id: number) => {
    removeMutation.mutate({ camera_identifier, recording_id });
  };

  return {
    favorites,
    isFavorite,
    toggle,
    remove,
    isLoading: query.isPending,
    isError: query.isError,
  };
}
