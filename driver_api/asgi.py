from services.role_api.runtime import configure_service

configure_service('driver-api', 'driver_api.settings')

from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_application = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from zimdelivery.routing import websocket_urlpatterns  # noqa: E402
from zimdelivery.ws_auth import JWTWebSocketMiddleware  # noqa: E402

application = ProtocolTypeRouter(
    {
        'http': django_asgi_application,
        'websocket': JWTWebSocketMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
