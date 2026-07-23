import copy
import logging
import logging.config

import yaml
from time import sleep

from openldap_opencensus_stats.config_transformers.base import ConfigurationTransformationChainSingleton
from openldap_opencensus_stats.ldap_metric_set import MetricSet
from openldap_opencensus_stats.sync_metric_set import SyncMetricSet
from openldap_opencensus_stats.ldap_server import LdapServerPool
from openldap_opencensus_stats.ldap_statistic import LdapStatistic
from openldap_opencensus_stats.ldap_stats_log_pipe import LdapStatsLogPipeReader, METRICS_NAMESPACE_DEFAULT

from opencensus.stats import stats
from opencensus.ext.prometheus import stats_exporter
import opencensus.ext.stackdriver.stats_exporter

# Make up for broken code in the Prometheus exporter
import opencensus.stats.aggregation_data
opencensus.stats.aggregation_data.SumAggregationDataFloat = opencensus.stats.aggregation_data.SumAggregationData

SUPPORTED_EXPORTERS = ['Prometheus', 'Stackdriver']


class Configuration:
    def __init__(self, config_file_name):
        if config_file_name is None:
            raise ValueError("Config file name must be supplied")
        self._config_file_name = config_file_name
        self._configuration_dict = {}
        self._sleep_time = 5
        self._metric_sets = []
        self._ldap_metrics = {}
        self._stats_log_pipe_readers = []

        self.reconfigure()

    def reconfigure(self):
        self._configuration_dict = read_yaml_file(self._config_file_name)
        normalized_configuration = ConfigurationTransformationChainSingleton().transform_configuration(
            self._configuration_dict
        )

        self._sleep_time = normalized_configuration.get('period', 5)
        log_config = normalized_configuration.get('log_config')
        if log_config and isinstance(log_config, dict):
            log_config['version'] = log_config.get('version', 1)
            logging.config.dictConfig(log_config)
        prometheus_namespace = METRICS_NAMESPACE_DEFAULT
        for exporter_config in normalized_configuration.get('exporters', []):
            exporter = create_exporter(exporter_config)
            stats.stats.view_manager.register_exporter(exporter)
            if exporter_config.get('name') == 'Prometheus':
                prometheus_namespace = exporter.options.namespace

        for ldap_server_config in normalized_configuration.get('ldap_servers', []):
            if not ldap_server_config.get('sync_only', False):
                metric_set = self.generate_metric_set(ldap_server_config)
                self._metric_sets.append(metric_set)
            stats_log_pipe = ldap_server_config.get('stats_log_pipe')
            if stats_log_pipe:
                if isinstance(stats_log_pipe, str):
                    stats_log_pipe = {'pipe': stats_log_pipe}
                stats_log_pipe_reader = LdapStatsLogPipeReader(
                    database=ldap_server_config.get('database'),
                    pipe_path=stats_log_pipe.get('pipe'),
                    suffixes=stats_log_pipe.get('suffixes'),
                    namespace=prometheus_namespace
                )
                stats_log_pipe_reader.start()
                self._stats_log_pipe_readers.append(stats_log_pipe_reader)

        for base_dn, sync_config in normalized_configuration.get('sync', {}).items():
            ldap_server_names = sync_config.get('cluster_servers', [])
            ldap_servers = [
                LdapServerPool().get_ldap_server(**server)
                for server in normalized_configuration.get('ldap_servers', [])
                if server['database'] in ldap_server_names
            ]
            report_servers = sync_config.get('report_servers', [])
            sync_metric_set = SyncMetricSet(
                base_dn=base_dn,
                ldap_servers=ldap_servers,
                report_servers=report_servers
            )
            self._metric_sets.append(sync_metric_set)

    def generate_metric_set(self, ldap_server_config):
        args = copy.deepcopy(ldap_server_config.get('connection', {}))
        args['database'] = ldap_server_config.get('database')
        ldap_server = LdapServerPool().get_ldap_server(**args)
        metric_set = MetricSet(ldap_server=ldap_server)
        configs = [ldap_server_config.get('object')]
        while configs:
            config = configs.pop()
            if isinstance(config, list):
                for item in config:
                    configs.insert(0, item)
            elif isinstance(config, dict):
                dn = config.get('computed_dn')
                name = config.get('metric_name')
                attribute = config.get('attribute')
                unit = config.get('unit')
                description = config.get('description', '')
                value_function = config.get('func', 'value')
                query_dn = config.get('query_dn')
                if dn and name and attribute and unit and query_dn:
                    stat = self._ldap_metrics.get(name)
                    if not stat:
                        stat = LdapStatistic(
                            dn=dn,
                            name=name,
                            attribute=attribute,
                            description=description,
                            unit=unit,
                            value_function=value_function,
                            query_dn=query_dn,
                            tag_keys=['database']
                        )
                        self._ldap_metrics[name] = stat
                    metric_set.add_statistic(stat)
                for key, value in config.items():
                    configs.insert(0, value)
        return metric_set

    def metric_sets(self):
        return self._metric_sets

    def sleep(self):
        sleep(self._sleep_time)


def read_yaml_file(file_name):
    with open(file_name, 'r') as file:
        ret_val = yaml.safe_load(file)
    return ret_val


def create_exporter(exporter_configuration=None):
    if exporter_configuration is None:
        raise ValueError("Cannot create an exporter with no configuration!")

    name = exporter_configuration.get('name')
    if name not in SUPPORTED_EXPORTERS:
        logging.error(
            f"Requested exporter named {name}, which is not supported.  Choose from:{', '.join(SUPPORTED_EXPORTERS)}"
        )
        raise ValueError(
            f"Requested exporter named {name}, which is not supported.  Choose from:{', '.join(SUPPORTED_EXPORTERS)}"
        )

    options = exporter_configuration.get('options', {})
    exporter = None
    if "Prometheus" == name:
        if 'options' not in exporter_configuration:
            logging.error("The Prometheus exporter requires options configuration.")
            raise ValueError("The Prometheus exporter requires options configuration.")
        final_options = {'namespace': METRICS_NAMESPACE_DEFAULT, 'port': 8000, 'address': '0.0.0.0'}
        final_options.update(options)
        exporter = stats_exporter.new_stats_exporter(
            stats_exporter.Options(**final_options)
        )

    elif "Stackdriver" == name:
        exporter = opencensus.ext.stackdriver.stats_exporter.new_stats_exporter(interval=5)
        print(f"Exporting stats to this project {exporter.options.project_id}")

    return exporter
