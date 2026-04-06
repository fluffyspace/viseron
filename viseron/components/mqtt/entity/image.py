"""MQTT image entity."""
import json

from viseron.components.mqtt.helpers import PublishPayload
from viseron.domains.camera.entity.image import CameraImage
from viseron.helpers.entity.image import ImageEntity

from . import MQTTEntity


class ImageMQTTEntity(MQTTEntity[ImageEntity]):
    """Base image MQTT entity class."""

    @property
    def state_topic(self) -> str:
        """Return state topic."""
        return (
            f"{self._mqtt.base_topic}/{self.entity.domain}/"
            f"{self.entity.object_id}/image"
        )

    @property
    def attributes_topic(self) -> str:
        """Return attributes topic."""
        return (
            f"{self._mqtt.base_topic}/{self.entity.domain}/"
            f"{self.entity.object_id}/attributes"
        )

    def _get_snapshot_url(self) -> str | None:
        """Return the snapshot URL for the entity's camera."""
        if isinstance(self.entity, CameraImage):
            camera = self.entity._camera
            return (
                f"/api/v1/camera/{camera.identifier}"
                f"/snapshot?access_token={camera.access_token}"
            )
        return None

    def publish_state(self) -> None:
        """Publish state to MQTT."""
        snapshot_url = self._get_snapshot_url()

        payload = {}
        payload["state"] = snapshot_url
        payload["attributes"] = self.entity.attributes
        self._mqtt.publish(
            PublishPayload(
                self.state_topic,
                json.dumps(payload),
                retain=True,
            )
        )
