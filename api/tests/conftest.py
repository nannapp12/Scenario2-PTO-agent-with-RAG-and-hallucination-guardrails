import base64
import os
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(API_DIR), str(API_DIR.parent)]  # `app` and `common`

TEST_KEY = base64.b64encode(b"k" * 32).decode()
os.environ.update({
    "PG_HOST": "localhost",
    "PG_USER": "test",
    "PII_ENCRYPTION_KEY": TEST_KEY,
    "PTO_AUTH_MODE": "none",
})
