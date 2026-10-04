"""Constrained Git calls. Never inherit executable Git settings from the caller."""
import os
import subprocess

from .core import BridgeError

FIXED_CONFIG = ("core.fsmonitor=false", "core.autocrlf=false", "core.attributesFile=" + os.devnull,
                "core.hooksPath=" + os.devnull, "core.pager=cat", "apply.whitespace=nowarn")


def _environment():
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _argv(path, args):
    argv = ["git"]
    for value in FIXED_CONFIG:
        argv += ["-c",value]
    return argv + ["-C",str(path),*args]


def run(path, *args, input=None, text=False, check_config=True):
    env = _environment()
    if check_config:
        config = subprocess.run(_argv(path,["config","--list","--name-only","--includes"]),
                                capture_output=True,text=True,timeout=10,env=env)
        if config.returncode:
            raise BridgeError("Cannot inspect repository Git configuration")
        for key in config.stdout.splitlines():
            lower = key.casefold()
            if (lower.startswith("filter.") or lower == "diff.external" or
                    lower.startswith("diff.") and lower.endswith(".command") or
                    lower.startswith("merge.") and lower.endswith(".driver")):
                raise BridgeError("Executable Git filter or driver configuration is unsupported")
    return subprocess.run(_argv(path,list(args)),input=input,capture_output=True,text=text,timeout=10,env=env)
