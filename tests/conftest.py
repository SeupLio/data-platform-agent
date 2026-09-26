import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session")
def index():
    from dataagent.knowledge.index import load_index

    return load_index()


@pytest.fixture(scope="session")
def wh(index):
    from dataagent.warehouse.engine import Warehouse

    return Warehouse(index=index)


@pytest.fixture(scope="session")
def today(wh):
    return wh.today


@pytest.fixture()
def registry(index, wh):
    from dataagent.agents.tools import ToolRegistry

    return ToolRegistry(index, wh)
