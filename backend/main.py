from src.agents.lead_agent.agent import make_lead_agent
from langchain_core.messages import HumanMessage

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

if __name__ == "__main__":
    result = lead_agent.invoke(
        {"messages": [HumanMessage(content="我们刚刚聊了什么？")]},
        # config=config,
        context={"thread_id": "test-thread-001"}
    )
    print(result["messages"][-1].content)