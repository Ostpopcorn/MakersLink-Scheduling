#!/bin/sh
# Container entrypoint: apply migrations, then serve with gunicorn.
# Migrations are created in development and committed; they are never
# generated here. Static files are collected when the image is built and
# served by WhiteNoise.
#
# --no-control-socket: gunicorn 25.1+ opens a management socket for its
# gunicornc tool under $HOME, and the container user has no home directory.
# Nothing here uses gunicornc.
set -e
python3 ./manage.py migrate --noinput
exec gunicorn makerslink.wsgi:application \
    --bind 0.0.0.0:8000 --workers 2 --threads 4 \
    --no-control-socket \
    --access-logfile - --error-logfile -
