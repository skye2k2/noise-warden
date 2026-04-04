from pydantic import BaseModel
class ControlRequest(BaseModel):
    reason: str | None = None
