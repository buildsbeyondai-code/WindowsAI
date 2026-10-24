"""Run background probes without opening console windows on Windows."""
import os
import subprocess


def run_hidden(*args, **kwargs):
    if os.name == 'nt':
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.run(*args, **kwargs)
