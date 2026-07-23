import copy
import logging
import re

import ldap

from openldap_opencensus_stats.config_transformers.base import ConfigurationTransformer


class ChildObjectConfigurationTransformer(ConfigurationTransformer):
    @staticmethod
    def process(configuration):
        if not isinstance(configuration, dict):
            raise ValueError('The top level of the configuration must be a mapping.')

        config = copy.deepcopy(configuration)
        for server_config in config.get('ldap_servers'):
            ldap_server = ChildObjectConfigurationTransformer.get_ldap_server(server_config)
            # A server entry with no object tree defines no cn=Monitor
            # metrics (e.g. statsLogPipe only) and has nothing to transform
            if not server_config.get('sync_only', False) and server_config.get('object'):
                server_config['object'] = ChildObjectConfigurationTransformer.process_objects_for_ldap_server(
                    configuration=server_config.get('object'),
                    dn='',
                    ldap_server=ldap_server
                )

        return config

    @staticmethod
    def process_objects_for_ldap_server(configuration, dn, ldap_server):
        if not configuration or not ldap_server:
            raise ValueError(
                'ChildObjectConfigurationTransformer.process_objects_for_ldap_server: All arguments must exist'
            )

        if isinstance(configuration, dict):
            config = {}
            for key, value in configuration.items():
                proper_value = ChildObjectConfigurationTransformer.process_objects_for_ldap_server(
                    value,
                    configuration.get('computed_dn', dn),
                    ldap_server
                )
                if key == 'children':
                    if not isinstance(proper_value, dict):
                        raise TypeError("A configuration entry of 'children' must contain a mapping.")

                    for child_dn in ChildObjectConfigurationTransformer.get_child_keys(dn, ldap_server):
                        child_value = copy.deepcopy(proper_value)
                        child_value['computed_dn'] = child_dn
                        child_value['computed_rdn'] = re.sub(r',.*', '', child_dn)
                        new_key = re.sub(r',.*', '', re.sub(r'^[^=]*=', '', child_dn))
                        if re.match(child_value.get('rdn'), child_value.get('computed_rdn')):
                            config[new_key] = child_value
                else:
                    config[key] = proper_value
            return config
        elif isinstance(configuration, list):
            return [
                ChildObjectConfigurationTransformer.process_objects_for_ldap_server(item, dn, ldap_server)
                for item in configuration
            ]
        else:
            return configuration

    @staticmethod
    def get_child_keys(dn, ldap_server):
        ldap_result = ldap_server.query(dn=dn, scope=ldap.SCOPE_ONELEVEL)
        if not ldap_result:
            logging.warning(f"Found no child objects for {dn} on LDAP database {ldap_server.database}!")
        return [
            result_item[0]
            for result_item in ldap_result
        ]
