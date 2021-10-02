import pytest

from pylsp.workspace import Workspace, Document
from pylsp.config.config import Config
from pylsp import uris
from unittest.mock import Mock, patch
from pylsp_mypy import plugin

DOC_URI = __file__
DOC_TYPE_ERR = """{}.append(3)
"""
TYPE_ERR_MSG = '"Dict[<nothing>, <nothing>]" has no attribute "append"'

TEST_LINE = 'test_plugin.py:279:8: error: "Request" has no attribute "id"'
TEST_LINE_WITHOUT_COL = "test_plugin.py:279: " 'error: "Request" has no attribute "id"'
TEST_LINE_WITHOUT_LINE = "test_plugin.py: " 'error: "Request" has no attribute "id"'


@pytest.fixture
def workspace(tmpdir):
    """Return a workspace."""
    ws = Workspace(uris.from_fs_path(str(tmpdir)), Mock())
    ws._root_path = "C:"
    ws._config = Config(ws.root_uri, {}, 0, {})
    return ws


class FakeConfig(object):
    def __init__(self):
        self._root_path = "C:"

    def plugin_settings(self, plugin, document_path=None):
        return {}


def test_settings():
    config = FakeConfig()
    settings = plugin.pylsp_settings(config)
    assert settings == {"plugins": {"pylsp_mypy": {}}}


def test_plugin(workspace):
    config = FakeConfig()
    doc = Document(DOC_URI, workspace, DOC_TYPE_ERR)
    plugin.pylsp_settings(config)
    diags = plugin.pylsp_lint(config, workspace, doc, is_saved=False)

    assert len(diags) == 1
    diag = diags[0]
    assert diag["message"] == TYPE_ERR_MSG
    assert diag["range"]["start"] == {"line": 0, "character": 0}
    assert diag["range"]["end"] == {"line": 0, "character": 1}


def test_parse_full_line(workspace):
    doc = Document(DOC_URI, workspace)
    diag = plugin.parse_line(TEST_LINE, doc)
    assert diag["message"] == '"Request" has no attribute "id"'
    assert diag["range"]["start"] == {"line": 278, "character": 7}
    assert diag["range"]["end"] == {"line": 278, "character": 8}


def test_parse_line_without_col(workspace):
    doc = Document(DOC_URI, workspace)
    diag = plugin.parse_line(TEST_LINE_WITHOUT_COL, doc)
    assert diag["message"] == '"Request" has no attribute "id"'
    assert diag["range"]["start"] == {"line": 278, "character": 0}
    assert diag["range"]["end"] == {"line": 278, "character": 1}


def test_parse_line_without_line(workspace):
    doc = Document(DOC_URI, workspace)
    diag = plugin.parse_line(TEST_LINE_WITHOUT_LINE, doc)
    assert diag["message"] == '"Request" has no attribute "id"'
    assert diag["range"]["start"] == {"line": 0, "character": 0}
    assert diag["range"]["end"] == {"line": 0, "character": 6}


@pytest.mark.parametrize("word,bounds", [("", (7, 8)), ("my_var", (7, 13))])
def test_parse_line_with_context(monkeypatch, word, bounds, workspace):
    doc = Document(DOC_URI, workspace)
    monkeypatch.setattr(Document, "word_at_position", lambda *args: word)
    diag = plugin.parse_line(TEST_LINE, doc)
    assert diag["message"] == '"Request" has no attribute "id"'
    assert diag["range"]["start"] == {"line": 278, "character": bounds[0]}
    assert diag["range"]["end"] == {"line": 278, "character": bounds[1]}


def test_multiple_workspaces(tmpdir):
    DOC_SOURCE = """
def foo():
    return
    unreachable = 1
"""
    DOC_ERR_MSG = "Statement is unreachable"

    # Initialize two workspace folders.
    folder1 = tmpdir.mkdir("folder1")
    ws1 = Workspace(uris.from_fs_path(str(folder1)), Mock())
    ws1._config = Config(ws1.root_uri, {}, 0, {})
    folder2 = tmpdir.mkdir("folder2")
    ws2 = Workspace(uris.from_fs_path(str(folder2)), Mock())
    ws2._config = Config(ws2.root_uri, {}, 0, {})

    # Create configuration file for workspace folder 1.
    mypy_config = folder1.join("mypy.ini")
    mypy_config.write("[mypy]\nwarn_unreachable = True")

    # Initialize settings for both folders.
    plugin.pylsp_settings(ws1._config)
    plugin.pylsp_settings(ws2._config)

    # Test document in workspace 1 (uses mypy.ini configuration).
    doc1 = Document(DOC_URI, ws1, DOC_SOURCE)
    diags = plugin.pylsp_lint(ws1._config, ws1, doc1, is_saved=False)
    assert len(diags) == 1
    diag = diags[0]
    assert diag["message"] == DOC_ERR_MSG

    # Test document in workspace 2 (without mypy.ini configuration)
    doc2 = Document(DOC_URI, ws2, DOC_SOURCE)
    diags = plugin.pylsp_lint(ws2._config, ws2, doc2, is_saved=False)
    assert len(diags) == 0


def test_default_config_file(tmpdir):
    config = FakeConfig()
    with patch("pylsp_mypy.plugin.findConfigFile", return_value=None), patch(
        "pathlib.Path.exists", return_value=True
    ):
        plugin.pylsp_settings(config)
        assert plugin.mypyConfigFileMap.get(config._root_path) == "~/.config/mypy/config"
