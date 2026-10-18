# Open in VS Code (3 steps)

1. Unzip, then **File > Open Folder...** and choose the `newtonite-ops` folder. Accept the "install recommended extensions" prompt (Python).
2. Open the terminal (**Terminal > New Terminal**) and run:
   - macOS / Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
   - Windows (PowerShell): `python -m venv .venv; .venv\Scripts\Activate.ps1; pip install -r requirements.txt`
   (Or press Ctrl+Shift+P, run **Tasks: Run Task**, choose **Setup: create venv and install**.)
   Then pick the `.venv` interpreter if VS Code asks (Ctrl+Shift+P > **Python: Select Interpreter**).
3. Press **F5** and choose **Run Newtonite Ops**, then open http://localhost:5000.
   Sign in as `sam@newtonite.test` / `password123` (other demo users are on the login screen).

Run tests: F5 > **Run all tests**, or the beaker icon (Testing panel). Needs Python 3.10+.

Tip: open two different browsers (or one normal + one private window) signed in as different users to watch
claim races, edit conflicts and live updates.
