#!/usr/bin/env python
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))

from services.role_api.runtime import configure_service  # noqa: E402

configure_service('driver-api', 'driver_api.settings')

from django.core.management import execute_from_command_line  # noqa: E402

execute_from_command_line(sys.argv)
