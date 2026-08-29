#  This file is part of Headphones.
#
#  Headphones is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Headphones is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Headphones.  If not, see <http://www.gnu.org/licenses/>.

###################################
# Stolen from Sick-Beard's db.py  #
###################################



import time

import sqlite3

import os
import headphones
from headphones import logger


def dbFilename(filename="headphones.db"):
    return os.path.join(headphones.DATA_DIR, filename)


def getCacheSize():
    # this will protect against typecasting problems produced by empty string and None settings
    if not headphones.CONFIG.CACHE_SIZEMB:
        # sqlite will work with this (very slowly)
        return 0
    return int(headphones.CONFIG.CACHE_SIZEMB)


def checkpoint_wal():
    # In WAL mode the .db-wal file only ever gets folded back into the main
    # file on its own once it crosses SQLite's default auto-checkpoint size
    # (1000 pages, a few MB) -- under sustained write load (constant
    # searching/snatching/postprocessing) it can outrun that and grow to
    # multiple GB, which is exactly what made this instance's startup take
    # minutes and made ordinary requests time out. TRUNCATE checkpoints and
    # shrinks the WAL file back down; unlike VACUUM it doesn't rewrite the
    # whole database or hold an exclusive lock for more than the time it
    # takes to flush pending writes, so it's safe to run on a schedule.
    myDB = DBConnection()
    try:
        result = myDB.action("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, log_pages, checkpointed_pages = result[0], result[1], result[2]

        if busy:
            logger.debug(
                "WAL checkpoint deferred (database busy): %d of %d pages checkpointed",
                checkpointed_pages, log_pages
            )
        else:
            logger.info(
                "WAL checkpoint complete: %d pages folded back into the database", checkpointed_pages
            )
    except Exception as e:
        logger.warn("WAL checkpoint failed: %s", e)


def get_db_file_sizes():
    """Returns (main db size, wal file size) in bytes, either 0 if missing."""
    db_path = dbFilename()
    wal_path = db_path + "-wal"

    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

    return db_size, wal_size


class DBConnection:
    def __init__(self, filename="headphones.db"):

        self.filename = filename
        self.connection = sqlite3.connect(dbFilename(filename), timeout=20)
        # don't wait for the disk to finish writing
        self.connection.execute("PRAGMA synchronous = OFF")
        # default set to Write-Ahead Logging WAL
        self.connection.execute("PRAGMA journal_mode = %s" % headphones.CONFIG.JOURNAL_MODE)
        # 64mb of cache memory,probably need to make it user configurable
        self.connection.execute("PRAGMA cache_size=-%s" % (getCacheSize() * 1024))
        self.connection.row_factory = sqlite3.Row

    def action(self, query, args=None, upsert_insert_qry=None):

        if query is None:
            return

        sqlResult = None
        attempts = 0
        dberror = None

        while attempts < 10:
            try:
                with self.connection as c:

                    # log that previous attempt was locked and we're trying again
                    if dberror:
                        if args is None:
                            logger.debug('SQL: Database was previously locked, trying again. Attempt number %i. Query: %s', attempts + 1, query)
                        else:
                            logger.debug('SQL: Database was previously locked, trying again. Attempt number %i. Query: %s. Args: %s', attempts + 1, query, args)

                    # debugging
                    # try:
                    #     explain_query = 'EXPLAIN QUERY PLAN ' + query
                    #     if not args:
                    #         sql_results = c.execute(explain_query)
                    #     else:
                    #         sql_results = c.execute(explain_query, args)
                    #     if not args:
                    #         print(explain_query)
                    #     else:
                    #         print(explain_query + ' ' + str(args))
                    #     explain_results = sql_results
                    #     for row in explain_results:
                    #         print row
                    # except Exception as e:
                    #     print(e)

                    # Execute query

                    # time0 = time.time()

                    if args is None:
                        sqlResult = c.execute(query)
                        # logger.debug('SQL: ' + query)
                    else:
                        sqlResult = c.execute(query, args)
                        # logger.debug('SQL: %s. Args: %s', query, args)

                        # INSERT part of upsert query
                        if upsert_insert_qry:
                            sqlResult = c.execute(upsert_insert_qry, args)
                            # logger.debug('SQL: %s. Args: %s', upsert_insert_qry, args)

                    # debugging: loose test to log queries taking longer than 5 seconds
                    # seconds = time.time() - time0
                    # if seconds > 5:
                    #     if args is None:
                    #         logger.debug("SQL: Query ran for %s seconds: %s", seconds, query)
                    #     else:
                    #         logger.debug("SQL: Query ran for %s seconds: %s with args %s", seconds, query, args)

                    break

            except sqlite3.OperationalError as e:
                if "unable to open database file" in str(e) or "database is locked" in str(e):
                    dberror = e
                    if args is None:
                        logger.debug('Database error: %s. Query: %s', e, query)
                    else:
                        logger.debug('Database error: %s. Query: %s. Args: %s', e, query, args)
                    attempts += 1
                    time.sleep(1)
                else:
                    logger.error('Database error: %s', e)
                    raise
            except sqlite3.DatabaseError as e:
                logger.error('Fatal Error executing %s :: %s', query, e)
                raise

        # log if no results returned due to lock
        if not sqlResult and attempts:
            if args is None:
                logger.warn('SQL: Query failed due to database error: %s. Query: %s', dberror, query)
            else:
                logger.warn('SQL: Query failed due to database error: %s. Query: %s. Args: %s', dberror, query, args)

        return sqlResult

    def select(self, query, args=None):

        sqlResults = self.action(query, args).fetchall()

        if sqlResults is None or sqlResults == [None]:
            return []

        return sqlResults

    def upsert(self, tableName, valueDict, keyDict):
        """
        Transactions an Update or Insert to a table based on key.
        If the table is not updated then the 'WHERE changes' will be 0 and the table inserted
        """
        def genParams(myDict):
            return [x + " = ?" for x in list(myDict.keys())]

        update_query = "UPDATE " + tableName + " SET " + ", ".join(genParams(valueDict)) + " WHERE " + " AND ".join(genParams(keyDict))

        insert_query = ("INSERT INTO " + tableName + " (" + ", ".join(list(valueDict.keys()) + list(keyDict.keys())) + ")" + " SELECT " + ", ".join(
            ["?"] * len(list(valueDict.keys()) + list(keyDict.keys()))) + " WHERE changes()=0")

        try:
            self.action(update_query, list(valueDict.values()) + list(keyDict.values()), upsert_insert_qry=insert_query)
        except sqlite3.IntegrityError:
            logger.info('Queries failed: %s and %s', update_query, insert_query)
