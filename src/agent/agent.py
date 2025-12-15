from google.adk.agents import Agent
from google.genai import types

from .prompts import INSTRUCTION
from .tools import before_tool, send_escalation_email

root_agent = Agent(
    model="gemini-2.5-flash",
    name="dgen_agent",
    instruction=INSTRUCTION,
    tools=[send_escalation_email],
    before_tool_callback=before_tool,
    generate_content_config=types.GenerateContentConfig(max_output_tokens=1500),
)
