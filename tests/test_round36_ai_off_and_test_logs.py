"""
Round 36.

* IBO_ENABLE_AI=0 (the default) means no external AI call from anywhere --
  including the dashboard's on-demand analysis, which used to call Gemini /
  Grok whenever keys were present. No API cost, no SAP data sent out.
* Tests log into a temporary folder (IBO_LOG_DIR), never the real logs/.
"""
import os

from evaluation import ai_analyzer
from utils import logger as L
from utils.paths import BASE_DIR


def test_ai_off_by_default_uses_the_local_provider(monkeypatch):
    monkeypatch.delenv("IBO_ENABLE_AI", raising=False)
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("USE_MOCK_AI", "false")
    assert ai_analyzer.get_provider().name == "mock"


def test_ai_off_explicitly(monkeypatch):
    monkeypatch.setenv("IBO_ENABLE_AI", "0")
    monkeypatch.setenv("AI_PROVIDER_CHAIN", "gemini:GEMINI_API_KEY,grok:GROK_API_KEY")
    monkeypatch.setenv("USE_MOCK_AI", "false")
    assert ai_analyzer.get_provider().name == "mock"


def test_ai_on_still_selects_the_configured_provider(monkeypatch):
    monkeypatch.setenv("IBO_ENABLE_AI", "1")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    assert ai_analyzer.get_provider().name != "mock"


def test_tests_do_not_write_into_the_real_logs_folder():
    real = os.path.normcase(os.path.abspath(os.path.join(BASE_DIR, "logs")))
    assert os.path.normcase(os.path.abspath(L.LOG_DIR)) != real


def test_rca_state_file_follows_the_log_folder():
    from core import rca_trigger
    assert os.path.normcase(str(rca_trigger.RCA_STATE_PATH.parent)) == \
        os.path.normcase(os.path.abspath(L.LOG_DIR)) or \
        os.path.samefile(rca_trigger.RCA_STATE_PATH.parent, L.LOG_DIR)
