# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer, reactor
from twisted.enterprise import adbapi

from synapse.storage._base import LoggingTransaction, SQLBaseStore
from synapse.storage.engines import create_engine

import argparse
import curses
import logging
import sys
import time
import traceback
import yaml


logger = logging.getLogger("port_from_sqlite_to_postgres")


BOOLEAN_COLUMNS = {
    "events": ["processed", "outlier"],
    "rooms": ["is_public"],
    "event_edges": ["is_state"],
    "presence_list": ["accepted"],
}


APPEND_ONLY_TABLES = [
    "event_content_hashes",
    "event_reference_hashes",
    "event_signatures",
    "event_edge_hashes",
    "events",
    "event_json",
    "state_events",
    "room_memberships",
    "feedback",
    "topics",
    "room_names",
    "rooms",
    "local_media_repository",
    "local_media_repository_thumbnails",
    "remote_media_cache",
    "remote_media_cache_thumbnails",
    "redactions",
    "event_edges",
    "event_auth",
    "received_transactions",
    "sent_transactions",
    "transaction_id_to_pdu",
    "users",
    "state_groups",
    "state_groups_state",
    "event_to_state_groups",
    "rejections",
]


end_error_exec_info = None


class Store(object):
    """This object is used to pull out some of the convenience API from the
    Storage layer.

    *All* database interactions should go through this object.
    """
    def __init__(self, db_pool, engine):
        self.db_pool = db_pool
        self.database_engine = engine

    _simple_insert_txn = SQLBaseStore.__dict__["_simple_insert_txn"]
    _simple_insert = SQLBaseStore.__dict__["_simple_insert"]

    _simple_select_onecol_txn = SQLBaseStore.__dict__["_simple_select_onecol_txn"]
    _simple_select_onecol = SQLBaseStore.__dict__["_simple_select_onecol"]
    _simple_select_one_onecol = SQLBaseStore.__dict__["_simple_select_one_onecol"]
    _simple_select_one_onecol_txn = SQLBaseStore.__dict__["_simple_select_one_onecol_txn"]

    _simple_update_one = SQLBaseStore.__dict__["_simple_update_one"]
    _simple_update_one_txn = SQLBaseStore.__dict__["_simple_update_one_txn"]

    _execute_and_decode = SQLBaseStore.__dict__["_execute_and_decode"]

    def runInteraction(self, desc, func, *args, **kwargs):
        def r(conn):
            try:
                i = 0
                N = 5
                while True:
                    try:
                        txn = conn.cursor()
                        return func(
                            LoggingTransaction(txn, desc, self.database_engine),
                            *args, **kwargs
                        )
                    except self.database_engine.module.DatabaseError as e:
                        if self.database_engine.is_deadlock(e):
                            logger.warn("[TXN DEADLOCK] {%s} %d/%d", desc, i, N)
                            if i < N:
                                i += 1
                                conn.rollback()
                                continue
                        raise
            except Exception as e:
                logger.debug("[TXN FAIL] {%s} %s", desc, e)
                raise

        return self.db_pool.runWithConnection(r)

    def execute(self, f, *args, **kwargs):
        return self.runInteraction(f.__name__, f, *args, **kwargs)

    def execute_sql(self, sql, *args):
        def r(txn):
            txn.execute(sql, args)
            return txn.fetchall()
        return self.runInteraction("execute_sql", r)

    def insert_many_txn(self, txn, table, headers, rows):
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            table,
            ", ".join(k for k in headers),
            ", ".join("%s" for _ in headers)
        )

        try:
            txn.executemany(sql, rows)
        except:
            logger.exception(
                "Failed to insert: %s",
                table,
            )
            raise


class Porter(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    @defer.inlineCallbacks
    def setup_table(self, table):
        if table in APPEND_ONLY_TABLES:
            # It's safe to just carry on inserting.
            next_chunk = yield self.postgres_store._simple_select_one_onecol(
                table="port_from_sqlite3",
                keyvalues={"table_name": table},
                retcol="rowid",
                allow_none=True,
            )

            total_to_port = None
            if next_chunk is None:
                if table == "sent_transactions":
                    next_chunk, already_ported, total_to_port = (
                        yield self._setup_sent_transactions()
                    )
                else:
                    yield self.postgres_store._simple_insert(
                        table="port_from_sqlite3",
                        values={"table_name": table, "rowid": 1}
                    )

                    next_chunk = 1
                    already_ported = 0

            if total_to_port is None:
                already_ported, total_to_port = yield self._get_total_count_to_port(
                    table, next_chunk
                )
        else:
            def delete_all(txn):
                txn.execute(
                    "DELETE FROM port_from_sqlite3 WHERE table_name = %s",
                    (table,)
                )
                txn.execute("TRUNCATE %s CASCADE" % (table,))

            yield self.postgres_store.execute(delete_all)

            yield self.postgres_store._simple_insert(
                table="port_from_sqlite3",
                values={"table_name": table, "rowid": 0}
            )

            next_chunk = 1

            already_ported, total_to_port = yield self._get_total_count_to_port(
                table, next_chunk
            )

        defer.returnValue((table, already_ported, total_to_port, next_chunk))

    @defer.inlineCallbacks
    def handle_table(self, table, postgres_size, table_size, next_chunk):
        if not table_size:
            return

        self.progress.add_table(table, postgres_size, table_size)

        select = (
            "SELECT rowid, * FROM %s WHERE rowid >= ? ORDER BY rowid LIMIT ?"
            % (table,)
        )

        while True:
            def r(txn):
                txn.execute(select, (next_chunk, self.batch_size,))
                rows = txn.fetchall()
                headers = [column[0] for column in txn.description]

                return headers, rows

            headers, rows = yield self.sqlite_store.runInteraction("select", r)

            if rows:
                next_chunk = rows[-1][0] + 1

                self._convert_rows(table, headers, rows)

                def insert(txn):
                    self.postgres_store.insert_many_txn(
                        txn, table, headers[1:], rows
                    )

                    self.postgres_store._simple_update_one_txn(
                        txn,
                        table="port_from_sqlite3",
                        keyvalues={"table_name": table},
                        updatevalues={"rowid": next_chunk},
                    )

                yield self.postgres_store.execute(insert)

                postgres_size += len(rows)

                self.progress.update(table, postgres_size)
            else:
                return

    def setup_db(self, db_config, database_engine):
        db_conn = database_engine.module.connect(
            **{
                k: v for k, v in db_config.get("args", {}).items()
                if not k.startswith("cp_")
            }
        )

        database_engine.prepare_database(db_conn)

        db_conn.commit()

    @defer.inlineCallbacks
    def run(self):
        try:
            sqlite_db_pool = adbapi.ConnectionPool(
                self.sqlite_config["name"],
                **self.sqlite_config["args"]
            )

            postgres_db_pool = adbapi.ConnectionPool(
                self.postgres_config["name"],
                **self.postgres_config["args"]
            )

            sqlite_engine = create_engine("sqlite3")
            postgres_engine = create_engine("psycopg2")

            self.sqlite_store = Store(sqlite_db_pool, sqlite_engine)
            self.postgres_store = Store(postgres_db_pool, postgres_engine)

            # Step 1. Set up databases.
            self.progress.set_state("Preparing SQLite3")
            self.setup_db(sqlite_config, sqlite_engine)

            self.progress.set_state("Preparing PostgreSQL")
            self.setup_db(postgres_config, postgres_engine)

            # Step 2. Get tables.
            self.progress.set_state("Fetching tables")
            sqlite_tables = yield self.sqlite_store._simple_select_onecol(
                table="sqlite_master",
                keyvalues={
                    "type": "table",
                },
                retcol="name",
            )

            postgres_tables = yield self.postgres_store._simple_select_onecol(
                table="information_schema.tables",
                keyvalues={
                    "table_schema": "public",
                },
                retcol="distinct table_name",
            )

            tables = set(sqlite_tables) & set(postgres_tables)

            self.progress.set_state("Creating tables")

            logger.info("Found %d tables", len(tables))

            def create_port_table(txn):
                txn.execute(
                    "CREATE TABLE port_from_sqlite3 ("
                    " table_name varchar(100) NOT NULL UNIQUE,"
                    " rowid bigint NOT NULL"
                    ")"
                )

            try:
                yield self.postgres_store.runInteraction(
                    "create_port_table", create_port_table
                )
            except Exception as e:
                logger.info("Failed to create port table: %s", e)

            self.progress.set_state("Setting up")

            # Set up tables.
            setup_res = yield defer.gatherResults(
                [
                    self.setup_table(table)
                    for table in tables
                    if table not in ["schema_version", "applied_schema_deltas"]
                    and not table.startswith("sqlite_")
                ],
                consumeErrors=True,
            )

            # Process tables.
            yield defer.gatherResults(
                [
                    self.handle_table(*res)
                    for res in setup_res
                ],
                consumeErrors=True,
            )

            self.progress.done()
        except:
            global end_error_exec_info
            end_error_exec_info = sys.exc_info()
            logger.exception("")
        finally:
            reactor.stop()

    def _convert_rows(self, table, headers, rows):
        bool_col_names = BOOLEAN_COLUMNS.get(table, [])

        bool_cols = [
            i for i, h in enumerate(headers) if h in bool_col_names
        ]

        def conv(j, col):
            if j in bool_cols:
                return bool(col)
            return col

        for i, row in enumerate(rows):
            rows[i] = tuple(
                self.postgres_store.database_engine.encode_parameter(
                    conv(j, col)
                )
                for j, col in enumerate(row)
                if j > 0
            )

    @defer.inlineCallbacks
    def _setup_sent_transactions(self):
        # Only save things from the last day
        yesterday = int(time.time()*1000) - 86400000

        # And save the max transaction id from each destination
        select = (
            "SELECT rowid, * FROM sent_transactions WHERE rowid IN ("
            "SELECT max(rowid) FROM sent_transactions"
            " GROUP BY destination"
            ")"
        )

        def r(txn):
            txn.execute(select)
            rows = txn.fetchall()
            headers = [column[0] for column in txn.description]

            ts_ind = headers.index('ts')

            return headers, [r for r in rows if r[ts_ind] < yesterday]

        headers, rows = yield self.sqlite_store.runInteraction(
            "select", r,
        )

        self._convert_rows("sent_transactions", headers, rows)

        inserted_rows = len(rows)
        max_inserted_rowid = max(r[0] for r in rows)

        def insert(txn):
            self.postgres_store.insert_many_txn(
                txn, "sent_transactions", headers[1:], rows
            )

        yield self.postgres_store.execute(insert)

        def get_start_id(txn):
            txn.execute(
                "SELECT rowid FROM sent_transactions WHERE ts >= ?"
                " ORDER BY rowid ASC LIMIT 1",
                (yesterday,)
            )

            rows = txn.fetchall()
            if rows:
                return rows[0][0]
            else:
                return 1

        next_chunk = yield self.sqlite_store.execute(get_start_id)
        next_chunk = max(max_inserted_rowid + 1, next_chunk)

        yield self.postgres_store._simple_insert(
            table="port_from_sqlite3",
            values={"table_name": "sent_transactions", "rowid": next_chunk}
        )

        def get_sent_table_size(txn):
            txn.execute(
                "SELECT count(*) FROM sent_transactions"
                " WHERE ts >= ?",
                (yesterday,)
            )
            size, = txn.fetchone()
            return int(size)

        remaining_count = yield self.sqlite_store.execute(
            get_sent_table_size
        )

        total_count = remaining_count + inserted_rows

        defer.returnValue((next_chunk, inserted_rows, total_count))

    @defer.inlineCallbacks
    def _get_remaining_count_to_port(self, table, next_chunk):
        rows = yield self.sqlite_store.execute_sql(
            "SELECT count(*) FROM %s WHERE rowid >= ?" % (table,),
            next_chunk,
        )

        defer.returnValue(rows[0][0])

    @defer.inlineCallbacks
    def _get_already_ported_count(self, table):
        rows = yield self.postgres_store.execute_sql(
            "SELECT count(*) FROM %s" % (table,),
        )

        defer.returnValue(rows[0][0])

    @defer.inlineCallbacks
    def _get_total_count_to_port(self, table, next_chunk):
        remaining, done = yield defer.gatherResults(
            [
                self._get_remaining_count_to_port(table, next_chunk),
                self._get_already_ported_count(table),
            ],
            consumeErrors=True,
        )

        remaining = int(remaining) if remaining else 0
        done = int(done) if done else 0

        defer.returnValue((done, remaining + done))


##############################################
###### The following is simply UI stuff ######
##############################################


class Progress(object):
    """Used to report progress of the port
    """
    def __init__(self):
        self.tables = {}

        self.start_time = int(time.time())

    def add_table(self, table, cur, size):
        self.tables[table] = {
            "start": cur,
            "num_done": cur,
            "total": size,
            "perc": int(cur * 100 / size),
        }

    def update(self, table, num_done):
        data = self.tables[table]
        data["num_done"] = num_done
        data["perc"] = int(num_done * 100 / data["total"])

    def done(self):
        pass


class CursesProgress(Progress):
    """Reports progress to a curses window
    """
    def __init__(self, stdscr):
        self.stdscr = stdscr

        curses.use_default_colors()
        curses.curs_set(0)

        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)

        self.last_update = 0

        self.finished = False

        self.total_processed = 0
        self.total_remaining = 0

        super(CursesProgress, self).__init__()

    def update(self, table, num_done):
        super(CursesProgress, self).update(table, num_done)

        self.total_processed = 0
        self.total_remaining = 0
        for table, data in self.tables.items():
            self.total_processed += data["num_done"] - data["start"]
            self.total_remaining += data["total"] - data["num_done"]

        self.render()

    def render(self, force=False):
        now = time.time()

        if not force and now - self.last_update < 0.2:
            # reactor.callLater(1, self.render)
            return

        self.stdscr.clear()

        rows, cols = self.stdscr.getmaxyx()

        duration = int(now) - int(self.start_time)

        minutes, seconds = divmod(duration, 60)
        duration_str = '%02dm %02ds' % (minutes, seconds,)

        if self.finished:
            status = "Time spent: %s (Done!)" % (duration_str,)
        else:

            if self.total_processed > 0:
                left = float(self.total_remaining) / self.total_processed

                est_remaining = (int(now) - self.start_time) * left
                est_remaining_str = '%02dm %02ds remaining' % divmod(est_remaining, 60)
            else:
                est_remaining_str = "Unknown"
            status = (
                "Time spent: %s (est. remaining: %s)"
                % (duration_str, est_remaining_str,)
            )

        self.stdscr.addstr(
            0, 0,
            status,
            curses.A_BOLD,
        )

        max_len = max([len(t) for t in self.tables.keys()])

        left_margin = 5
        middle_space = 1

        items = self.tables.items()
        items.sort(
            key=lambda i: (i[1]["perc"], i[0]),
        )

        for i, (table, data) in enumerate(items):
            if i + 2 >= rows:
                break

            perc = data["perc"]

            color = curses.color_pair(2) if perc == 100 else curses.color_pair(1)

            self.stdscr.addstr(
                i+2, left_margin + max_len - len(table),
                table,
                curses.A_BOLD | color,
            )

            size = 20

            progress = "[%s%s]" % (
                "#" * int(perc*size/100),
                " " * (size - int(perc*size/100)),
            )

            self.stdscr.addstr(
                i+2, left_margin + max_len + middle_space,
                "%s %3d%% (%d/%d)" % (progress, perc, data["num_done"], data["total"]),
            )

        if self.finished:
            self.stdscr.addstr(
                rows-1, 0,
                "Press any key to exit...",
            )

        self.stdscr.refresh()
        self.last_update = time.time()

    def done(self):
        self.finished = True
        self.render(True)
        self.stdscr.getch()

    def set_state(self, state):
        self.stdscr.clear()
        self.stdscr.addstr(
            0, 0,
            state + "...",
            curses.A_BOLD,
        )
        self.stdscr.refresh()


class TerminalProgress(Progress):
    """Just prints progress to the terminal
    """
    def update(self, table, num_done):
        super(TerminalProgress, self).update(table, num_done)

        data = self.tables[table]

        print "%s: %d%% (%d/%d)" % (
            table, data["perc"],
            data["num_done"], data["total"],
        )

    def set_state(self, state):
        print state + "..."


##############################################
##############################################


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="A script to port an existing synapse SQLite database to"
                    " a new PostgreSQL database."
    )
    parser.add_argument("-v", action='store_true')
    parser.add_argument(
        "--sqlite-database", required=True,
        help="The snapshot of the SQLite database file. This must not be"
             " currently used by a running synapse server"
    )
    parser.add_argument(
        "--postgres-config", type=argparse.FileType('r'), required=True,
        help="The database config file for the PostgreSQL database"
    )
    parser.add_argument(
        "--curses", action='store_true',
        help="display a curses based progress UI"
    )

    parser.add_argument(
        "--batch-size", type=int, default=1000,
        help="The number of rows to select from the SQLite table each"
             " iteration [default=1000]",
    )

    args = parser.parse_args()

    logging_config = {
        "level": logging.DEBUG if args.v else logging.INFO,
        "format": "%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s"
    }

    if args.curses:
        logging_config["filename"] = "port-synapse.log"

    logging.basicConfig(**logging_config)

    sqlite_config = {
        "name": "sqlite3",
        "args": {
            "database": args.sqlite_database,
            "cp_min": 1,
            "cp_max": 1,
            "check_same_thread": False,
        },
    }

    postgres_config = yaml.safe_load(args.postgres_config)

    if "name" not in postgres_config:
        sys.stderr.write("Malformed database config: no 'name'")
        sys.exit(2)
    if postgres_config["name"] != "psycopg2":
        sys.stderr.write("Database must use 'psycopg2' connector.")
        sys.exit(3)

    def start(stdscr=None):
        if stdscr:
            progress = CursesProgress(stdscr)
        else:
            progress = TerminalProgress()

        porter = Porter(
            sqlite_config=sqlite_config,
            postgres_config=postgres_config,
            progress=progress,
            batch_size=args.batch_size,
        )

        reactor.callWhenRunning(porter.run)

        reactor.run()

    if args.curses:
        curses.wrapper(start)
    else:
        start()

    if end_error_exec_info:
        exc_type, exc_value, exc_traceback = end_error_exec_info
        traceback.print_exception(exc_type, exc_value, exc_traceback)
