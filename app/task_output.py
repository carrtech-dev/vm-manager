"""Per-task host output, isolated from inventory reads and bounded on disk."""

import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

_sink = ContextVar('task_output', default=None)
MAX_ENTRIES = 256
CHUNK_SIZE = 1024


def emit(text):
    sink = _sink.get()
    if sink:
        sink(text)


def capturing():
    return _sink.get() is not None


@contextmanager
def capture(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


class Recorder:
    def __init__(self, store, task_id):
        self.store, self.task_id = store, task_id
        self.lock = threading.Lock()

    def __call__(self, text):
        text = str(text).replace('\x00', '')
        if not text:
            return
        try:
            with self.lock, self.store.db() as db:
                seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM task_output WHERE task_id=?', (self.task_id,)).fetchone()[0]
                # Discard the front of oversized chunks without writing them first.
                skipped = max(0, (len(text) + CHUNK_SIZE - 1) // CHUNK_SIZE - MAX_ENTRIES)
                seq += skipped
                text = text[-MAX_ENTRIES * CHUNK_SIZE:]
                for offset in range(0, len(text), CHUNK_SIZE):
                    seq += 1
                    db.execute('INSERT INTO task_output VALUES(?,?,?,?)',
                               (self.task_id, seq, time.time(), text[offset:offset + CHUNK_SIZE]))
                db.execute('DELETE FROM task_output WHERE task_id=? AND seq<=?',
                           (self.task_id, seq - MAX_ENTRIES))
        except Exception:
            # Observability failures must not interrupt hypervisor operations.
            logging.exception('Could not record task output for %s', self.task_id)


def stream_command(args, timeout, stdin):
    """Drain both pipes concurrently, retaining run()'s separate return streams.

    Only called for approved mutation commands, never XML/configuration queries.
    No command arguments or standard input are recorded.
    """
    sink = _sink.get()
    with subprocess.Popen(args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        buffers = [bytearray(), bytearray()]
        def drain(pipe, buffer):
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            while chunk := os.read(pipe.fileno(), 4096):
                buffer.extend(chunk)
                sink(decoder.decode(chunk))
            sink(decoder.decode(b'', final=True))
        readers = [threading.Thread(target=drain, args=(pipe, buffer), daemon=True)
                   for pipe, buffer in zip((process.stdout, process.stderr), buffers)]
        for thread in readers:
            thread.start()
        def write_input():
            try:
                process.stdin.write(stdin.encode())
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        writer = threading.Thread(target=write_input, daemon=True) if stdin is not None else None
        if writer:
            writer.start()
        try:
            process.wait(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            for thread in readers:
                thread.join()
            if writer:
                writer.join()
        return subprocess.CompletedProcess(args, process.returncode,
            buffers[0].decode(errors='replace'), buffers[1].decode(errors='replace'))


def command_label(args):
    """Allow only mutation output; read-only results may contain guest secrets."""
    program = os.path.basename(args[0])
    if program == 'virsh' and len(args) > 3 and args[3] in {
        'start', 'shutdown', 'destroy', 'reboot', 'define', 'undefine',
        'snapshot-create', 'snapshot-revert', 'snapshot-delete', 'suspend', 'resume',
    }:
        return 'virsh ' + args[3]
    if program == 'qemu-img' and len(args) > 1 and args[1] in {'convert', 'create', 'resize'}:
        return 'qemu-img ' + args[1]
    if (program == 'incus' and args[1:5] == ('--force-local', '--quiet', '--project', 'default')
            and len(args) > 5 and args[5] in {'export', 'import', 'storage'}):
        return 'Incus archive/storage operation'
    return None
