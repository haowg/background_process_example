#!/usr/bin/env python
# coding: utf8
"""同步比分到数据"""
import os
import sys
import time
import json
import signal
import logging
import datetime

import click
import requests
from setproctitle import setproctitle
from sqlalchemy import create_engine, text


API = "http://w.rt.sports.baofeng.com/api/server/v1/commit"
TOKEN = "8GE7NIv5c"


class ScoreSyncer(object):
    """syncer"""

    def __init__(self, database_uri, rt_score_api, token):
        """init"""
        self._cache = {}
        self._rt_score_api = rt_score_api
        self._token = token
        self._database_uri = database_uri
        self._engine = create_engine(database_uri, echo=False)
        self._clean_cache_flag = ""
        self._stopped = False
        self._setup_signals()

    def _setup_signals(self):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logging.info("%s got signal %s in %s", self, signum, frame)

        if signum in [signal.SIGINT, signal.SIGTERM]:
            self._stopped = True

    def get_yester_tomorrow_time(self):
        now_time = datetime.datetime.now()
        tormorrow = now_time + datetime.timedelta(days=1)
        yesterday = now_time - datetime.timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d"), tormorrow.strftime("%Y-%m-%d")

    def get_matches(self):
        """get"""
        logging.info("getting matches from database.....")
        yesterday_str, tormorrow_str = self.get_yester_tomorrow_time()
        with self._engine.begin() as connection:
            rows = connection.execute(
                text(
                    "SELECT m.id as match_id, m.status, mt.team1_id,"
                    "mt.team1_score, mt.team2_id, mt.team2_score "
                    "FROM `match` m inner join match_extra_team mt "
                    "on m.id = mt.match_id "
                    "WHERE m.start_tm>:yesterday AND m.start_tm<:tormorrow"),
                {"yesterday": yesterday_str,
                 "tormorrow": tormorrow_str}
            ).fetchall()

        if self._clean_cache_flag != tormorrow_str:
            self._clean_cache_flag = tormorrow_str
            self.clean_cache(rows)

            if not rows:
                logging.warning("%s: match on %s not found", self,
                                time.strftime("%Y-%m-%d"))
            for row in rows:
                match_id = row['match_id']
                cached_match = self._cache.get(match_id)
                if not cached_match:
                    self._cache[match_id] = row
                    yield row
                else:
                    if row['team1_score'] != cached_match['team1_score'] or\
                       row['team2_score'] != cached_match['team2_score'] or\
                       row['status'] != cached_match['status']:
                        self._cache[match_id] = row
                        yield row

    def clean_cache(self, rows):
        """clean caches"""
        match_ids = {match['match_id'] for match in rows}
        cached_match_ids = self._cache.keys()
        for key in cached_match_ids:
            if key not in match_ids:
                self._cache.pop(key)


    def pack_score_data(self, match_id, team1_id, team2_id, team1_score,
                        team2_score, status):
        """接口所需格式的数据"""
        return {
            "type": 4,
            "version": 1,
            "data": {
                "match": match_id,
                "status": status,
                "score": {
                    team1_id: team1_score,
                    team2_id: team2_score,
                }
            }
        }

    def submit_score(self, data, score_api, token):
        """提交比分数据到互动系统"""
        logging.info("submit" + json.dumps(data, indent=4))
        if not data:
            return
        response = requests.post(
            "{}?token={}".format(score_api, token), json=data
        )
        res_data = json.loads(response.content)
        if res_data.get("errno") != 10000:
            logging.error(response.content)

    def run_forever(self):
        """run forever"""
        while not self._stopped:
            for match in self.get_matches():
                if self._stopped:
                    return
                try:
                    self.submit_score(
                        self.pack_score_data(
                            match["match_id"],
                            match["team1_id"],
                            match["team2_id"],
                            match["team1_score"],
                            match["team2_score"],
                            match["status"],
                        ),
                        self._rt_score_api,
                        self._token
                    )
                except Exception as ex:
                    logging.error("%s: %s", type(ex), str(ex), exc_info=True)
            time.sleep(5)


def setup_logger(logfile, loglevel):
    """setup logger"""
    if logfile == "stdout":
        handler = logging.StreamHandler(sys.stdout)

    else:
        dirname = os.path.dirname(logfile)
        if not os.path.exists(dirname):
            os.makedirs(dirname, 0755)

        handler = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=40 * 1024 * 1024, backupCount=40)

    formatter = logging.Formatter("%(asctime)-12s %(levelname)-6s %(message)s")
    handler.setFormatter(formatter)

    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(loglevel.upper())


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--log-file", required=False, type=click.STRING, default="stdout",
              show_default=True, help="Where to output the logs")
@click.option("--log-level", required=False,
              type=click.Choice(["debug", "info", "warning", "error", "critical"]),
              default="info", show_default=True, help="Log level")
@click.option("--database-uri", required=False, type=click.STRING,
              default="mysql+pymysql://localhost/sports", show_default=True,
              help="Database URI for SQLAlchemy")
def runscoresyncer(log_file, log_level, database_uri):
    """run"""
    setup_logger(log_file, log_level)

    setproctitle("rtsports-scoresyncer")

    scoresyncer = ScoreSyncer(database_uri, API, TOKEN)
    scoresyncer.run_forever()


if __name__ == "__main__":
    runscoresyncer()
