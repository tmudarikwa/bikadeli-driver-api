from importlib import import_module

_base_settings = import_module('zimdelivery.settings')
for _setting_name in dir(_base_settings):
    if _setting_name.isupper():
        globals()[_setting_name] = getattr(_base_settings, _setting_name)

ROOT_URLCONF = 'driver_api.urls'
WSGI_APPLICATION = 'driver_api.wsgi.application'
ASGI_APPLICATION = 'driver_api.asgi.application'
ROLE_API_NAME = 'driver-api'
