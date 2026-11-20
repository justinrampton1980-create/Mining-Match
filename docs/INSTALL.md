# Installing Mining Match on your own computer

For trying the platform and developing it. To put it on the internet, see [DEPLOYMENT.md](DEPLOYMENT.md).

## You need

- Python 3.11 or newer
- Node.js 18 or newer (only to run the JavaScript tests)
- Google Chrome (only for the browser tests)

## Steps

```bash
git clone https://github.com/justinrampton1980-create/Mining-Match.git
cd Mining-Match/python
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Create the database and the first admin account (works once, while no admin exists)
python -c "import data_layer as dl, access_control as ac, auth; c = dl.connect('mining_match.db'); dl.init_schema(c); dl.seed_catalog(c); ac.create_admin(c, input('Admin email: '), auth.hash_password(input('Password: ')))"

# Start it
MM_DB=mining_match.db MM_FILES=uploads MM_BASE_URL=http://localhost:8000 MM_SECURE_COOKIES=0 uvicorn api:app --reload
```

Open http://localhost:8000 and log in as the admin. Publish placement terms first (Admin > Placement terms), because employers can't unlock candidates without them.

- `MM_SECURE_COOKIES=0` lets the login cookie work over plain http on your own computer. Never use it on a server.
- Without email settings, emails (confirmation links and so on) are printed in the terminal. Copy the link from there to confirm test accounts.
- Invoicing stays off until the billing settings are set (see the README's "Invoicing" section).

## Run the tests

```bash
cd python && python -m unittest           # 170 tests; the 2 browser tests need Playwright and Chrome
cd ../js && node --test                   # 20 tests
```

The browser tests are skipped if Playwright isn't installed. To see screenshots of every main screen, run `MM_SHOTS=/some/folder python -m unittest test_web`.

## Daily job

`python -m daily` sends licence expiry reminders and issues invoices that are due. Use the same settings as the server.
