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
End to end test of the statsLogPipe latency path against a real slapd.

The CI workflow (.github/workflows/ci.yml) runs slapd as a service container
from the stock bitnami OpenLDAP image and creates a named pipe (FIFO) in a
docker volume both containers share at /stats.  This test then:

1. Starts the exporter, which opens the pipe's read end.
2. Points the running slapd at the pipe by setting olcLogFile through the
   cn=config admin, the same way production enables it on a live server.
   slapd's open of the pipe's write end blocks until a reader exists, so
   the test first waits for the exporter to hold the read end.
3. Generates LDAP traffic over the service network (ldap://openldap:389)
   with python-ldap and asserts the Prometheus metrics reflect it.

Counts are checked as ranges (expected to expected + slack) because slapd
may perform a small number of internal operations beyond the generated
traffic.
"""
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

import ldap

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = f'{TEST_DIR}/run'
PIPE_PATH = '/stats/stats.pipe'
LDAP_URI = 'ldap://openldap:389'
ADMIN_DN = 'cn=admin,dc=example,dc=org'
CONFIG_ADMIN_DN = 'cn=admin,cn=config'
ADMIN_PW = 'secret'
METRICS_URL = 'http://127.0.0.1:8000/metrics'
SUFFIX = 'dc=example,dc=org'

# The traffic generated below, asserted against afterwards.  Every
# connection performs one bind: 4 admin connections (seed, users, modifies,
# deletes) plus one per search.
SEARCH_COUNT = 50
USER_COUNT = 5
ADD_COUNT = 7      # 2 seed entries + USER_COUNT users
MOD_COUNT = 5
DEL_COUNT = 2
BIND_COUNT = 54    # SEARCH_COUNT + 4 admin binds

COUNT_SLACK = 5
READY_DEADLINE_SECONDS = 60
METRICS_DEADLINE_SECONDS = 30

SAMPLE_RE = re.compile(r'^openldap_(\w+)\{(.*)\} (\S+)$')
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def admin_connection():
    """Connect to the directory server and log in as the admin user."""
    connection = ldap.initialize(LDAP_URI)
    connection.simple_bind_s(ADMIN_DN, ADMIN_PW)
    return connection


def fetch_metrics():
    """Download the exporter's metrics page and unpack it into a dictionary.

    Each metric line becomes one entry, keyed by the metric name plus its
    labels, holding the metric's numeric value.
    """
    body = urllib.request.urlopen(METRICS_URL).read().decode()

    samples = {}
    for line in body.splitlines():
        matched = SAMPLE_RE.match(line)
        if not matched:
            continue
        name, labels, value = matched.groups()
        samples[(name, frozenset(LABEL_RE.findall(labels)))] = float(value)
    return samples


def sample(samples, name, operation, suffix):
    """Look up one metric value by name, operation, and suffix.

    Returns 0.0 when the exporter has not published a matching metric yet.
    """
    labels = frozenset({
        ('database', 'citest'),
        ('operation', operation),
        ('suffix', suffix),
    })
    return samples.get((name, labels), 0.0)


@unittest.skipUnless(os.path.exists(PIPE_PATH),
                     'needs the slapd service container and the shared /stats volume, see ci.yml')
class TestStatsLog(unittest.TestCase):
    exporter = None

    @classmethod
    def setUpClass(cls):
        """Start the exporter and prepare slapd, run once before the test.

        Starts the exporter, waits for the exporter to hold the pipe and
        serve metrics, waits for slapd to answer, then tells slapd to write
        its stats lines to the pipe.
        """
        os.makedirs(RUN_DIR, exist_ok=True)
        exporter_bin = os.path.join(os.path.dirname(sys.executable), 'openldap_opencensus_stats')
        with open(f'{RUN_DIR}/exporter.log', 'wb') as exporter_log:
            cls.exporter = subprocess.Popen([exporter_bin, f'{TEST_DIR}/exporter.yml'],
                                            stdout=exporter_log, stderr=subprocess.STDOUT)

        cls.wait_for_pipe_reader()
        cls.wait_for_metrics_endpoint()
        cls.wait_for_slapd()
        cls.enable_stats_logging()

    @classmethod
    def tearDownClass(cls):
        """Stop the exporter, run once after the test."""
        if cls.exporter is not None:
            cls.exporter.terminate()
            cls.exporter.wait(timeout=15)

    @staticmethod
    def wait_for_pipe_reader():
        """Wait until the exporter has opened the pipe for reading.

        slapd blocks opening the pipe when no reader exists, so the test
        must not point slapd at the pipe until the exporter holds the read
        end.  The probe: a non-blocking write open of a named pipe fails
        with ENXIO until some process has the pipe open for reading, see
        fifo(7), so the open succeeding is the all-clear.
        """
        deadline = time.monotonic() + READY_DEADLINE_SECONDS
        while True:
            try:
                os.close(os.open(PIPE_PATH, os.O_WRONLY | os.O_NONBLOCK))
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    @staticmethod
    def wait_for_slapd():
        """Wait until slapd answers an admin login."""
        deadline = time.monotonic() + READY_DEADLINE_SECONDS
        while True:
            try:
                admin_connection().unbind_s()
                return
            except ldap.SERVER_DOWN:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    @staticmethod
    def wait_for_metrics_endpoint():
        """Wait until the exporter serves its metrics page."""
        deadline = time.monotonic() + READY_DEADLINE_SECONDS
        while True:
            try:
                fetch_metrics()
                return
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    @staticmethod
    def enable_stats_logging():
        """Tell the running slapd to write its stats log to the pipe.

        Sets olcLogFile through the config admin, the same change
        production makes on a live server.  The debug mask that selects
        the stats lines (-d 256) is already set by the service environment
        in ci.yml.  Operations before the change are simply not counted,
        so the traffic below starts from zero.
        """
        connection = ldap.initialize(LDAP_URI)
        connection.simple_bind_s(CONFIG_ADMIN_DN, ADMIN_PW)
        connection.modify_s('cn=config', [
            (ldap.MOD_REPLACE, 'olcLogFile', [PIPE_PATH.encode()]),
        ])
        connection.unbind_s()

    @staticmethod
    def wait_for_search_count():
        """Poll the metrics until every search shows up, then return them.

        Gives up after METRICS_DEADLINE_SECONDS and returns whatever is
        published, letting the count assertions report the shortfall.
        """
        deadline = time.monotonic() + METRICS_DEADLINE_SECONDS
        while True:
            samples = fetch_metrics()
            if sample(samples, 'operation_latency_seconds_count', 'search', SUFFIX) >= SEARCH_COUNT:
                return samples
            if time.monotonic() >= deadline:
                return samples
            time.sleep(1)

    @staticmethod
    def seed_directory():
        """Create the top entry and the ou=people branch the users live under."""
        connection = admin_connection()
        connection.add_s(SUFFIX, [
            ('objectClass', [b'dcObject', b'organization']),
            ('dc', [b'example']),
            ('o', [b'Example Org']),
        ])
        connection.add_s(f'ou=people,{SUFFIX}', [
            ('objectClass', [b'organizationalUnit']),
            ('ou', [b'people']),
        ])
        connection.unbind_s()

    @staticmethod
    def add_users():
        """Add USER_COUNT test users under ou=people."""
        connection = admin_connection()
        for i in range(1, USER_COUNT + 1):
            connection.add_s(f'uid=user{i},ou=people,{SUFFIX}', [
                ('objectClass', [b'inetOrgPerson']),
                ('uid', [f'user{i}'.encode()]),
                ('cn', [f'User {i}'.encode()]),
                ('sn', [b'Test']),
                ('mail', [f'user{i}@example.org'.encode()]),
            ])
        connection.unbind_s()

    @staticmethod
    def run_searches():
        """Search for a user SEARCH_COUNT times.

        Each search opens a fresh connection and logs in, one bind per
        search, which is what BIND_COUNT is built from.
        """
        for i in range(SEARCH_COUNT):
            n = (i % USER_COUNT) + 1
            connection = admin_connection()
            connection.search_s(SUFFIX, ldap.SCOPE_SUBTREE, f'(uid=user{n})')
            connection.unbind_s()

    @staticmethod
    def run_modifies():
        """Change the mail address of MOD_COUNT users."""
        connection = admin_connection()
        for i in range(1, MOD_COUNT + 1):
            connection.modify_s(f'uid=user{i},ou=people,{SUFFIX}', [
                (ldap.MOD_REPLACE, 'mail', [f'user{i}-changed@example.org'.encode()]),
            ])
        connection.unbind_s()

    @staticmethod
    def run_deletes():
        """Delete DEL_COUNT users."""
        connection = admin_connection()
        connection.delete_s(f'uid=user4,ou=people,{SUFFIX}')
        connection.delete_s(f'uid=user5,ou=people,{SUFFIX}')
        connection.unbind_s()

    @staticmethod
    def run_unknown_suffix_search():
        """Search the server's own top entry (the root DSE) anonymously.

        The root DSE sits under no configured suffix, so the timing for
        both the search and its anonymous bind must land in the
        suffix="unknown" bucket, which the test asserts on.
        """
        connection = ldap.initialize(LDAP_URI)
        connection.simple_bind_s()
        connection.search_s('', ldap.SCOPE_BASE, '(objectClass=*)')
        connection.unbind_s()

    def assert_count(self, samples, operation, expected):
        """Check one operation's count sits between expected and expected + slack."""
        value = sample(samples, 'operation_latency_seconds_count', operation, SUFFIX)
        self.assertGreaterEqual(value, expected, f'{operation}/{SUFFIX} count')
        self.assertLessEqual(value, expected + COUNT_SLACK, f'{operation}/{SUFFIX} count')

    def test_stats_log(self):
        """Generate the traffic, then check the published metrics reflect it."""
        self.seed_directory()
        self.add_users()
        self.run_searches()
        self.run_modifies()
        self.run_deletes()
        self.run_unknown_suffix_search()

        samples = self.wait_for_search_count()

        self.assert_count(samples, 'search', SEARCH_COUNT)
        self.assert_count(samples, 'add', ADD_COUNT)
        self.assert_count(samples, 'modify', MOD_COUNT)
        self.assert_count(samples, 'delete', DEL_COUNT)
        self.assert_count(samples, 'bind', BIND_COUNT)

        # The rootDSE search and its anonymous bind carry no configured suffix
        self.assertGreaterEqual(
            sample(samples, 'operation_latency_seconds_count', 'search', 'unknown'), 1)

        # Latency sanity: both timers start at receipt, so summed queue
        # latency can never exceed summed operation latency, and a local
        # search should average well under half a second
        queue_sum = sample(samples, 'queue_latency_seconds_sum', 'search', SUFFIX)
        operation_sum = sample(samples, 'operation_latency_seconds_sum', 'search', SUFFIX)
        operation_count = sample(samples, 'operation_latency_seconds_count', 'search', SUFFIX)
        self.assertLessEqual(queue_sum, operation_sum)
        self.assertGreater(operation_sum, 0.0)
        self.assertLess(operation_sum, 60.0)
        self.assertGreater(operation_count, 0)
        self.assertLess(operation_sum / operation_count, 0.5)


if __name__ == '__main__':
    unittest.main()
