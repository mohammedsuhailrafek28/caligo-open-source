"""Entry point for the read-only PayPal SANDBOX invoice verifier. Dry run by default; see docs/STAGE_2D_SMOKE_TEST.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.sandbox_readonly import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
