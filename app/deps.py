from datetime import datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.clock import now
from app.db import get_db

# Annotated dependency: keeps `Depends(...)` out of argument defaults.
DbSession = Annotated[Session, Depends(get_db)]

# The instant the request is judged against. A dependency rather than a call inside
# `rsvp.phase()`, so a test can sit exactly on an RSVP deadline. TAP-7729.
Now = Annotated[datetime, Depends(now)]
