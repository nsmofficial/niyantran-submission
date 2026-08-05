"""`python -m gateway` — run the gateway on http://127.0.0.1:8000."""

import uvicorn

from . import app as app_module
from . import config

if __name__ == "__main__":
    settings = config.from_env()
    print(f"database : {settings.db_path}")
    print(f"gates    : {settings.rate_limit_per_minute}/min, monthly budget per token")
    uvicorn.run(app_module.create_app(settings), host="127.0.0.1", port=8000, log_level="info")
