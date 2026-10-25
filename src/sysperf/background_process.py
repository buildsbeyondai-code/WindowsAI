"""Run background probes without opening console windows on Windows."""
import os
import subprocess

# Read through this name (not os.name) so tests can simulate the other OS
# without patching the global os module, which breaks pathlib on Windows.
OS_NAME = os.name


def run_hidden(*args, **kwargs):
    if OS_NAME == 'nt':
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.run(*args, **kwargs)
