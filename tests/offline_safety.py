"""Safety barriers shared by mock-only tests."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest.mock import patch


class OfflineTestCase:
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        self.enterContext(patch.dict(os.environ, {'CALENDAR_REMINDERS_HOME':str(home),
                                                  'HERMES_HOME':str(home)}))
        self.enterContext(patch.object(socket, 'socket', side_effect=AssertionError('Tests prohibit network sockets')))
        self.enterContext(patch.object(subprocess, 'Popen', side_effect=AssertionError('Tests prohibit external subprocesses')))
