"""Home Assistant MQTT camera."""
from __future__ import annotations

from typing import Final

from .entity import HassMQTTEntity

DOMAIN: Final = "image"


class HassMQTTCamera(HassMQTTEntity):
    """Base class for all Home Assistant MQTT image entities."""

    # These should NOT be overridden.
    domain = DOMAIN

    @property
    def config_payload(self):
        """Return config payload."""
        payload = super().config_payload
        payload["url_topic"] = self.state_topic
        payload["url_template"] = "{{ value_json.state }}"
        return payload
