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
Tests for the configuration transformer chain.

The chain talks no LDAP for these configurations: connections are created
lazily (ReconnectLDAPObject does not connect at construction) and only an
`object` tree with `children` or name interpolation triggers queries.
"""
from openldap_opencensus_stats.config_transformers.base import ConfigurationTransformationChainSingleton
from openldap_opencensus_stats.config_transformers.snake_case import SnakeCaseConfigurationTransformer


def transform(configuration):
    return ConfigurationTransformationChainSingleton().transform_configuration(configuration)


def test_server_without_object_tree():
    # Regression: a server entry configured only for statsLogPipe defines no
    # cn=Monitor metrics, and the chain used to raise on the missing object
    # key (ChildObjectConfigurationTransformer: "All arguments must exist"),
    # killing the exporter at startup
    config = {
        'ldapServers': [
            {
                'database': 'transformer-test-pipe-only',
                'connection': {'serverUri': 'ldap://127.0.0.1:13890/'},
                'statsLogPipe': {
                    'pipe': '/run/slapd-stats/stats.pipe',
                    'suffixes': ['dc=example,dc=org'],
                },
            },
        ],
    }

    normalized = transform(config)

    server = normalized['ldap_servers'][0]
    assert server['stats_log_pipe']['pipe'] == '/run/slapd-stats/stats.pipe'
    assert server['stats_log_pipe']['suffixes'] == ['dc=example,dc=org']
    assert not server.get('object')


def test_sync_only_server_without_object_tree():
    config = {
        'ldapServers': [
            {
                'database': 'transformer-test-sync-only',
                'connection': {'serverUri': 'ldap://127.0.0.1:13891/'},
                'syncOnly': True,
            },
        ],
    }

    normalized = transform(config)

    assert normalized['ldap_servers'][0]['sync_only'] is True


def test_snake_case_keys_not_values():
    config = {
        'ldapServers': [
            {
                'database': 'CamelCaseName',
                'connection': {'serverUri': 'ldap://host/', 'startTls': False},
                'statsLogPipe': {'suffixes': ['dc=Example,dc=Org']},
            },
        ],
    }

    snaked = SnakeCaseConfigurationTransformer.process(config)

    server = snaked['ldap_servers'][0]
    # Keys are converted at every level; values are never touched
    assert server['database'] == 'CamelCaseName'
    assert server['connection']['server_uri'] == 'ldap://host/'
    assert server['connection']['start_tls'] is False
    assert server['stats_log_pipe']['suffixes'] == ['dc=Example,dc=Org']
