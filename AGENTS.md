# Advanced Thumbnail Agent

Automated YouTube-style thumbnail pipeline: reads rows from Google Sheets, fetches creator images from Google Drive, uses Groq (Llama) for title/layout JSON, generates backgrounds via Pollinations, and composites text + portrait into JPEGs under `output/`.

## Repository layout

| Path | Role |
|------|------|
| `advanced_pipeline.py` | Main orchestrator loop (Sheets → Drive → LLM → image → compose) |
| `credentials.json` | Google service account (local only; never commit) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` / `docker-compose.yml` | Containerized run with `/app/output` volume |
| `output/` | Generated thumbnails (gitignored) |

## Environment

- **Required:** `credentials.json` at repo root with Sheets + Drive readonly scopes.
- **Required:** `GROQ_API_KEY` — set in the shell or `.env`; do not add API keys to tracked files.
- **Optional:** Adjust `SPREADSHEET_ID`, `SHEET_NAME` in `advanced_pipeline.py` if using a different sheet.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY=your_key_here
python advanced_pipeline.py
```

## Run with Docker

```bash
cp .env.example .env   # fill GROQ_API_KEY
docker compose up --build
```

Outputs appear in `./output/` on the host.

## Sheet workflow

Columns **A** (title), **B** (Drive portrait URL), **C** (status), **D** (optional custom background Drive URL). The agent sets `PROCESSING` then `DONE` (or `ERROR` for retry). Skip rows marked `DONE` or `PROCESSING`. Optional reference JPGs in `assets/references/` steer fallback colors.

## Coding conventions

- Python 3.10+; keep dependencies in `requirements.txt`.
- Image work uses **Pillow**; fonts assume DejaVu/Liberation paths inside Docker (`/usr/share/fonts/...`).
- Prefer `os.getenv` for secrets; remove hardcoded keys if you touch auth code.
- Preserve flush on `print` calls — logs are consumed in Docker.
- Do not commit `credentials.json`, `.env`, or `output/*.jpg`.

## Safe changes

- Typography, colors, layout coordinates, and LLM prompts in `advanced_pipeline.py`.
- Retry/timeouts on `http_session` and Google API calls.
- New env-driven config instead of editing constants when possible.

## Ask before changing

- `SPREADSHEET_ID` or service account permissions.
- Replacing Groq model or Pollinations URL contract.
- Removing fail-safe `default_response` paths (pipeline depends on them when the API fails).

## Verification

After code changes, run one cycle against a test sheet row or dry-run the functions you modified. With Docker: `docker compose build && docker compose up` and confirm a file lands in `output/`.
