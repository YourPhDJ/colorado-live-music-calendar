name: Update Colorado Live Music Events

on:
  schedule:
    # 6:00 UTC = midnight Mountain Time (MDT, UTC-6)
    # Adjust to 7:00 UTC (0:00 MST) in winter if you want strict midnight
    - cron: '0 6 * * *'
  workflow_dispatch:   # also allows a manual "Run workflow" button in GitHub UI

jobs:
  update-events:
    runs-on: ubuntu-latest

    permissions:
      contents: write   # needed to commit + push index.html

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install requests

      - name: Fetch events and patch index.html
        env:
          JAMBASE_API_KEY: ${{ secrets.JAMBASE_API_KEY }}
        run: python scripts/update_events.py

      - name: Commit and push if index.html changed
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add index.html
          if git diff --staged --quiet; then
            echo "No new events — index.html unchanged, skipping commit."
          else
            DATESTAMP=$(date -u +'%Y-%m-%d')
            git commit -m "chore: refresh live music events for ${DATESTAMP}"
            git push
          fi
