"""Controlled PayPal Sandbox validation runner (CLI). Dry-run by default; see backend/app/sandbox_runner.py and docs/PAYPAL_SANDBOX_VALIDATION_PLAN.md.

    backend/.venv/Scripts/python scripts/sandbox_validation_runner.py                       # offline rehearsal (default): no network
    backend/.venv/Scripts/python scripts/sandbox_validation_runner.py --stage create        # dry run of the create stage: prints the planned requests
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.sandbox_runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
