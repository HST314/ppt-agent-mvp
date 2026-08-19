"""ASGI entry point for a fast, predictable local startup.

Run with: python -m uvicorn main_front:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os

from config.env_loader import load_dotenv
from ppt_agent.config import load_config
from ppt_agent.gateways import agent_gateways_from_config
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app


def build_app():
    load_dotenv(".env")
    config=load_config(os.getenv("PPT_AGENT_CONFIG") or None)
    data_root=os.getenv("PPT_AGENT_DATA",".ppt-agent-data")
    service=TaskService(WorkspaceStore(data_root),clarification_config=config.clarification,**agent_gateways_from_config(config))
    return create_app(service)


app=build_app()
