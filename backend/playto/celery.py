"""Celery application bootstrap.

Started either as a worker (`celery -A playto worker -l info`) or as a beat
scheduler (`celery -A playto beat -l info`). The beat schedule itself lives in
settings.CELERY_BEAT_SCHEDULE so all tunables are co-located.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "playto.settings")

app = Celery("playto")
# All settings prefixed with CELERY_ in Django settings are picked up here.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
