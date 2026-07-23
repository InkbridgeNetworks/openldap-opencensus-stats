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
Unit tests for the stats log pipe reader, through a real FIFO and the real
Prometheus exporter.

One exporter serves the whole session (the Prometheus collector registers
globally, a second would collide), so each test uses its own database label
and asserts only on its own series.

The synchronous tests drive _open/_read directly.  The event loop test runs
the real thread, waking on a counter wrapped around _record rather than
polling, so delivery is observed as an event.
"""
import os
import threading
import urllib.request

import pytest

from opencensus.ext.prometheus import stats_exporter
from opencensus.stats import stats

from openldap_opencensus_stats.ldap_stats_log_pipe import LdapStatsLogPipeReader

METRICS_URL = 'http://127.0.0.1:18123/metrics'

SUFFIXES = ['dc=example,dc=org', 'cn=accesslog']


class RecordCounter:
    """Wraps a reader's _record; sets an event once enough lines arrived."""

    def __init__(self, reader, target):
        self.record = reader._record
        self.target = target
        self.seen = 0
        self.done = threading.Event()

    def __call__(self, line):
        self.record(line)
        self.seen += 1
        if self.seen >= self.target:
            self.done.set()


@pytest.fixture(scope='session')
def metrics_endpoint():
    exporter = stats_exporter.new_stats_exporter(
        stats_exporter.Options(namespace='openldap', port=18123, address='127.0.0.1'))
    stats.stats.view_manager.register_exporter(exporter)
    return METRICS_URL


@pytest.fixture
def fifo(tmp_path):
    path = str(tmp_path / 'stats.pipe')
    os.mkfifo(path)
    return path


def scrape(url):
    return urllib.request.urlopen(url).read().decode()


def test_pipeline_labels_and_eviction(metrics_endpoint, fifo):
    reader = LdapStatsLogPipeReader(database='synctest', pipe_path=fifo, suffixes=SUFFIXES)
    reader._open()
    assert reader.source_fd is not None

    # Succeeds only because the reader already holds the pipe open, the
    # same ordering slapd depends on
    write_fd = os.open(fifo, os.O_WRONLY)

    lines = [
        # search under the main suffix, DN with case and space variance
        'conn=1001 op=2 SRCH base="DC=Example, dc=ORG" scope=2 deref=0 filter="(uid=bob)"',
        'Jul 20 18:00:01 ldap1 slapd[1234]: conn=1001 op=2 SEARCH RESULT tag=101 err=0 '
        'qtime=0.000012 etime=0.001234 nentries=5 text=',
        # bind, exact suffix match
        'conn=1001 op=0 BIND dn="dc=example,dc=org" method=128',
        'conn=1001 op=0 RESULT tag=97 err=0 qtime=0.000045 etime=0.000210 text=',
        # modify under the accesslog suffix
        'conn=1002 op=1 MOD dn="reqStart=20260721000000.000001Z,cn=accesslog"',
        'conn=1002 op=1 RESULT tag=103 err=0 qtime=0.000100 etime=0.020000 text=',
        # extended op: no DN-bearing request line -> unknown
        'conn=1003 op=1 RESULT oid= err=0 qtime=0.000033 etime=0.000900 text=',
        # request evicted by connection close before its late result -> unknown
        'conn=1005 op=7 DEL dn="uid=gone,dc=example,dc=org"',
        'conn=1005 fd=42 closed (connection lost)',
        'conn=1005 op=7 RESULT tag=107 err=0 qtime=0.000500 etime=0.500000 text=',
        # DN outside every configured suffix -> unknown
        'conn=1006 op=1 SRCH base="cn=config" scope=0 deref=0 filter="(objectClass=*)"',
    ]
    for line in lines:
        os.write(write_fd, line.encode() + b'\n')

    # A line split across two writes must be stitched back together
    split = b'conn=1006 op=1 SEARCH RESULT tag=101 err=0 qtime=0.000009 etime=0.000090 nentries=1 text=\n'
    os.write(write_fd, split[:40])
    reader._read()
    os.write(write_fd, split[40:])
    reader._read()
    os.close(write_fd)

    body = scrape(metrics_endpoint)
    for needle in (
        'operation_latency_seconds_count{database="synctest",operation="search",suffix="dc=example,dc=org"} 1.0',
        'queue_latency_seconds_count{database="synctest",operation="bind",suffix="dc=example,dc=org"} 1.0',
        'operation="modify",suffix="cn=accesslog"',
        'operation="extended",suffix="unknown"',
        'operation="delete",suffix="unknown"',
        'operation_latency_seconds_sum{database="synctest",operation="search",suffix="dc=example,dc=org"} 0.001234',
        'operation_latency_seconds_sum{database="synctest",operation="search",suffix="unknown"} 9e-05',
    ):
        assert needle in body

    # Untimed request lines must never be recorded
    assert 'SRCH' not in body
    # The connection close dropped the tracked mapping
    assert reader.request_dn_table.get(1005) is None

    reader._close()


def test_two_pass_sweep(fifo):
    reader = LdapStatsLogPipeReader(database='sweeptest', pipe_path=fifo, suffixes=SUFFIXES)
    reader._open()
    write_fd = os.open(fifo, os.O_WRONLY)

    os.write(write_fd, b'conn=1 op=1 SRCH base="uid=slow,dc=example,dc=org" scope=0 deref=0 filter="(x=y)"\n')
    reader._read()
    os.close(write_fd)
    assert reader.request_dn_table.get(1)

    reader._sweep_expired()
    assert reader.request_dn_table.get(1), 'first sweep must not reclaim a fresh mapping'

    reader._sweep_expired()
    assert reader.request_dn_table.get(1) is None, 'second sweep must reclaim the mapping'

    reader._close()


def test_event_loop_delivery(metrics_endpoint, fifo):
    reader = LdapStatsLogPipeReader(database='threadtest', pipe_path=fifo, suffixes=SUFFIXES)
    counter = RecordCounter(reader, target=40)
    reader._record = counter
    reader.start()

    # The reader thread must be joined before the interpreter exits, or
    # teardown kills it mid native call and the process can dump core
    try:
        # Blocks until the reader thread has the pipe open
        write_fd = os.open(fifo, os.O_WRONLY)
        for i in range(20):
            line = (f'conn=2000 op={i} SRCH base="dc=example,dc=org" scope=2 deref=0 filter="(uid=u{i})"\n'
                    f'conn=2000 op={i} SEARCH RESULT tag=101 err=0 qtime=0.000010 etime=0.000500 nentries=1 text=\n')
            os.write(write_fd, line.encode())
        os.close(write_fd)

        assert counter.done.wait(timeout=15), \
            f'event loop delivered {counter.seen} of {counter.target} lines'
    finally:
        reader.stop()

    assert reader.thread is None

    body = scrape(metrics_endpoint)
    assert ('operation_latency_seconds_count{database="threadtest",operation="search",'
            'suffix="dc=example,dc=org"} 20.0') in body
