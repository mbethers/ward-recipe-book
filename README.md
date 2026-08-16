# University Ward Recipe Book

A lightweight, mobile-first recipe book for the ward's linger-longer potlucks.
No login required to browse or submit. Submissions go into an admin review
queue before they're published.

- **Submit a recipe** by typing it in, or by uploading a photo/PDF of a recipe
  card - Claude reads it automatically.
- **Browse/search** by name, ingredient, category, or theme (e.g. "Cold Cereal").
- **Admin** (`/admin`) reviews pending submissions, edits fields inline, and
  approves or rejects.

## Stack
- Flask (Python), server-rendered HTML - no frontend framework
- Postgres + Storage, both via one free [Supabase](https://supabase.com) project
  (Render's free web services have no persistent disk, so this is where the
  data and photos actually live - Render just runs the app)
- Claude Haiku 4.5 for recipe-card reading, with a "reprocess with Sonnet"
  admin button for hard-to-read handwriting
- Hosted on [Render](https://render.com) free tier

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env: ADMIN_PASSWORD, FLASK_SECRET_KEY, ANTHROPIC_API_KEY,
# SUPABASE_URL, SUPABASE_SERVICE_KEY, DATABASE_URL
python3 app.py
```

Then open http://localhost:5050

## One-time setup before first deploy

1. **Anthropic API key** - console.anthropic.com -> API Keys -> Create Key.
2. **Supabase project** - supabase.com -> New project. Then:
   - **Storage**: create a bucket named `recipe-photos`, set it to **public**.
   - **Database**: Project Settings -> Database -> Connection string ->
     "Transaction pooler" - that's your `DATABASE_URL`. The app creates its
     own tables on first run (see `db.py`), no manual SQL needed.
   - **API keys**: Project Settings -> API -> copy the Project URL
     (`SUPABASE_URL`) and the **service_role** secret key (`SUPABASE_SERVICE_KEY`,
     never the anon key - this one must stay server-side only).
3. **Render** - New -> Web Service -> connect this GitHub repo -> it picks up
   `render.yaml` automatically. Fill in the env vars marked `sync: false` in
   the Render dashboard (ADMIN_PASSWORD, ANTHROPIC_API_KEY, SUPABASE_URL,
   SUPABASE_SERVICE_KEY, DATABASE_URL).

## Notes
- Render's free tier sleeps after inactivity - the first visit after a quiet
  spell takes ~30-60 seconds to wake up. Normal, no action needed.
- Theme tags grow over time from `/admin/themes` - no code changes or
  redeploys needed for a new linger longer theme.
- AI-parsed submissions go straight to the admin queue (not shown back to the
  submitter first) to keep the submission flow simple for less tech-savvy
  members. Flip this later if you'd rather submitters confirm the reading
  themselves.
