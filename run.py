"""Start the app:  python run.py   (seeds a demo database on first run)."""
import os

from app import create_app
from app.seed import seed, PASSWORD

if __name__ == "__main__":
    db = os.environ.get("DATABASE", "newtonite.db")
    if not os.path.exists(db):
        n = int(os.environ.get("SEED_ITEMS", "400"))
        seed(db, n)
        print(f"Seeded demo data ({n} items). Log in with any *@newtonite.test user, password: {PASSWORD}")
    app = create_app({"DATABASE": db})
    port = int(os.environ.get("PORT", "5000"))
    print(f"Newtonite Ops on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
