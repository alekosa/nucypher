from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict

from twisted.internet import task
from twisted.logger import Logger

from nucypher.blockchain.economics import TokenEconomicsFactory
from nucypher.blockchain.eth.token import NU, StakeList
from nucypher.blockchain.eth.utils import datetime_at_period
from nucypher.characters.chaotic import Moe
from influxdb import InfluxDBClient
from maya import MayaDT


class MoeBlockchainCrawler:
    """
    Obtain Blockchain information for Moe and output to a DB.
    """
    DEFAULT_REFRESH_RATE = 60  # seconds

    # InfluxDB Line Protocol Format (note the spaces, commas):
    # +-----------+--------+-+---------+-+---------+
    # |measurement|,tag_set| |field_set| |timestamp|
    # +-----------+--------+-+---------+-+---------+
    DB_MEASUREMENT = 'moe_network_info'
    DB_LINE_PROTOCOL = '{measurement},staker_address={staker_address} ' \
                           'worker_address="{worker_address}",' \
                           'start_date={start_date},' \
                           'end_date={end_date},' \
                           'stake={stake},' \
                           'locked_stake={locked_stake},' \
                           'current_period={current_period}i,' \
                           'last_confirmed_period={last_confirmed_period}i ' \
                       '{timestamp}'
    DB_NAME = 'network'

    DB_RETENTION_POLICY_NAME = 'network_info_retention'
    DB_RETENTION_POLICY_PERIOD = '5w'  # 5 weeks of data
    DB_RETENTION_POLICY_REPLICATION = '1'

    def __init__(self,
                 moe: Moe,
                 refresh_rate=DEFAULT_REFRESH_RATE,
                 restart_on_error=True):

        self._moe = moe
        self._refresh_rate = refresh_rate
        self._node_learning_task = task.LoopingCall(self._learn_about_nodes)
        self._contract_learning_task = task.LoopingCall(self._learn_about_contracts)
        self._restart_on_error = restart_on_error
        self.log = Logger('moe-crawler')

        # initialize InfluxDB
        self._client = InfluxDBClient(host='localhost', port=8086, database=self.DB_NAME)
        self._ensure_db_exists()

        self.__snapshot = dict()

    @property
    def snapshot(self):
        return self.__snapshot

    def _ensure_db_exists(self):
        db_list = self._client.get_list_database()
        found_db = (list(filter(lambda db: db['name'] == self.DB_NAME, db_list)))
        if len(found_db) == 0:
            # db not previously created
            self.log.info(f'Database {self.DB_NAME} not found, creating it')
            self._client.create_database(self.DB_NAME)
            # TODO: review defaults for retention policy
            self._client.create_retention_policy(name=self.DB_RETENTION_POLICY_NAME,
                                                 duration=self.DB_RETENTION_POLICY_PERIOD,
                                                 replication=self.DB_RETENTION_POLICY_REPLICATION,
                                                 database=self.DB_NAME,
                                                 default=True)
        else:
            self.log.info(f'Database {self.DB_NAME} already exists, no need to create it')

    def _learn_about_contracts(self):
        period_range = range(1, 365+1)
        token_counter = {day: self._moe.staking_agent.get_all_locked_tokens(day) for day in period_range}
        self.__snapshot['future_locked_tokens'] = token_counter

    def _learn_about_nodes(self):
        agent = self._moe.staking_agent

        block_time = agent.blockchain.client.w3.eth.getBlock('latest').timestamp  # precision in seconds
        current_period = agent.get_current_period()

        nodes_dict = self._moe.known_nodes.abridged_nodes_dict()
        self.log.info(f'Processing {len(nodes_dict)} nodes at '
                      f'{MayaDT(epoch=block_time)} | Period {current_period}')
        data = []
        for staker_address in nodes_dict:
            worker = agent.get_worker_from_staker(staker_address)

            stake = agent.owned_tokens(staker_address)
            staked_nu_tokens = float(NU.from_nunits(stake).to_tokens())
            locked_nu_tokens = float(NU.from_nunits(agent.get_locked_tokens(
                staker_address=staker_address)).to_tokens())

            economics = TokenEconomicsFactory.get_economics(registry=self._moe.registry)
            stakes = StakeList(checksum_address=staker_address, registry=self._moe.registry)
            stakes.refresh()

            # store dates as floats for comparison purposes
            start_date = datetime_at_period(stakes.initial_period,
                                            seconds_per_period=economics.seconds_per_period).datetime().timestamp()
            end_date = datetime_at_period(stakes.terminal_period,
                                          seconds_per_period=economics.seconds_per_period).datetime().timestamp()

            last_confirmed_period = agent.get_last_active_period(staker_address)

            # TODO: do we need to worry about how much information is in memory if number of nodes is
            #  large i.e. should I check for size of data and write within loop if too big
            data.append(self.DB_LINE_PROTOCOL.format(
                measurement=self.DB_MEASUREMENT,
                staker_address=staker_address,
                worker_address=worker,
                start_date=start_date,
                end_date=end_date,
                stake=staked_nu_tokens,
                locked_stake=locked_nu_tokens,
                current_period=current_period,
                last_confirmed_period=last_confirmed_period,
                timestamp=block_time
            ))

        if not self._client.write_points(data,
                                         database=self.DB_NAME,
                                         time_precision='s',
                                         batch_size=10000,
                                         protocol='line'):
            # TODO: what do we do here
            self.log.warn(f'Unable to write to database {self.DB_NAME} at '
                          f'{MayaDT(epoch=block_time)} | Period {current_period}')

    def _handle_errors(self, *args, **kwargs):
        failure = args[0]
        cleaned_traceback = failure.getTraceback().replace('{', '').replace('}', '')
        if self._restart_on_error:
            self.log.warn(f'Unhandled error: {cleaned_traceback}. Attempting to restart crawler')
            if not self._node_learning_task.running:
                self.start()
        else:
            self.log.critical(f'Unhandled error: {cleaned_traceback}')

    def start(self):
        """
        Start the crawler if not already running
        """
        if not self.is_running:
            self.log.info('Starting Moe Crawler')
            if self._client is None:
                self._client = InfluxDBClient(host='localhost', port=8086, database=self.DB_NAME)

            offset = 2  # seconds
            node_learner_deferred = self._node_learning_task.start(interval=self._refresh_rate, now=True)
            contract_learner_deferred = self._contract_learning_task.start(interval=self._refresh_rate - offset, now=True)

            node_learner_deferred.addErrback(self._handle_errors)
            contract_learner_deferred.addErrback(self._handle_errors)

    def stop(self):
        """
        Stop the crawler if currently running
        """
        if self.is_running:
            self.log.info('Stopping Moe Crawler')
            self._client.close()
            self._client = None
            self._node_learning_task.stop()
            self._contract_learning_task.stop()

    @property
    def is_running(self):
        """
        Returns True if currently running, False otherwise
        :return: True if currently running, False otherwise
        """
        return self._node_learning_task.running and self._contract_learning_task.running

    def get_db_client(self):
        return MoeCrawlerDBClient(host='localhost', port=8086, database=self.DB_NAME)


class MoeCrawlerDBClient:
    """
    Performs operations on data in the MoeBlockchainCrawler DB.

    Helpful for data intensive long-running graphing calculations on historical data.
    """
    def __init__(self, host, port, database):
        self._client = InfluxDBClient(host=host, port=port, database=database)

    def get_historical_locked_tokens_over_range(self, days: int):
        today = datetime.utcnow()
        range_end = datetime(year=today.year, month=today.month, day=today.day,
                             hour=0, minute=0, second=0, microsecond=0)
        range_begin = range_end - timedelta(days=days-1)
        results = list(self._client.query(f"SELECT SUM(locked_stake) "
                                          f"FROM ("
                                          f"SELECT staker_address, current_period, "
                                          f"LAST(locked_stake) "
                                          f"AS locked_stake "
                                          f"FROM moe_network_info "
                                          f"WHERE time >= '{MayaDT.from_datetime(range_begin).rfc3339()}' "
                                          f"AND "
                                          f"time < '{MayaDT.from_datetime(range_end + timedelta(days=1)).rfc3339()}' "
                                          f"GROUP BY staker_address, time(1d)"
                                          f") "
                                          f"GROUP BY time(1d)").get_points())

        # Note: all days may not have values eg. days before DB started getting populated
        # As time progresses this should be less of an issue
        locked_tokens_dict = OrderedDict()
        for r in results:
            locked_stake = r['sum']
            if locked_stake:
                # Dash accepts datetime objects for graphs
                locked_tokens_dict[MayaDT.from_rfc3339(r['time']).datetime()] = locked_stake

        return locked_tokens_dict

    def get_historical_num_stakers_over_range(self, days: int):
        today = datetime.utcnow()
        range_end = datetime(year=today.year, month=today.month, day=today.day,
                             hour=0, minute=0, second=0, microsecond=0)
        range_begin = range_end - timedelta(days=days - 1)
        results = list(self._client.query(f"SELECT COUNT(staker_address) FROM "
                                          f"("
                                            f"SELECT staker_address, LAST(locked_stake)"
                                            f"FROM moe_network_info WHERE "
                                            f"time >= '{MayaDT.from_datetime(range_begin).rfc3339()}' AND "
                                            f"time < '{MayaDT.from_datetime(range_end + timedelta(days=1)).rfc3339()}' "
                                            f"GROUP BY staker_address, time(1d)"
                                          f") "
                                          "GROUP BY time(1d)").get_points())   # 1 day measurements

        # Note: all days may not have values eg. days before DB started getting populated
        # As time progresses this should be less of an issue
        num_stakers_dict = OrderedDict()
        for r in results:
            locked_stake = r['count']
            if locked_stake:
                # Dash accepts datetime objects for graphs
                num_stakers_dict[MayaDT.from_rfc3339(r['time']).datetime()] = locked_stake

        return num_stakers_dict

    def close(self):
        self._client.close()