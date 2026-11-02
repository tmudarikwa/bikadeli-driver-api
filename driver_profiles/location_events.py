import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def publish_driver_location_events(driver_id, order_id, latitude, longitude):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.error(
            "No channel layer configured; driver location event was not published."
        )
        return False

    try:
        async_to_sync(channel_layer.group_send)(
            f"driver_{driver_id}",
            {
                "type": "driver_location",
                "latitude": latitude,
                "longitude": longitude,
            },
        )
        if order_id is not None:
            async_to_sync(channel_layer.group_send)(
                f"order_{order_id}",
                {
                    "type": "driver_location_update",
                    "lat": latitude,
                    "lng": longitude,
                },
            )
    except Exception:
        logger.exception("Failed to publish location events for driver %s.", driver_id)
        return False
    return True
