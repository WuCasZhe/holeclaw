import _thread
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import run_digest as runtime
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_sink import RunSink, SinkServer, SITE_ORIGIN


class CancellationTests(unittest.TestCase):
    def test_idle_progress_wait_is_interruptible(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / 'cache.sqlite3')
            checkpoint = runtime.new_checkpoint({}, 100, 200, 100, 'test')
            sink = RunSink(cache, checkpoint, Path(directory) / 'cp.json', None, None)
            timer = threading.Timer(0.05, _thread.interrupt_main)
            started = time.monotonic()
            try:
                timer.start()
                with self.assertRaises(KeyboardInterrupt):
                    sink.wait_for_progress(0, threading.Event())
                self.assertLess(time.monotonic() - started, 1)
            finally:
                timer.join()
                cache.close()

    @unittest.skipUnless(os.name == 'posix', 'POSIX process-group integration test')
    def test_stop_terminates_real_process_tree(self):
        child_code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'
        parent_code = ('import subprocess,sys,signal,time; '
                       'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                       f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}]); '
                       'print(p.pid,flush=True); time.sleep(30)')
        process = subprocess.Popen([sys.executable, '-c', parent_code], stdout=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            child_pid = int(process.stdout.readline())
            started = time.monotonic()
            runtime.stop_collector_process(process, timeout=0.1)
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNotNone(process.poll())
            # A reparented zombie is stopped; there must be no live descendant.
            status = Path(f'/proc/{child_pid}/stat')
            for _ in range(20):
                if not status.exists() or status.read_text().split()[2] == 'Z':
                    break
                time.sleep(0.01)
            else:
                self.fail('collector descendant remained alive')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            process.stdout.close()

    def test_windows_fallback_targets_only_collector_tree(self):
        process = MagicMock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = 0
        with patch.object(runtime.os, 'name', 'nt'), \
             patch.object(runtime.signal, 'CTRL_BREAK_EVENT', 1, create=True), \
             patch.object(runtime.subprocess, 'run') as taskkill:
            runtime.stop_collector_process(process)
        taskkill.assert_called_once_with(['taskkill', '/PID', '12345', '/T', '/F'],
                                         capture_output=True, timeout=3)

    def test_control_connection_delivers_cancellation_event(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / 'cache.sqlite3')
            sink = RunSink(cache, runtime.new_checkpoint({}, 100, 200, 100, 'test'),
                           Path(directory) / 'cp.json', None, None)
            server = SinkServer(sink)
            def read_control():
                request = urllib.request.Request(server.url.replace('/ingest?', '/control?'),
                                                 headers={'Origin': SITE_ORIGIN})
                with urllib.request.urlopen(request, timeout=2) as result:
                    return json.load(result)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(read_control)
                    sink.cancel()
                    self.assertEqual(future.result(timeout=2), {'cancelled': True})
            finally:
                server.close()
                cache.close()

    def test_expected_windows_disconnect_is_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / 'cache.sqlite3')
            sink = RunSink(cache, runtime.new_checkpoint({}, 100, 200, 100, 'test'),
                           Path(directory) / 'cp.json', None, None)
            server = SinkServer(sink)
            try:
                handler_type = server.server.RequestHandlerClass
                handler = handler_type.__new__(handler_type)
                with patch.object(BaseHTTPRequestHandler, 'handle', side_effect=ConnectionAbortedError(10053, 'closed')):
                    handler.handle()
                with patch.object(BaseHTTPRequestHandler, 'handle', side_effect=RuntimeError('unexpected bug')):
                    with self.assertRaises(RuntimeError):
                        handler.handle()
            finally:
                server.close()
                cache.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows process-tree integration test')
    def test_stop_terminates_real_windows_process_tree(self):
        import ctypes
        from ctypes import wintypes
        child_code = 'import time; time.sleep(30)'
        parent_code = ('import subprocess,sys,time; '
                       f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}]); '
                       'print(p.pid,flush=True); time.sleep(30)')
        process = subprocess.Popen([sys.executable, '-c', parent_code], stdout=subprocess.PIPE,
                                   text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            child_pid = int(process.stdout.readline())
            started = time.monotonic()
            runtime.stop_collector_process(process)
            self.assertLess(time.monotonic() - started, 4)
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, child_pid)
            if handle:
                try:
                    code = wintypes.DWORD()
                    self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    self.assertNotEqual(code.value, 259, 'descendant is still active')
                finally:
                    kernel.CloseHandle(handle)
        finally:
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=3)
            process.wait(timeout=2)
            process.stdout.close()


if __name__ == '__main__':
    unittest.main()
