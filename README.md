# Visual monitor

`monitor.py` keeps a baseline screenshot per URL per device. On each pass it captures a full-page JPEG through the Crawlbase Crawling API, downloads that JPEG immediately, and compares it with the baseline. SSIM and a changed-area ratio decide whether a vision model writes a paragraph. When that paragraph names a pricing, hero, or banner change, the same render is read back from Cloud Storage by `rid`. The alert is a JSON file beside the heatmap. A Slack webhook is used only when `SLACK_WEBHOOK_URL` is set.

Python 3.11 or newer. From the `code` directory:

## Windows

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy final\.env.example final\.env
python -m pytest final
python final\monitor.py --once
```

## Linux

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp final/.env.example final/.env
python -m pytest final
python final/monitor.py --once
```

## macOS

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp final/.env.example final/.env
python -m pytest final
python final/monitor.py --once
```

Put `CRAWLBASE_JS_TOKEN` in `final/.env`. Add `OPENAI_API_KEY` when a crossing should be narrated. Leave `SLACK_WEBHOOK_URL` empty to keep the alert on disk.

`python final/monitor.py --ack 3` marks alert 3 acknowledged and makes that shot the new baseline. The baseline also rotates on its own after `rotate_after_n` compared runs.

Hourly cron, with the virtualenv next to `monitor.py`:

```cron
0 * * * * cd /opt/visual-monitor && .venv/bin/python monitor.py --once
```

GitHub Actions, scheduled the same way:

```yaml
name: visual-monitor
on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:
jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install -r requirements.txt
        working-directory: code
      - run: python monitor.py --once
        working-directory: code/final
        env:
          CRAWLBASE_JS_TOKEN: ${{ secrets.CRAWLBASE_JS_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

`pytest` does not call the network. The live pass is `monitor.py --once`.

---

Copyright 2026 [Crawlbase](https://crawlbase.com/)
