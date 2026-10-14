"""Validated multimodal and caller-executed function-tool input for the SaaS API."""
import base64
import io
import json
import re
from PIL import Image
from jsonschema import validators
from .api import APIRequestError, normalize_chat


def normalize_extended(body):
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 32 or len(json.dumps(tools)) > 20000:
        raise APIRequestError("At most 32 function tools and 20,000 schema characters are allowed.")
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
            raise APIRequestError("Malformed function tool.")
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        if tool.get("type") != "function" or not isinstance(function.get("name"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", function["name"]):
            raise APIRequestError("Only named function tools are supported.")
        schema = function.get("parameters", {"type": "object"})
        try:
            validators.validator_for(schema).check_schema(schema)
        except Exception:
            raise APIRequestError("Invalid function parameter schema.") from None
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise APIRequestError("messages must be a list.")
    simplified, rich = [], []
    pending = set()
    image_count = 0
    for source in raw_messages:
        if not isinstance(source, dict) or set(source)-{"role", "content", "tool_calls", "tool_call_id"}:
            raise APIRequestError("Unsupported message fields.")
        role, content = source.get("role"), source.get("content")
        extra = {}
        if isinstance(content, list):
            if role != "user":
                raise APIRequestError("Image blocks are supported on user messages only.")
            pieces=[]
            for block in content:
                if not isinstance(block,dict):
                    raise APIRequestError("Invalid content block.")
                if block.get("type") == "text" and isinstance(block.get("text"),str):
                    pieces.append(block["text"])
                elif block.get("type") == "image_url":
                    if not isinstance(block.get("image_url"), dict):
                        raise APIRequestError("Invalid image URL block.")
                    url = block.get("image_url",{}).get("url", "")
                    if not isinstance(url,str) or not re.match(r"^data:image/(png|jpeg|webp|gif);base64,",url) or len(url)>11_200_000:
                        raise APIRequestError("Images must be bounded base64 data URLs; remote fetches are unsupported.")
                    try:
                        data=base64.b64decode(url.split(",",1)[1],validate=True)
                        with Image.open(io.BytesIO(data)) as image:
                            if image.width*image.height>16_000_000:
                                raise ValueError
                            image.verify()
                    except Exception:
                        raise APIRequestError("Invalid image.") from None
                    image_count+=1
                    if image_count>4:
                        raise APIRequestError("At most four images are allowed.")
                else:
                    raise APIRequestError("Unsupported content block.")
            extra["blocks"]=content
            content="\n".join(pieces) or "Describe the image."
        calls=source.get("tool_calls")
        if calls:
            if role!="assistant" or not isinstance(calls,list) or len(calls)>32 or len(json.dumps(calls))>100000:
                raise APIRequestError("Invalid assistant tool calls.")
            for call in calls:
                try:
                    if call["type"]!="function" or not isinstance(call["id"],str) or not call["id"] or not isinstance(call["function"]["name"],str):
                        raise ValueError
                    if not isinstance(json.loads(call["function"]["arguments"]),dict):
                        raise ValueError
                    pending.add(call["id"])
                except (KeyError,TypeError,ValueError):
                    raise APIRequestError("Malformed tool call.") from None
            extra["tool_calls"]=calls
            content=content or ""
        if role=="tool":
            identifier=source.get("tool_call_id")
            if not isinstance(identifier,str) or identifier not in pending or not isinstance(content,str):
                raise APIRequestError("Tool results must match a preceding assistant call.")
            pending.remove(identifier);extra["tool_call_id"]=identifier
        simplified.append({"role":"user" if role=="tool" else role,"content":content or "[tool call]"})
        rich.append({"role":role,"text":content or "",**extra})
    if pending:
        raise APIRequestError("Supply a result for every pending tool call.")
    base={k:v for k,v in body.items() if k!="tools"}
    base["messages"]=simplified
    _, options=normalize_chat(base)
    if tools and (options["require_verification"] or options["response_schema"]):
        raise APIRequestError("Tool calls cannot be combined with answer verification or JSON response validation.")
    if tools:
        # Account for tool schema tokens before budget/context eligibility checks.
        rich.insert(0,{"role":"system","text":"Available function schemas: "+json.dumps(tools)})
    return rich, options, {"tools":tools, "requires_tools":bool(tools or any(m.get("tool_calls") or m["role"]=="tool" for m in rich))}
