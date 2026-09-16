from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db

# Annotated dependency: keeps `Depends(...)` out of argument defaults.
DbSession = Annotated[Session, Depends(get_db)]
