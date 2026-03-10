from src.agents.lead_agent.agent import make_lead_agent
from langchain_core.messages import HumanMessage, AIMessage
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Union

config = {
    "configurable": {
        "model_name": "kimi-k2.5",
        "thread_id": "test-thread-001",
        "thinking_enabled": True,
        "is_plan_mode": True,
        "subagent_enabled": False,
        "max_concurrent_subagents": 3,
        "mode": "pro",
    }
}
lead_agent = make_lead_agent(config)

app = FastAPI()

class Message(BaseModel):
    role: str
    content: str

class MessageRequest(BaseModel):
    messages: List[Message]


@app.post("/chat")
async def chat(request: MessageRequest):
    try:
        messages = []
        for msg in request.messages:
            if msg.role == "user":
                messages.append(HumanMessage(content=msg.content))
            elif msg.role == "assistant":
                messages.append(AIMessage(content=msg.content))
            else:
                messages.append(HumanMessage(content=msg.content))
        result = lead_agent.invoke(
            {"messages": messages},
            context={"thread_id": "test-thread-002"}
        )
        return result["messages"][-1].content
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)