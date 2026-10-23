from services.role_api.runtime import configure_service

configure_service('driver-api', 'driver_api.settings')

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
