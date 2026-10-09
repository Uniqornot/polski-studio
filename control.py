"""Cooperative cancellation/deadlines, including ffmpeg and network waits."""
import asyncio
from contextvars import ContextVar
import subprocess
import threading
import time
import tempfile

CURRENT = ContextVar('job_control', default=None)
MAX_SECONDS = 1800
MAX_AUDIO_SECONDS = 1800
MAX_UNIQUE = 200

class JobStopped(Exception):
    pass

class Control:
    def __init__(self, seconds=MAX_SECONDS):
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + seconds

    def check(self):
        if self.cancelled.is_set():
            raise JobStopped('Задание отменено')
        if time.monotonic() >= self.deadline:
            raise JobStopped('Превышено время задания (30 минут)')


def check():
    control = CURRENT.get()
    if control:
        control.check()

async def guarded(awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            check()
            await asyncio.wait({task},timeout=.2)
        check()
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


def run_process(command,cwd=None):
    # Temporary streams avoid pipe deadlocks and unbounded ffmpeg logs in memory.
    check()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command,cwd=cwd,stdout=out,stderr=err)
        try:
            while process.poll() is None:
                check()
                time.sleep(.1)
            if process.returncode:
                err.seek(0)
                raise subprocess.CalledProcessError(process.returncode,command,stderr=err.read(65536).decode(errors='replace'))
            check()
            out.seek(0)
            return out.read().decode()
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
