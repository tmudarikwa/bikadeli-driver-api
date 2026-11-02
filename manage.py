#!/usr/bin/env python
import os
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = SERVICE_ROOT.parent
sys.path.insert(0, str(WORKSPACE_ROOT / 'common-services' / 'python'))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'driver_api.settings')

from django.core.management import execute_from_command_line  # noqa: E402

execute_from_command_line(sys.argv)
