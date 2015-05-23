from collections import namedtuple
import logging
import os
import yaml

from kafka.consumer.base import AUTO_COMMIT_MSG_COUNT
from kafka.consumer.base import AUTO_COMMIT_INTERVAL
from kafka.consumer.base import FETCH_MIN_BYTES
from kafka.consumer.kafka import DEFAULT_CONSUMER_CONFIG
from kafka.util import kafka_bytestring

from yelp_kafka.error import ConfigurationError


DEFAULT_KAFKA_TOPOLOGY_BASE_PATH = '/nail/etc/kafka_discovery'

# This is fixed to 1 MB for making a fetch call more efficient when dealing
# with ranger messages, that can be more than 100KB in size
KAFKA_BUFFER_SIZE = 1024 * 1024  # 1MB

ZOOKEEPER_BASE_PATH = '/yelp-kafka'
PARTITIONER_COOLDOWN = 30
MAX_TERMINATION_TIMEOUT_SECS = 10
MAX_ITERATOR_TIMEOUT_SECS = 0.1
DEFAULT_OFFSET_RESET = 'largest'
DEFAULT_CLIENT_ID = 'yelp-kafka'


cluster_configuration = {}


ClusterConfigTuple = namedtuple('ClusterConfig', ['name', 'broker_list', 'zookeeper'])
"""Tuple representing the cluster configuration.

* **name**\(``str``): Name of the cluster
* **broker_list**\(``list``): List of brokers
* **zookeeper**\(``str``): Zookeeper cluster
"""


class ClusterConfig(ClusterConfigTuple):
    def __ne__(self, other):
        return self.__hash__() != other.__hash__()

    def __eq__(self, other):
        return self.__hash__() == other.__hash__()

    def __hash__(self):
        if isinstance(self.broker_list, list):
            self.broker_list.sort()
            broker_list_str = ""
            for host in self.broker_list:
                broker_list_str = broker_list_str + host + ","
            broker_list_str = broker_list_str[:-1]
            my_hash = hash((self.name, broker_list_str, self.zookeeper))
        else:
            my_hash = hash((self.name, self.broker_list, self.zookeeper))

        return my_hash


def load_yaml_config(config_path):
    with open(config_path, 'r') as config_file:
        return yaml.safe_load(config_file)


class TopologyConfiguration(object):
    """Topology configuration for a kafka cluster.
    A topology configuration represents a kafka cluster
    in all the available regions at Yelp.

    :param kafka_id: kafka cluster id. Ex. standard or scribe
    :type kafka_id: string
    :param kafka_topology_path: path of the directory containing
        the kafka topology.yaml config
    :type kafka_topology_path: string
    """

    def __init__(self, kafka_id,
                 kafka_topology_path=DEFAULT_KAFKA_TOPOLOGY_BASE_PATH):
        if kafka_topology_path[len(kafka_topology_path) - 1] == '/':
            kafka_topology_path = kafka_topology_path[:-1]
        self.kafka_topology_path = kafka_topology_path
        self.kafka_id = kafka_id
        self.log = logging.getLogger(self.__class__.__name__)
        self.clusters = None
        self.local_config = None
        self.load_topology_config()

    def __eq__(self, other):
        if self.kafka_id != other.kafka_id:
            return False
        if self.kafka_topology_path != other.kafka_topology_path:
            return False
        return True

    def __ne__(self, other):
        if self.kafka_id == other.kafka_id:
            if self.kafka_topology_path == other.kafka_topology_path:
                return False
        return True

    def load_topology_config(self):
        """Load the topology configuration"""
        config_path = os.path.join(self.kafka_topology_path,
                                   '{id}.yaml'.format(id=self.kafka_id))
        self.log.debug("Loading configuration from %s", config_path)
        if os.path.isfile(config_path):
            topology_config = load_yaml_config(config_path)
        else:
            raise ConfigurationError(
                "Topology configuration {0} for cluster {1} "
                "does not exist".format(
                    config_path, self.kafka_id
                )
            )
        self.log.debug("Topology configuration %s", topology_config)
        try:
            self.clusters = topology_config['clusters']
            self.local_config = topology_config['local_config']
        except KeyError:
            self.log.exception("Invalid topology file")
            raise ConfigurationError("Invalid topology file {0}".format(
                config_path))

    def get_all_clusters(self):
        return [ClusterConfig(name, cluster['broker_list'], cluster['zookeeper'])
                for name, cluster in self.clusters.iteritems()]

    def get_local_cluster(self):
        try:
            if self.local_config:
                local_cluster = self.clusters[self.local_config['cluster']]
                return ClusterConfig(
                    name=self.local_config['cluster'],
                    broker_list=local_cluster['broker_list'],
                    zookeeper=local_cluster['zookeeper'])
        except KeyError:
            self.log.exception("Invalid topology file")
            raise ConfigurationError("Invalid topology file.")

    def get_scribe_local_prefix(self):
        """We use prefix only in the scribe cluster."""
        return self.local_config.get('prefix')

    def __repr__(self):
        return ("TopologyConfig: kafka_id {0}, clusters: {1},"
                "local_config {2}".format(
                    self.kafka_id,
                    self.clusters,
                    self.local_config
                ))


class KafkaConsumerConfig(object):
    """Config class for ConsumerGroup, MultiprocessingConsumerGroup,
    KafkaSimpleConsumer and KakfaConsumerBase.

    :param group_id: group of the kafka consumer
    :param cluster: cluster config from :py:mod:`yelp_kafka.discovery`
    :param config: keyword arguments, configuration arguments from kafka-python
        SimpleConsumer are accepted.
        See valid keyword arguments in: http://kafka-python.readthedocs.org/en/latest/apidoc/kafka.consumer.html#module-kafka.consumer.simple
        Note: Please do NOT specify topics and partitions as part of config. These should
        be specified when initializing the consumer.
        See: :py:mod:`yelp_kafka.consumer.py` or :py:mod:`yelp_kafka.consumer_group`

        Yelp_kafka specific configuration arguments are:

        * **auto_offset_reset**: Used by KafkaSimpleConsumer for offset validation.
          if 'largest' reset the offset to the latest available
          message (tail). If 'smallest' uses consumes from the
          earliest (head). Default: 'largest'.
        * **client_id**: client id to use on connection. Default: 'yelp-kafka'.
        * **partitioner_cooldown**: Waiting time for the consumer
          to acquire the partitions. Default: 30 seconds.
        * **max_termination_timeout_secs**: Used by MultiprocessinConsumerGroup
          time to wait for a consumer to terminate. Default 10 secs.
    """

    SIMPLE_CONSUMER_DEFAULT_CONFIG = {
        'buffer_size': KAFKA_BUFFER_SIZE,
        'auto_commit_every_n': AUTO_COMMIT_MSG_COUNT,
        'auto_commit_every_t': AUTO_COMMIT_INTERVAL,
        'auto_commit': True,
        'fetch_size_bytes': FETCH_MIN_BYTES,
        'max_buffer_size': None,
        'iter_timeout': MAX_ITERATOR_TIMEOUT_SECS,
    }
    """Default SimpleConsumer configuration"""

    def __init__(
        self, group_id, cluster,
        **config
    ):
        self._config = config
        self.cluster = cluster
        self.group_id = kafka_bytestring(group_id)

    def __eq__(self, other):
        if self._config != other._config:
            return False
        if self.cluster != other.cluster:
            return False
        if self.group_id != self.group_id:
            return False
        return True

    def __ne__(self, other):
        if self._config == other._config:
            if self.cluster == other.cluster:
                if self.group_id == other.group_id:
                    return False
        return True

    def get_simple_consumer_args(self):
        """Get the configuration args for kafka-python SimpleConsumer."""
        args = {}
        for key in self.SIMPLE_CONSUMER_DEFAULT_CONFIG.iterkeys():
            args[key] = self._config.get(
                key, self.SIMPLE_CONSUMER_DEFAULT_CONFIG[key]
            )
        args['group'] = self.group_id
        return args

    def get_kafka_consumer_config(self):
        """Get the configuration for kafka-python KafkaConsumer."""
        config = {}
        for key in DEFAULT_CONSUMER_CONFIG.iterkeys():
            config[key] = self._config.get(
                key, DEFAULT_CONSUMER_CONFIG[key]
            )
        config['group_id'] = self.group_id
        config['metadata_broker_list'] = self.cluster.broker_list
        return config

    @property
    def broker_list(self):
        return self.cluster.broker_list

    @property
    def zookeeper(self):
        return self.cluster.zookeeper

    @property
    def group_path(self):
        return '{zk_base}/{group_id}'.format(
            zk_base=ZOOKEEPER_BASE_PATH,
            group_id=self.group_id
        )

    @property
    def partitioner_cooldown(self):
        return self._config.get('partitioner_cooldown', PARTITIONER_COOLDOWN)

    @property
    def max_termination_timeout_secs(self):
        return self._config.get('max_termination_timeout_secs',
                                MAX_ITERATOR_TIMEOUT_SECS)

    @property
    def auto_offset_reset(self):
        return self._config.get('auto_offset_reset', DEFAULT_OFFSET_RESET)

    @property
    def client_id(self):
        return self._config.get('client_id', DEFAULT_CLIENT_ID)

    def __repr__(self):
        return ("KafkaConsumerConfig: cluster {cluster}, group {group} "
                "config: {config}".format(cluster=self.cluster,
                                          group=self.group_id,
                                          config=self._config))
