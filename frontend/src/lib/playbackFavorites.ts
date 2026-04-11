import { useCallback, useEffect, useState } from "react";

import * as types from "lib/types";

const STORAGE_KEY = "viseron.playback.favorites.v1";
const STORAGE_EVENT = "viseron.playback.favorites.changed";

export type FavoriteRecording = types.CameraRecordingEvent;

export type FavoritesMap = Record<string, FavoriteRecording>;

export const favoriteKey = (
  camera_identifier: string,
  recording_id: number,
): string => `${camera_identifier}:${recording_id}`;

function readFromStorage(): FavoritesMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      return parsed as FavoritesMap;
    }
    return {};
  } catch {
    return {};
  }
}

function writeToStorage(map: FavoritesMap) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
    window.dispatchEvent(new CustomEvent(STORAGE_EVENT));
  } catch {
    // Quota or serialization failure — silently ignore. The in-memory state
    // still works for this session.
  }
}

/**
 * Hook returning the playback favorites map plus mutators. Persists to
 * localStorage and reacts to changes from other tabs / other consumers via a
 * custom window event so multiple components stay in sync.
 */
export function usePlaybackFavorites() {
  const [favorites, setFavorites] = useState<FavoritesMap>(() =>
    readFromStorage(),
  );

  useEffect(() => {
    const refresh = () => setFavorites(readFromStorage());
    window.addEventListener(STORAGE_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(STORAGE_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  const isFavorite = useCallback(
    (event: types.CameraRecordingEvent) =>
      favoriteKey(event.camera_identifier, event.id) in favorites,
    [favorites],
  );

  const toggle = useCallback((event: types.CameraRecordingEvent) => {
    setFavorites((prev) => {
      const key = favoriteKey(event.camera_identifier, event.id);
      const next = { ...prev };
      if (key in next) {
        delete next[key];
      } else {
        next[key] = event;
      }
      writeToStorage(next);
      return next;
    });
  }, []);

  const remove = useCallback(
    (camera_identifier: string, recording_id: number) => {
      setFavorites((prev) => {
        const key = favoriteKey(camera_identifier, recording_id);
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        writeToStorage(next);
        return next;
      });
    },
    [],
  );

  return { favorites, isFavorite, toggle, remove };
}
