from typing import List, Optional
from pydantic import BaseModel, ConfigDict

class Message(BaseModel):
    role: str
    content: Optional[str] = ""
    model_config = ConfigDict(extra="allow")

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: Optional[bool] = False
    file_ids: Optional[List[str]] = []
    model_config = ConfigDict(extra="allow")

class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: Message
    finish_reason: Optional[str] = "stop"

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]