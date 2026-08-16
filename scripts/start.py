#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn

from ppt_agent.config import load_config
from ppt_agent.gateways import agent_gateways_from_config
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app
from config.env_loader import load_dotenv  # 引入 .env 加载器

load_dotenv(".env")  # 在程序启动时自动读取当前目录下的 .env 文件
p=argparse.ArgumentParser(); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8000); p.add_argument("--data",default=".ppt-agent-data")
a=p.parse_args()
config=load_config()
service=TaskService(WorkspaceStore(a.data),clarification_config=config.clarification,**agent_gateways_from_config(config))
uvicorn.run(create_app(service),host=a.host,port=a.port)
