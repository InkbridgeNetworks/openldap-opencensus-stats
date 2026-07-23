# openldap_opencensus_stats
# Copyright (C) 2026  InkBridge Networks
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Read slapd operation timing from a named pipe and publish latency histograms.

slapd writes one RESULT line to its stats log for every completed LDAP
operation.  Each line carries two timings (enabled by default via
SLAP_STATS_ETIME):

    conn=1001 op=2 SEARCH RESULT tag=101 err=0 qtime=0.000012 etime=0.001234 nentries=5 text=

1. qtime is how long the operation sat in slapd's thread pool queue before a
   worker thread picked it up.
2. etime is the wall clock time from receipt of the operation until its
   result was sent.  Both timers start at receipt, so etime includes qtime;
   execution time alone is etime minus qtime.

The monitoring database (cn=Monitor) only exposes operation counters, so these
log lines are the only latency data slapd produces.  To keep the data off
disk, slapd's logfile is pointed at a named pipe (FIFO).  This module reads
the pipe and records each timing into Prometheus histograms, served on the
same metrics page as the existing cn=Monitor statistics.  The recording
deliberately bypasses OpenCensus, so a busy server cannot outrun the reader;
the register_metrics header explains the cost difference and the consequence
(Prometheus only, the Stackdriver exporter never sees these two metrics).
The metric names spell the abbreviations out: qtime is published as
queue_latency_seconds and etime as operation_latency_seconds.

The pipe handling:

1. The pipe is opened O_RDWR|O_NONBLOCK.  Holding our own write end means
   reads never return end of file when slapd restarts, so one descriptor
   survives any number of writer turnovers.  Read-write FIFO opens are
   Linux-defined behaviour, see fifo(7).  slapd's own open of the write end
   blocks until a reader exists, so this service must be running first, and
   must never block waiting for slapd.
2. The event loop polls the descriptor (loop.add_reader) and drains the pipe
   whenever data arrives.  Data moving through a pipe is not a filesystem
   event, so an inotify/watchfiles monitor cannot see writes.  (The
   openldap-audit2json PipeReader can watch the path because the auditlog
   overlay opens and closes its log file around every entry; slapd opens its
   stats logfile once and holds it.)
3. A watchfiles monitor on the pipe's directory handles lifecycle only: the
   pipe appearing triggers an open, deletion triggers a close.

slapd ignores SIGPIPE and discards logging write errors, so stopping this
service only ever costs metrics, never the directory server.

To enable, add a statsLogPipe key naming the FIFO to an ldapServers entry, and
point slapd's logfile at the same FIFO.  The README section "Operation
latency over a stats pipe" walks through the slapd side.

Optionally, statsLogPipe can list the directory suffixes to label latency by.
The RESULT line does not name a database, but the request line each operation
logs first does carry the target distinguished name (DN), under the same
conn=/op= prefix:

    conn=1001 op=2 SRCH base="dc=example,dc=org" scope=2 deref=0 filter="(uid=bob)"
    conn=1001 op=2 SEARCH RESULT tag=101 err=0 qtime=... etime=...

The reader remembers the DN per (conn, op), and when the matching RESULT
arrives it labels the timing with the longest configured suffix the DN falls
under.  Results whose DN was never seen or matches no configured suffix are
labelled "unknown" (extended operations log no DN, and operations already in
flight when the reader starts have no remembered request line).  Completed
operations reclaim their mapping immediately: the matching RESULT pops it, and
slapd logging the connection close drops all of a connection's mappings.  For
operations that never complete (abandons, persistent searches), an hourly two
pass sweep on the reader's event loop frees mappings born before the previous
sweep, so those live at least one hour and at most two, and cannot
accumulate.  With no suffixes configured the label is empty and no request
tracking happens.
"""
import asyncio
import logging
import os
import re
import stat
import threading

from pathlib import Path

from watchfiles import awatch, Change

from prometheus_client import Histogram

# Result tag values from the LDAP protocol, per
# https://github.com/openldap/openldap/blob/45c7590378c60120e9ba3295ba1f223ceffa1de9/include/ldap.h#L536-L547
TAG_TO_OPERATION = {
    97: 'bind',
    101: 'search',
    103: 'modify',
    105: 'add',
    107: 'delete',
    109: 'modrdn',
    111: 'compare',
    120: 'extended',
}

# Extended operation results carry oid= where every other result carries tag=
RESULT_LINE_RE = re.compile(
    r'(?:tag=(?P<tag>\d+)|oid=(?P<oid>\S*)) err=-?\d+ '
    r'qtime=(?P<qtime>\d+\.\d+) etime=(?P<etime>\d+\.\d+)'
)

# The DN-bearing request lines: SRCH base="..." plus dn="..." for the others.
# Extended operations (EXT oid=...) carry no DN and stay unmatched on purpose.
REQUEST_LINE_RE = re.compile(
    r'conn=(?P<conn>\d+) op=(?P<op>\d+) '
    r'(?:SRCH base|(?:BIND|ADD|MOD|MODRDN|DEL|CMP) dn)="(?P<dn>[^"]*)"'
)

CONN_OP_RE = re.compile(r'conn=(?P<conn>-?\d+) op=(?P<op>\d+) ')

CONN_CLOSED_RE = re.compile(r'conn=(?P<conn>\d+) fd=-?\d+ closed')

SUFFIX_UNKNOWN = 'unknown'

# Bounds on the (conn, op) -> DN table, tripped only if close lines go missing
TRACKED_CONNECTIONS_MAX = 16384
TRACKED_OPERATIONS_PER_CONNECTION_MAX = 4096

# Completed operations reclaim their mapping immediately (the RESULT pops it,
# the connection close drops it).  The two pass timer sweep only reclaims
# mappings for operations that never complete (abandons, persistent searches):
# a mapping survives the first sweep after it was tracked and is freed by the
# second, so it lives at least one sweep interval and at most two.  An
# operation that outlives its mapping is still counted when a RESULT finally
# arrives, labelled suffix="unknown".
TRACKED_REQUEST_SWEEP_SECONDS = 3600

# slapd reports with microsecond resolution; the low edges capture healthy
# operations (queue latency is normally single-digit microseconds)
LATENCY_BUCKETS_SECONDS = [
    0.00001, 0.000025, 0.00005, 0.0001, 0.00025, 0.0005,
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05,
    0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
]

READ_CHUNK_BYTES = 65536

METRICS_NAMESPACE_DEFAULT = 'openldap'

METRICS_LABEL_NAMES = ('database', 'operation', 'suffix')

_queue_latency_histogram = None
_operation_latency_histogram = None


def register_metrics(namespace=METRICS_NAMESPACE_DEFAULT) -> tuple:
    """Create the two latency histograms, once per process.

    The histograms go straight to the prometheus_client library, not
    through OpenCensus: OpenCensus deep-copies its entire accumulated view
    state on every single recording (~100us per log line, worse as label
    combinations accumulate), while a direct histogram observation is a
    couple of additions (~1us).  A busy slapd produces tens of thousands
    of result lines per second, and a reader that cannot keep up fills the
    pipe and stalls slapd's worker threads.  The trade: prometheus_client
    metrics only exist on the Prometheus metrics page, so the Stackdriver
    exporter never sees these two histograms.

    prometheus_client serves whatever is on its default registry, and the
    Prometheus exporter this service starts serves exactly that registry,
    so the histograms appear next to the cn=Monitor statistics with no
    extra wiring.  Every reader calls in, so a module guard makes the
    second and later calls return the histograms already made.
    """
    global _queue_latency_histogram, _operation_latency_histogram

    if _queue_latency_histogram is None:
        _queue_latency_histogram = Histogram(
            'queue_latency_seconds',
            'Time an operation spent queued before a worker thread picked it up '
            '(qtime in the slapd stats log)',
            namespace=namespace,
            labelnames=METRICS_LABEL_NAMES,
            buckets=LATENCY_BUCKETS_SECONDS,
        )
        _operation_latency_histogram = Histogram(
            'operation_latency_seconds',
            'Wall clock time from receipt of the operation until its result was sent, '
            'includes queue latency (etime in the slapd stats log)',
            namespace=namespace,
            labelnames=METRICS_LABEL_NAMES,
            buckets=LATENCY_BUCKETS_SECONDS,
        )

    return _queue_latency_histogram, _operation_latency_histogram


def normalize_dn(dn):
    """Lowercase a DN and strip spaces after commas, so comparisons match."""
    return re.sub(r',\s+', ',', dn.strip().lower())


def len_of_normalized(suffix_pair):
    """Sort key: the normalized DN's length in a (normalized, display) pair."""
    return len(suffix_pair[0])


class LdapStatsLogPipeReader:
    def __init__(self, database=None, pipe_path=None, suffixes=None,
                 namespace=METRICS_NAMESPACE_DEFAULT):
        if database is None:
            raise ValueError('LdapStatsLogPipeReader requires the database name for metric labelling')
        if pipe_path is None:
            raise ValueError('LdapStatsLogPipeReader requires the path of the pipe to read')

        self.database = database
        self.pipe_path = pipe_path
        # longest suffix first, so nested suffixes match their deepest entry
        self.suffixes = sorted(
            [(normalize_dn(suffix), suffix) for suffix in (suffixes or [])],
            key=len_of_normalized, reverse=True
        )
        self.queue_histogram, self.operation_histogram = register_metrics(namespace)

        self.source_fd: int = None
        self.buffer: bytes = b''
        self.thread: threading.Thread = None
        self.loop: asyncio.AbstractEventLoop = None
        self.stop_event: asyncio.Event = None
        self.request_dn_table: dict = {}
        self.sweep_generation: int = 0
        self.latency_child_table: dict = {}

    def start(self) -> None:
        """Start reading the pipe on a background thread.

        The thread runs its own event loop, so the exporter's LDAP polling
        loop is never held up by pipe work.
        """
        self.thread = threading.Thread(
            target=self.run,
            name=f'stats-pipe-{self.database}',
            daemon=True
        )
        self.thread.start()

    def run(self) -> None:
        """The background thread's body: read the pipe until stop() is called."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.stop_event = asyncio.Event()
        if self.suffixes:
            self._schedule_sweep()

        self.loop.run_until_complete(self._monitor())

        # Only reached via stop().  The watch generator has already exited
        # normal iteration and cleaned up its native worker; release the pipe
        # and the loop so the thread finishes with nothing still running
        self._close()
        self.loop.close()

    def stop(self) -> None:
        """Stop the background thread and wait for the thread to finish."""
        if self.thread is None:
            return

        self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=10)
        self.thread = None

    def _schedule_sweep(self) -> None:
        """Run one sweep, then book the next one on the event loop."""
        self._sweep_expired()
        self.loop.call_later(TRACKED_REQUEST_SWEEP_SECONDS, self._schedule_sweep)

    def _sweep_expired(self) -> None:
        """Free remembered request DNs old enough that no result is coming.

        Entries born before the previous sweep are freed; everything newer
        survives to the next sweep.  The comment on
        TRACKED_REQUEST_SWEEP_SECONDS explains the resulting lifetimes.
        """
        for conn in list(self.request_dn_table):
            operation_dn_table = self.request_dn_table[conn]
            for operation in list(operation_dn_table):
                _, born_generation = operation_dn_table[operation]
                if born_generation < self.sweep_generation:
                    del operation_dn_table[operation]
            if not operation_dn_table:
                del self.request_dn_table[conn]

        self.sweep_generation += 1

    async def _monitor(self) -> None:
        """Open the pipe, then watch the pipe's directory until stopped.

        The directory watch only reacts to the pipe being created or
        deleted; data arriving is handled by _read() via the event loop.
        """
        pipe = str(Path(self.pipe_path).absolute())

        self._open()
        async for changes in awatch(Path(pipe).parent, stop_event=self.stop_event):
            for change_type, change_path in changes:
                if change_path != pipe:
                    continue
                match change_type:
                    case Change.added:
                        self._open()
                    case Change.deleted:
                        self._close()

    def _open(self) -> None:
        """Open the pipe and ask the event loop to call _read() on new data."""
        if self.source_fd is not None:
            return

        # O_RDWR, not O_RDONLY: holding our own write end keeps the pipe's
        # writer count above zero, so reads never return end of file when
        # slapd restarts.  Linux-defined behaviour, see fifo(7).
        try:
            source_fd = os.open(self.pipe_path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as error:
            logging.error(f'Cannot open stats pipe {self.pipe_path}: {error}')
            return

        if not stat.S_ISFIFO(os.fstat(source_fd).st_mode):
            logging.error(f'{self.pipe_path} is not a named pipe (FIFO), refusing to read it')
            os.close(source_fd)
            return

        self.source_fd = source_fd
        self.buffer = b''

        # Pipe data is not a filesystem event, so the directory watch never
        # sees writes; the event loop polls the descriptor for readability
        if self.loop is not None:
            self.loop.add_reader(source_fd, self._read)

        logging.info(f'{self.database}: reading slapd stats lines from {self.pipe_path}')
        self._read()

    def _close(self) -> None:
        """Close the pipe and forget any half-read line."""
        if self.source_fd is None:
            return

        if self.loop is not None:
            self.loop.remove_reader(self.source_fd)
        os.close(self.source_fd)
        self.source_fd = None
        self.buffer = b''
        logging.info(f'{self.database}: stats pipe {self.pipe_path} removed, waiting for it to reappear')

    def _read(self) -> None:
        """Drain everything sitting in the pipe and process complete lines."""
        if self.source_fd is None:
            return

        while True:
            try:
                data = os.read(self.source_fd, READ_CHUNK_BYTES)
            except BlockingIOError:
                break
            except OSError as error:
                logging.error(f'{self.database}: error reading stats pipe {self.pipe_path}: {error}')
                self._close()
                return

            # End of file means no writer right now.  Keep the descriptor:
            # a restarted slapd reattaches to the same pipe.
            if data == b'':
                break

            self.buffer += data
            self._drain_buffer()

    def _drain_buffer(self) -> None:
        """Split the buffer into complete lines and record each one.

        A partial line at the end stays in the buffer until the rest of
        the line arrives.
        """
        while True:
            line, separator, remainder = self.buffer.partition(b'\n')
            if separator == b'':
                break
            self.buffer = remainder
            self._record(line.decode('utf-8', errors='replace'))

    def _record(self, line: str) -> None:
        """Sort one log line to the right handler.

        A result line records timings, a request line remembers the target
        DN, a connection close line drops that connection's DNs, and
        anything else is ignored.

        Every stats line lands here, so each regex sits behind a substring
        gate: `in` runs in C and costs a third of a failed regex search.
        """
        if 'qtime=' in line:
            matched = RESULT_LINE_RE.search(line)
            if matched:
                self._record_result(line, matched)
                return

        # Request tracking only exists to resolve the suffix label
        if not self.suffixes:
            return

        if 'base="' in line or 'dn="' in line:
            matched = REQUEST_LINE_RE.search(line)
            if matched:
                self._track_request(matched)
                return

        if ' closed' in line:
            matched = CONN_CLOSED_RE.search(line)
            if matched:
                self.request_dn_table.pop(int(matched['conn']), None)

    def _record_result(self, line: str, matched: re.Match) -> None:
        """Record one completed operation's two timings with the metric labels.

        The labels() lookup costs more than the observation, so the handle
        for each (operation, suffix) pair is cached on first use.  The pair
        count is small and closed: eight operations times the configured
        suffixes plus "unknown".
        """
        operation = self._operation(line, matched)
        suffix = self._result_suffix(line)

        children = self.latency_child_table.get((operation, suffix))
        if children is None:
            children = (
                self.queue_histogram.labels(self.database, operation, suffix),
                self.operation_histogram.labels(self.database, operation, suffix),
            )
            self.latency_child_table[(operation, suffix)] = children

        children[0].observe(float(matched['qtime']))
        children[1].observe(float(matched['etime']))

    def _track_request(self, matched: re.Match) -> None:
        """Remember the DN a request targets, keyed by connection and operation.

        When a table hits a size cap the oldest entry is evicted rather
        than letting the table grow without bound.
        """
        conn = int(matched['conn'])

        operation_dn_table = self.request_dn_table.get(conn)
        if operation_dn_table is None:
            if len(self.request_dn_table) >= TRACKED_CONNECTIONS_MAX:
                self.request_dn_table.pop(next(iter(self.request_dn_table)))
            operation_dn_table = {}
            self.request_dn_table[conn] = operation_dn_table

        if len(operation_dn_table) >= TRACKED_OPERATIONS_PER_CONNECTION_MAX:
            operation_dn_table.pop(next(iter(operation_dn_table)))
        operation_dn_table[int(matched['op'])] = (matched['dn'], self.sweep_generation)

    def _result_suffix(self, line: str) -> str:
        """Find the suffix label for a result line.

        Pops the DN remembered for the result's connection and operation,
        then returns the longest configured suffix the DN falls under, or
        "unknown" when nothing was remembered or nothing matches.
        """
        if not self.suffixes:
            return ''

        conn_op = CONN_OP_RE.search(line)
        if not conn_op:
            return SUFFIX_UNKNOWN

        operation_dn_table = self.request_dn_table.get(int(conn_op['conn']))
        entry = operation_dn_table.pop(int(conn_op['op']), None) if operation_dn_table else None
        if entry is None:
            return SUFFIX_UNKNOWN
        dn, _ = entry

        normalized_dn = normalize_dn(dn)
        for normalized_suffix, display_suffix in self.suffixes:
            if normalized_dn == normalized_suffix or normalized_dn.endswith(',' + normalized_suffix):
                return display_suffix

        return SUFFIX_UNKNOWN

    @staticmethod
    def _operation(line: str, matched: re.Match) -> str:
        """Name the operation for the label from the result line's tag.

        oid= in place of tag= means an extended operation, and DISCONNECT
        lines get their own name.
        """
        if ' DISCONNECT ' in line:
            return 'disconnect'
        if matched['oid'] is not None:
            return 'extended'
        return TAG_TO_OPERATION.get(int(matched['tag']), 'other')
