import base64
import mimetypes
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from llm_router.registry import BY_ID


def conversation_messages(history, model_id, image_path):
    messages = []
    last_index = len(history) - 1
    accepts_image = "image" in BY_ID[model_id].modalities
    for index, turn in enumerate(history):
        if turn["role"] == "tool":
            messages.append(ToolMessage(content=turn["text"], tool_call_id=turn["tool_call_id"]))
            continue
        if turn["role"] == "system":
            messages.append(SystemMessage(content=turn["text"]))
            continue
        if turn["role"] == "assistant":
            messages.append(AIMessage(content=turn["text"], additional_kwargs={"tool_calls": turn["tool_calls"]} if turn.get("tool_calls") else {}))
            continue
        if index == last_index and image_path and accepts_image:
            messages.append(_image_message(turn["text"], image_path))
            continue
        messages.append(HumanMessage(content=turn.get("blocks") or turn["text"]))
    return messages


def _image_message(user_query, image_path):
    path = Path(image_path)
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None:
        mime_type = "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return HumanMessage(
        content=[
            {"type": "text", "text": user_query},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
        ]
    )
