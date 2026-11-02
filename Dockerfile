FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=driver_api.settings

WORKDIR /app
COPY driver_api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --retries=5 --timeout=120 -r /app/requirements.txt

COPY --chown=10001:10001 common-services/python/bikadeli_common /app/bikadeli_common
COPY --chown=10001:10001 driver_api/manage.py /app/manage.py
COPY --chown=10001:10001 driver_api/driver_api /app/driver_api
COPY --chown=10001:10001 driver_api/driver_profiles /app/driver_profiles

RUN adduser --system --uid 10001 --no-create-home app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=4).read()"]
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=30", "driver_api.wsgi:application"]
