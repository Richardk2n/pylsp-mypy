# -*- coding: utf-8 -*-
"""
File that contains the python-lsp-server plugin pylsp-mypy.

Created on Fri Jul 10 09:53:57 2020

@author: Richard Kellnberger
"""
import re
import tempfile
import os
import os.path
import subprocess
from pathlib import Path
import logging
from pylsp import hookimpl
from pylsp.workspace import Document, Workspace
from pylsp.config.config import Config
from typing import Optional, Dict, Any, IO, List
import atexit
import collections
import warnings

line_pattern: str = r"((?:^[a-z]:)?[^:]+):(?:(\d+):)?(?:(\d+):)? (\w+): (.*)"

log = logging.getLogger(__name__)

# A mapping from workspace path to config file path
mypyConfigFileMap: Dict[str, Optional[str]] = {}

tmpFile: Optional[IO[str]] = None

# In non-live-mode the file contents aren't updated.
# Returning an empty diagnostic clears the diagnostic result,
# so store a cache of last diagnostics for each file a-la the pylint plugin,
# so we can return some potentially-stale diagnostics.
# https://github.com/python-lsp/python-lsp-server/blob/v1.0.1/pylsp/plugins/pylint_lint.py#L55-L62
last_diagnostics: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)


def parse_line(line: str, document: Optional[Document] = None) -> Optional[Dict[str, Any]]:
    """
    Return a language-server diagnostic from a line of the Mypy error report.

    optionally, use the whole document to provide more context on it.


    Parameters
    ----------
    line : str
        Line of mypy output to be analysed.
    document : Optional[Document], optional
        Document in wich the line is found. The default is None.

    Returns
    -------
    Optional[Dict[str, Any]]
        The dict with the lint data.

    """
    result = re.match(line_pattern, line)
    if result:
        file_path, linenoStr, offsetStr, severity, msg = result.groups()

        if file_path != "<string>":  # live mode
            # results from other files can be included, but we cannot return
            # them.
            if document and document.path and not document.path.endswith(file_path):
                log.warning("discarding result for %s against %s", file_path, document.path)
                return None

        lineno = int(linenoStr or 1) - 1  # 0-based line number
        offset = int(offsetStr or 1) - 1  # 0-based offset
        errno = 2
        if severity == "error":
            errno = 1
        diag: Dict[str, Any] = {
            "source": "mypy",
            "range": {
                "start": {"line": lineno, "character": offset},
                # There may be a better solution, but mypy does not provide end
                "end": {"line": lineno, "character": offset + 1},
            },
            "message": msg,
            "severity": errno,
        }
        if document:
            # although mypy does not provide the end of the affected range, we
            # can make a good guess by highlighting the word that Mypy flagged
            word = document.word_at_position(diag["range"]["start"])
            if word:
                diag["range"]["end"]["character"] = diag["range"]["start"]["character"] + len(word)

        return diag
    return None


@hookimpl
def pylsp_lint(
    config: Config, workspace: Workspace, document: Document, is_saved: bool
) -> List[Dict[str, Any]]:
    """
    Lints.

    Parameters
    ----------
    config : Config
        The pylsp config.
    workspace : Workspace
        The pylsp workspace.
    document : Document
        The document to be linted.
    is_saved : bool
        Weather the document is saved.

    Returns
    -------
    List[Dict[str, Any]]
        List of the linting data.

    """
    settings = config.plugin_settings("pylsp_mypy")
    oldSettings1 = config.plugin_settings("mypy-ls")
    if oldSettings1 != {}:
        warnings.warn(
            DeprecationWarning(
                "Your configuration uses the namespace mypy-ls, this should be changed to pylsp_mypy"
            )
        )
    oldSettings2 = config.plugin_settings("mypy_ls")
    if oldSettings2 != {}:
        warnings.warn(
            DeprecationWarning(
                "Your configuration uses the namespace mypy_ls, this should be changed to pylsp_mypy"
            )
        )
    if settings == {}:
        settings = oldSettings1
        if settings == {}:
            settings = oldSettings2

    log.info(
        "lint settings = %s document.path = %s is_saved = %s",
        settings,
        document.path,
        is_saved,
    )

    live_mode = settings.get("live_mode", True)
    dmypy = settings.get("dmypy", False)

    if dmypy and live_mode:
        # dmypy can only be efficiently run on files that have been saved, see:
        # https://github.com/python/mypy/issues/9309
        log.warning("live_mode is not supported with dmypy, disabling")
        live_mode = False

    args = ["--show-column-numbers"]

    global tmpFile
    if live_mode and not is_saved:
        if tmpFile:
            tmpFile = open(tmpFile.name, "w")
        else:
            tmpFile = tempfile.NamedTemporaryFile("w", delete=False)
        log.info("live_mode tmpFile = %s", tmpFile.name)
        tmpFile.write(document.source)
        tmpFile.close()
        args.extend(["--shadow-file", document.path, tmpFile.name])
    elif not is_saved and document.path in last_diagnostics:
        # On-launch the document isn't marked as saved, so fall through and run
        # the diagnostics anyway even if the file contents may be out of date.
        log.info(
            "non-live, returning cached diagnostics len(cached) = %s",
            last_diagnostics[document.path],
        )
        return last_diagnostics[document.path]

    mypyConfigFile = mypyConfigFileMap.get(workspace.root_path)
    if mypyConfigFile:
        args.append("--config-file")
        args.append(mypyConfigFile)

    args.append(document.path)

    if settings.get("strict", False):
        args.append("--strict")

    if not dmypy:
        args.extend(["--incremental", "--follow-imports", "silent"])

        log.info("executing mypy args = %s", args)
        completed_process = subprocess.run(
            ["mypy", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        report = completed_process.stdout.decode()
        errors = completed_process.stderr.decode()
    else:
        # If dmypy daemon is non-responsive calls to run will block.
        # Check daemon status, if non-zero daemon is dead or hung.
        # If daemon is hung, kill will reset
        # If daemon is dead/absent, kill will no-op.
        # In either case, reset to fresh state
        completed_process = subprocess.run(["dmypy", *args], stderr=subprocess.PIPE)
        _err = completed_process.stderr.decode()
        _status = completed_process.returncode
        if _status != 0:
            log.info("restarting dmypy from status: %s message: %s", _status, _err.strip())
            subprocess.run(["dmypy", "kill"])

        # run to use existing daemon or restart if required
        args = ["run", "--"] + args
        log.info("dmypy run args = %s", args)
        completed_process = subprocess.run(
            ["dmypy", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        report = completed_process.stdout.decode()
        errors = completed_process.stderr.decode()

    log.debug("report:\n%s", report)
    log.debug("errors:\n%s", errors)

    diagnostics = []
    for line in report.splitlines():
        log.debug("parsing: line = %r", line)
        diag = parse_line(line, document)
        if diag:
            diagnostics.append(diag)

    log.info("pylsp-mypy len(diagnostics) = %s", len(diagnostics))

    last_diagnostics[document.path] = diagnostics
    return diagnostics


@hookimpl
def pylsp_settings(config: Config) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Read the settings.

    Parameters
    ----------
    config : Config
        The pylsp config.

    Returns
    -------
    Dict[str, Dict[str, Dict[str, str]]]
        The config dict.

    """
    configuration = init(config._root_path)
    return {"plugins": {"pylsp_mypy": configuration}}


def init(workspace: str) -> Dict[str, str]:
    """
    Find plugin and mypy config files and creates the temp file should it be used.

    Parameters
    ----------
    workspace : str
        The path to the current workspace.

    Returns
    -------
    Dict[str, str]
        The plugin config dict.

    """
    log.info("init workspace = %s", workspace)

    configuration = {}
    path = findConfigFile(workspace, ["pylsp-mypy.cfg", "mypy-ls.cfg", "mypy_ls.cfg"])
    if path:
        with open(path) as file:
            configuration = eval(file.read())

    mypyConfigFile = findConfigFile(workspace, ["mypy.ini", ".mypy.ini"])
    mypyConfigFileMap[workspace] = mypyConfigFile
    if mypyConfigFileMap[workspace] is None:
        mypyConfigFileMap[workspace] = defaultMypyConfigFile()

    log.info("mypyConfigFile = %s configuration = %s", mypyConfigFileMap[workspace], configuration)
    return configuration


def findConfigFile(path: str, names: List[str]) -> Optional[str]:
    """
    Search for a config file.

    Search for a file of a given name from the directory specifyed by path through all parent
    directories. The first file found is selected.

    Parameters
    ----------
    path : str
        The path where the search starts.
    names : List[str]
        The file to be found (or alternative names).

    Returns
    -------
    Optional[str]
        The path where the file has been found or None if no matching file has been found.

    """
    start = Path(path).joinpath(names[0])  # the join causes the parents to include path
    for parent in start.parents:
        for name in names:
            file = parent.joinpath(name)
            if file.is_file():
                if file.name in ["mypy-ls.cfg", "mypy_ls.cfg"]:
                    warnings.warn(
                        DeprecationWarning(
                            f"{str(file)}: {file.name} is no longer supported, you should rename your "
                            "config file to pylsp-mypy.cfg"
                        )
                    )
                return str(file)

    return None


def defaultMypyConfigFile() -> Optional[str]:
    """
    Find the mypy default config file path defined in the mypy document below.

    Reference: https://mypy.readthedocs.io/en/stable/config_file.html

    Returns
    -------
    Optional[str]
        The path where the default config file has been found or None if no matching file has been found.

    """
    # A list of user default config file path
    user_default_config = ["~/.config/mypy/config", "~/.mypy.ini"]
    if os.environ.get("XDG_CONFIG_HOME"):
        user_default_config.insert(0, os.path.join(os.environ["XDG_CONFIG_HOME"], "mypy/config"))

    for config_file in user_default_config:
        if Path(config_file).expanduser().exists():
            return config_file

    return None


@atexit.register
def close() -> None:
    """
    Deltes the tempFile should it exist.

    Returns
    -------
    None.

    """
    if tmpFile and tmpFile.name:
        os.unlink(tmpFile.name)
