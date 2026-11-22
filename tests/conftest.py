import os, sys, tempfile, random
_tmp = tempfile.mkdtemp()
os.environ["ONBOARDING_DATA_DIR"] = _tmp
os.environ["DISABLE_WORKER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from app import db, clock, samples, workflow


@pytest.fixture(autouse=True)
def fresh():
    clock.reset()
    db.init(":memory:")
    yield


@pytest.fixture
def rng():
    return random.Random(1234)


def run(seconds=0, step=1.0):
    """Advance the simulated clock in small steps, ticking the scheduler each time."""
    t = 0.0
    workflow.tick()
    while t < seconds:
        clock.advance(step)
        t += step
        workflow.tick()


def state(app_id):
    return workflow.get_app(app_id)["state"]


def events(app_id, name=None):
    ev = [a["event"] for a in workflow.application_view(app_id)["audit"]]
    return ev if name is None else [e for e in ev if e == name]
