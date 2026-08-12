#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ppt_agent.api import serve
from ppt_agent.gateways import gateways_from_env
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

p=argparse.ArgumentParser(); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8000); p.add_argument("--data",default=".ppt-agent-data")
a=p.parse_args(); serve(a.data,a.host,a.port,service=TaskService(WorkspaceStore(a.data),**gateways_from_env()))
