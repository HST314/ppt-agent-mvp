"""ASGI entry point for a fast, predictable local startup.

Run with: python -m uvicorn main_front:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os
from pathlib import Path

from config.env_loader import load_dotenv
from ppt_agent.config import load_config
from ppt_agent.gateways import agent_gateways_from_config
from ppt_agent.global_settings import GlobalSettingsStore
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app


def build_app():
    load_dotenv(".env")
    config_path=Path(os.getenv("PPT_AGENT_CONFIG") or "config/ppt-agent.yaml")
    config=load_config(config_path)
    data_root=os.getenv("PPT_AGENT_DATA",".ppt-agent-data")
    service=TaskService(
        WorkspaceStore(data_root),
        clarification_config=config.clarification,
        settings_store=GlobalSettingsStore(config_path),
        feature_flags=config.feature_flags,
        **agent_gateways_from_config(config),
    )
    return create_app(service)


app=build_app()
