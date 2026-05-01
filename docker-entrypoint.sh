#!/bin/sh
set -e

echo "⏳ Waiting for MySQL to be ready..."
until nc -z -v -w30 "$MYSQL_HOST" "$MYSQL_PORT"; do
  echo "   MySQL not ready yet — sleeping 2s..."
  sleep 2
done
echo "✅ MySQL is up!"

echo "🌱 Running database seed..."
python seed.py

echo "🚀 Starting Flask application..."
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 "app:app"
