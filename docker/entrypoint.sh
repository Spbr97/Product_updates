#!/bin/sh
# Container entrypoint.
#
# Compose already gates startup on the database healthcheck, so this does not poll for
# Postgres. It exists to keep signal handling correct: `exec` makes the application PID 1's
# child replacement, so SIGTERM reaches uvicorn and the worker directly and they shut down
# cleanly instead of being killed after the grace period.
set -eu

exec "$@"
