#!/bin/bash

set -u

cd "$(dirname "$0")" || exit 1

VENV_DIR=".venv"
APP_URL="http://127.0.0.1:5050"

pause() {
  echo
  read -r -p "Press Return to close this window..."
}

if command -v python3 >/dev/null 2>&1; then
  PYTHON_EXE="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_EXE="python"
else
  echo "Python was not found on PATH."
  echo "Install Python 3.10+ from https://www.python.org/downloads/ and try again."
  pause
  exit 1
fi

echo "Financial Dashboard"
echo "==================="
echo

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating local virtual environment..."
  "$PYTHON_EXE" -m venv "$VENV_DIR"
  if [ $? -ne 0 ]; then
    echo "Failed to create virtual environment."
    pause
    exit 1
  fi
fi

echo "Installing or updating dependencies..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
if [ $? -ne 0 ]; then
  echo "Failed to update pip."
  pause
  exit 1
fi

"$VENV_DIR/bin/python" -m pip install -r requirements.txt
if [ $? -ne 0 ]; then
  echo "Failed to install dependencies from requirements.txt."
  pause
  exit 1
fi

echo
echo "Starting Financial Dashboard at $APP_URL"
echo "Leave this window open while using the app."
echo "Press Ctrl+C in this window to stop the server."
echo

"$VENV_DIR/bin/python" server.py &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1
  fi
}
trap cleanup INT TERM EXIT

for _ in {1..30}; do
  if curl -fsS "$APP_URL" >/dev/null 2>&1; then
    open "$APP_URL"
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.5
done

echo "The server did not become available at $APP_URL."
echo "Check the messages above for startup errors."
wait "$SERVER_PID"
