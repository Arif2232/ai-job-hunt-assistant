from pydantic import Field
from src.models import StrictModel


class Board(StrictModel):
    name: str
    board: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    region: str = Field(default='global', pattern=r'^(global|eu)$')


class Feed(StrictModel):
    name: str
    url: str
