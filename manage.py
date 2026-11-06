#!/usr/bin/env python
import os
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent
candidate_roots = [
    SERVICE_ROOT.parent / 'bikadeli-services' / 'common-services' / 'python',
    SERVICE_ROOT.parent / 'common-services' / 'python',
    SERVICE_ROOT / 'common-services' / 'python',
]
common_python_root = next((candidate for candidate in candidate_roots if candidate.exists()), candidate_roots[0])
sys.path.insert(0, str(common_python_root))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'driver_api.settings')

from django.core.management import execute_from_command_line  # noqa: E402

execute_from_command_line(sys.argv)
