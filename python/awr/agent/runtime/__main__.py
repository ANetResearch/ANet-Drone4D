"""`python -m awr.agent.runtime` → `app.main()`（M14 §9.1）。"""

import sys

from .app import main

sys.exit(main())
