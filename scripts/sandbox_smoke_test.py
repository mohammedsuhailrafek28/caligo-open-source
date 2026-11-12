"""Entry point for the Stage 2D PayPal SANDBOX smoke test. Dry run by default; see docs/STAGE_2D_SMOKE_TEST.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.sandbox_smoke import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
