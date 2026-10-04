"""Run the whole app locally: the API plus the static site.

    set OPENAI_API_KEY=...        (or OPENAI_API_KEY_FILE=path\\to\\key.txt)
    python dev_server.py          -> http://127.0.0.1:4620
"""
import os
import sys

import uvicorn
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if "--key-file" in sys.argv:  # lets a launcher point at a key file without putting the key in argv
    os.environ.setdefault("OPENAI_API_KEY_FILE", sys.argv[sys.argv.index("--key-file") + 1])

from api.index import app  # noqa: E402

app.mount("/", StaticFiles(directory=os.path.join(HERE, "public"), html=True), name="site")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "4620")))
