#!/bin/bash

# Switch to the directory where the script is located
cd "$(dirname "$0")"

echo "=== Starting Radar Update: $(date -u +'%Y-%m-%d %H:%M:%S UTC') ==="

# 1. Pull latest code (in case of manual changes by you)
git pull --rebase origin main

# 2. Activate Python virtual environment
source venv/bin/activate

# 3. Run the processor
python process_latest.py

# 4. Check for changes and push
git config --global user.name "GCP Radar Server"
git config --global user.email "radar-bot@gcp.local"

git add static/ frames.json

# If there are changes to commit, commit and push
if ! git diff --cached --quiet; then
    git commit -m "Update radar frames $(date -u +'%Y-%m-%d %H:%M UTC')"
    # Pull rebase again just in case there was a push during processing
    git pull --rebase origin main
    git push origin main
    echo "Changes pushed to GitHub successfully."
else
    echo "No new frames or changes to commit."
fi

echo "=== Finished: $(date -u +'%Y-%m-%d %H:%M:%S UTC') ==="
