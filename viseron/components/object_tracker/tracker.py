"""IoU-based multi-object tracker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from viseron.domains.object_detector.const import EVENT_OBJECT_DETECTOR_RESULT
from viseron.events import EventData

from .const import EVENT_OBJECT_TRACKER_RESULT

if TYPE_CHECKING:
    from viseron import Event, Viseron
    from viseron.domains.object_detector import EventObjectDetectorScannerResult
    from viseron.domains.object_detector.detected_object import DetectedObject


@dataclass
class Track:
    """A single tracked object."""

    track_id: int
    label: str
    confidence: float
    rel_x1: float
    rel_y1: float
    rel_x2: float
    rel_y2: float
    disappeared: int = 0


@dataclass
class EventObjectTrackerResult(EventData):
    """Event data emitted after tracking update."""

    camera_identifier: str
    tracked_objects: list[DetectedObject]


def _iou(track: Track, obj: DetectedObject) -> float:
    """Compute Intersection over Union between a track and a detection."""
    x1 = max(track.rel_x1, obj.rel_x1)
    y1 = max(track.rel_y1, obj.rel_y1)
    x2 = min(track.rel_x2, obj.rel_x2)
    y2 = min(track.rel_y2, obj.rel_y2)

    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter == 0.0:
        return 0.0

    area_track = (track.rel_x2 - track.rel_x1) * (track.rel_y2 - track.rel_y1)
    area_obj = (obj.rel_x2 - obj.rel_x1) * (obj.rel_y2 - obj.rel_y1)
    union = area_track + area_obj - inter
    if union <= 0.0:
        return 0.0
    return inter / union


class ObjectTracker:
    """IoU-based multi-object tracker.

    Matches new detections to existing tracks using IoU and label matching.
    Assigns persistent track_id to each DetectedObject.
    """

    def __init__(
        self,
        vis: Viseron,
        camera_identifier: str,
        min_iou: float,
        max_disappeared: int,
    ) -> None:
        self._vis = vis
        self._camera_identifier = camera_identifier
        self._min_iou = min_iou
        self._max_disappeared = max_disappeared
        self._logger = logging.getLogger(
            f"{__name__}.{camera_identifier}"
        )

        self._tracks: dict[int, Track] = {}
        self._next_track_id = 1
        self._tracked_objects: list[DetectedObject] = []

        self._listeners: list[Callable] = []
        self._listeners.append(
            vis.listen_event(
                EVENT_OBJECT_DETECTOR_RESULT.format(
                    camera_identifier=camera_identifier
                ),
                self._handle_detection_result,
            )
        )

    @property
    def tracked_objects(self) -> list[DetectedObject]:
        """Return the latest list of tracked objects."""
        return self._tracked_objects

    def _handle_detection_result(
        self, event: Event[EventObjectDetectorScannerResult]
    ) -> None:
        """Process new object detection results and update tracks."""
        objects = event.data.objects
        tracked = self.update(objects)
        self._tracked_objects = tracked
        self._vis.dispatch_event(
            EVENT_OBJECT_TRACKER_RESULT.format(
                camera_identifier=self._camera_identifier
            ),
            EventObjectTrackerResult(
                camera_identifier=self._camera_identifier,
                tracked_objects=tracked,
            ),
            store=False,
        )

    def update(self, detections: list[DetectedObject]) -> list[DetectedObject]:
        """Match detections to tracks and return objects with track_ids assigned."""
        if not self._tracks and not detections:
            return []

        # No existing tracks — register all detections as new
        if not self._tracks:
            for det in detections:
                self._register(det)
            return detections

        # No detections — mark all tracks as disappeared
        if not detections:
            self._mark_all_disappeared()
            return []

        # Build IoU scores: (iou, track_id, det_index)
        scores: list[tuple[float, int, int]] = []
        track_ids = list(self._tracks.keys())
        for tid in track_ids:
            track = self._tracks[tid]
            for di, det in enumerate(detections):
                if track.label != det.label:
                    continue
                iou = _iou(track, det)
                if iou >= self._min_iou:
                    scores.append((iou, tid, di))

        # Greedy matching — highest IoU first
        scores.sort(key=lambda x: x[0], reverse=True)
        matched_tracks: set[int] = set()
        matched_dets: set[int] = set()

        for iou_val, tid, di in scores:
            if tid in matched_tracks or di in matched_dets:
                continue
            # Update track with new detection
            det = detections[di]
            track = self._tracks[tid]
            track.rel_x1 = det.rel_x1
            track.rel_y1 = det.rel_y1
            track.rel_x2 = det.rel_x2
            track.rel_y2 = det.rel_y2
            track.confidence = det.confidence
            track.disappeared = 0
            det.track_id = track.track_id
            matched_tracks.add(tid)
            matched_dets.add(di)

        # Unmatched tracks — increment disappeared
        for tid in track_ids:
            if tid not in matched_tracks:
                self._tracks[tid].disappeared += 1
                if self._tracks[tid].disappeared > self._max_disappeared:
                    del self._tracks[tid]

        # Unmatched detections — new tracks
        for di, det in enumerate(detections):
            if di not in matched_dets:
                self._register(det)

        return detections

    def _register(self, det: DetectedObject) -> None:
        """Register a new track from a detection."""
        tid = self._next_track_id
        self._next_track_id += 1
        self._tracks[tid] = Track(
            track_id=tid,
            label=det.label,
            confidence=det.confidence,
            rel_x1=det.rel_x1,
            rel_y1=det.rel_y1,
            rel_x2=det.rel_x2,
            rel_y2=det.rel_y2,
        )
        det.track_id = tid

    def _mark_all_disappeared(self) -> None:
        """Mark all tracks as disappeared and remove expired ones."""
        expired = []
        for tid, track in self._tracks.items():
            track.disappeared += 1
            if track.disappeared > self._max_disappeared:
                expired.append(tid)
        for tid in expired:
            del self._tracks[tid]

    def reset(self) -> None:
        """Reset all tracks. Called when recording stops."""
        self._tracks.clear()
        self._next_track_id = 1
        self._tracked_objects = []

    def unload(self) -> None:
        """Unload tracker and remove event listeners."""
        for unsubscribe in self._listeners:
            unsubscribe()
        self._tracks.clear()
