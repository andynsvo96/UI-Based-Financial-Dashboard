#!/bin/bash

set -u

cd "$(dirname "$0")" || exit 1

PROFILE_DIR="chrome_profiles/citizens"

pause() {
  echo
  read -r -p "Press Return to continue..."
}

echo "Citizens Profile Reset"
echo "======================"
echo
echo "Close all Chrome windows that are using the Citizens profile before continuing."
echo "This will back up the existing profile folder instead of deleting it."
pause

if [ ! -d "$PROFILE_DIR" ]; then
  echo "No Citizens profile exists yet."
  mkdir -p "$PROFILE_DIR"
  echo "Created a fresh Citizens profile folder."
  pause
  exit 0
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="chrome_profiles/citizens_backup_$STAMP"

if ! mv "$PROFILE_DIR" "$BACKUP_DIR"; then
  echo "Failed to move the Citizens profile. Make sure all Citizens Chrome windows are closed."
  pause
  exit 1
fi

mkdir -p "$PROFILE_DIR"
echo "Backed up old profile to $BACKUP_DIR"
echo "Created a fresh Citizens profile folder."
pause

