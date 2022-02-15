"""Serverless process manager."""

import json
import logging
import os
import signal
import sys
from typing import Generator, List, Optional, Tuple, Union

from celery import Signature, signature  # pylint: disable=no-name-in-module
from funcy.flow import reraise
from shortuuid import uuid

from ..utils import remove
from .exceptions import ProcessNotTerminatedError, UnsupportedSignalError
from .process import ProcessInfo

logger = logging.getLogger(__name__)


class ProcessManager:
    """Manager for controlling background ManagedProcess(es) via celery.

    Spawned process entries are kept in the manager directory until they
    are explicitly removed (with remove() or cleanup()) so that return
    value and log information can be accessed after a process has completed.
    """

    def __init__(
        self,
        wdir: Optional[str] = None,
    ):
        """Construct a ProcessManager

        Arguments:
            wdir: Directory used for storing process information. Defaults
                to the current working directory.
        """
        self.wdir = wdir or os.curdir

    def __iter__(self) -> Generator[str, None, None]:
        if not os.path.exists(self.wdir):
            return
        for name in os.listdir(self.wdir):
            yield name

    def __getitem__(self, key: str) -> "ProcessInfo":
        info_path = os.path.join(self.wdir, key, f"{key}.json")
        try:
            with open(info_path, encoding="utf-8") as fobj:
                return ProcessInfo.from_dict(json.load(fobj))
        except FileNotFoundError as exc:
            raise KeyError from exc

    @reraise(FileNotFoundError, KeyError)
    def __setitem__(self, key: str, value: "ProcessInfo"):
        info_path = os.path.join(self.wdir, key, f"{key}.json")
        with open(info_path, "w", encoding="utf-8") as fobj:
            return json.dump(value.asdict(), fobj)

    def __delitem__(self, key: str) -> None:
        path = os.path.join(self.wdir, key)
        if os.path.exists(path):
            remove(path)

    def get(self, key: str, default=None):
        """Return the specified process."""
        try:
            return self[key]
        except KeyError:
            return default

    def processes(self) -> Generator[Tuple[str, "ProcessInfo"], None, None]:
        """Iterate over managed processes."""
        for name in self:
            try:
                yield name, self[name]
            except KeyError:
                continue

    def run(
        self,
        args: Union[str, List[str]],
        name: Optional[str] = None,
        task: Optional[str] = None,
    ) -> Signature:
        """Return a task which would run the given command in the background.

        Arguments:
            args: Command to run.
            name: Optional name to use for the spawned process.
            task: Optional name of Celery task to use for spawning the process.
                Defaults to 'dvc_task.proc.tasks.run'.
        """
        name = name or uuid()
        task = task or "dvc_task.proc.tasks.run"
        return signature(
            task,
            args=(args,),
            kwargs={"name": name, "wdir": os.path.join(self.wdir, name)},
        )

    def send_signal(self, name: str, sig: int):
        """Send `signal` to the specified named process."""
        process_info = self[name]
        if sys.platform == "win32":
            if sig not in (
                signal.SIGTERM,
                signal.CTRL_C_EVENT,
                signal.CTRL_BREAK_EVENT,
            ):
                raise UnsupportedSignalError(sig)

        def handle_closed_process():
            logging.warning(
                "Process '%s' had already aborted unexpectedly.", name
            )
            process_info.returncode = -1
            self[name] = process_info

        if process_info.returncode is None:
            try:
                os.kill(process_info.pid, sig)
            except ProcessLookupError:
                handle_closed_process()
                raise
            except OSError as exc:
                if sys.platform == "win32":
                    if exc.winerror == 87:
                        handle_closed_process()
                        raise ProcessLookupError from exc
                raise

    def terminate(self, name: str):
        """Terminate the specified named process."""
        self.send_signal(name, signal.SIGTERM)

    def kill(self, name: str):
        """Kill the specified named process."""
        if sys.platform == "win32":
            self.send_signal(name, signal.SIGTERM)
        else:
            self.send_signal(name, signal.SIGKILL)  # pylint: disable=no-member

    def remove(self, name: str, force: bool = False):
        """Remove the specified named process from this manager.

        If the specified process is still running, it will be forcefully killed
        if `force` is True`, otherwise an exception will be raised.

        Raises:
            ProcessNotTerminatedError if the specified process is still
            running and was not forcefully killed.
        """
        try:
            process_info = self[name]
        except KeyError:
            return
        if process_info.returncode is None and not force:
            raise ProcessNotTerminatedError(name)
        try:
            self.kill(name)
        except ProcessLookupError:
            pass
        del self[name]

    def cleanup(self, force: bool = False):
        """Remove stale (terminated) processes from this manager."""
        for name in self:
            try:
                self.remove(name, force)
            except ProcessNotTerminatedError:
                continue
