#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright 2015 Kitware Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

# This file exposes endpoints to get taxi and other geoapp data.

import calendar
import cherrypy
import collections
import datetime
import dateutil.parser
import HTMLParser
import json
import pymongo
import re
import time
import threading
import urllib

import girder.api.rest
from girder import logger
from girder.api import access
from girder.constants import AccessType
from girder.api.describe import Description
from girder.api.rest import RestException

pgdb = None

GeoappUser = {
    'login': 'geoapp',
    'password': 'geoapp#1',
    'firstName': 'geoapp',
    'lastName': 'geoapp',
    'email': 'noemail@noemail.com',
    'admin': False
}


def tsqueryAddToList(itemList, addArray):
    """
    Add an array of values that should be added together to a list.

    :param itemList: list to append to if there are any values.
    :param andArray: array to combine with &.  If not empty, it is then added
                     to the specified list.
    """
    addval = '&'.join(addArray)
    if not len(addval):
        return
    itemList.append(tsqueryWrapVal(addval))


def tsqueryWrapVal(val):
    """
    Check if a value contains any special characters.  If it does, wrap it
    in parenthesis.

    :param val: value to possible wrap in parenthesis.
    :return: value that has been wrapped as needed.
    """
    if len(re.split('[&|!()]', val, 1)) > 1:
        return '(' + val + ')'
    return val


def tsqueryParse(parts, quotes={}, tsq=None, depth=0):
    """
    Given an array of strings where the elements of the array are either a
    single character with a special token of ( ) ! |, an empty string, or a
    string, produce a tsquery search string.

    :param parts: the array of strings to parse.
    :param quotes: a dictionary of quoted string.  A negated quoted string
                   won't be included in the tsquery to allow excluding phrases
                   with the same lexeme root as a desired lexeme root.
    :param tsq: an optional array of tsquery information.  If present, this
                must have a one-to-one correspondence with the parts array.
                Each entry is either None in indicate that that part has not
                been processed, or a tuple of ((partial tsquery string), (list
                of strings to potentially include exactly), (list of strings to
                potentially exclude exactly).
    :param depth: the depth of the parse tree.  Used for debugging.
    :return: tsquery string.
    :return: number of parts consumed by the parser.
    :return: list of strings to potentially include exactly.
    :return: list of strings to potentially exclude exactly.
    """
    reduced = 0
    if tsq is None:
        tsq = [None] * len(parts)
    include = []
    exclude = []
    while ('(' in parts and (')' not in parts or
                             parts.index('(') < parts.index(')'))):
        pos = parts.index('(')
        subtsq, consume, subinc, subexc = tsqueryParse(
            parts[pos + 1:], quotes, tsq[pos + 1:], depth + 1)
        reduced += len(parts)
        parts[pos:pos + consume + 1] = [None]
        tsq[pos:pos + consume + 1] = [(subtsq, subinc, subexc)]
        reduced -= len(parts)
    consume = len(parts)
    if (')' in parts and ('(' not in parts or
                          parts.index(')') < parts.index('('))):
        parts = parts[:parts.index(')')]
        tsq = tsq[:len(parts)]
        consume = len(parts) + 1
    orlist = []
    curtsq = []
    negate = False
    for pos in xrange(len(parts)):
        if parts[pos] == '|':
            tsqueryAddToList(orlist, curtsq)
            curtsq = []
            negate = False
        elif parts[pos] == '!':
            if pos + 1 < len(parts):
                negate = not negate
        elif tsq[pos] or parts[pos].strip():
            if not tsq[pos]:
                addval = '&'.join(parts[pos].strip().split())
                if negate:
                    exclude.extend(parts[pos].strip().split())
                else:
                    include.extend(parts[pos].strip().split())
            else:
                addval = tsq[pos][0]
                include.extend(tsq[pos][2 if negate else 1])
                exclude.extend(tsq[pos][1 if negate else 2])
            if not negate or addval not in quotes:
                curtsq.append(('!' if negate else '') + tsqueryWrapVal(addval))
            negate = False
    tsqueryAddToList(orlist, curtsq)
    if len(orlist) > 1:
        curtsq = '|'.join(orlist)
        include[:] = []
        exclude[:] = []
    else:
        curtsq = orlist[0]
    return curtsq, consume + reduced, include, exclude


def tsqueryExact(sql, phrases, quotes, field):
    """
    Given a list of phrases, add to an sql query to do a case insensitive
    match if the phrase is either quoted or a hashtag.

    :param sql: an array to append partial sql clauses to.  Modified.
    :param phrase: a list of phrases to consider adding.  These are either
                   keys in the quotes dictionary, in which case they are
                   included, or plain strings, in which case they are only
                   included if they start with #.
    :param quotes: a dictionary of quotes.
    :param field: name of the field to query.
    """
    for phrase in set(phrases):
        if phrase in quotes:
            escval = re.escape(quotes[phrase])
            if hasattr(pgdb, 'extensions'):
                escval = pgdb.extensions.adapt(escval).getquoted()[1:-1]
            else:
                escval = pgdb.escape_string(escval).strip('\'')
            sql.append(' AND ' + field + ' ~* E\'' + escval + '\'')
        elif phrase.startswith('#') and len(phrase) > 1:
            escval = re.escape(phrase)
            if hasattr(pgdb, 'extensions'):
                escval = pgdb.extensions.adapt(escval).getquoted()[1:-1]
            else:
                escval = pgdb.escape_string(escval).strip('\'')
            sql.append(' AND ' + field + ' ~* E\'(^|[^\\w#])' + escval +
                       '($|[^\\w#])\'')


def tsquerySearch(field, query):
    """
    Convert a string query into a Postgres tsquery.  Quoted sections require
    an exact case-insensitive match, as do #(hashtag) phrases.  All words are
    required.  There is limited support for grouping with ( ), exclusion with
    - or !, logical or of phrases with |, and using & or + for and (the same
    as with a space).  Currently, negating something that contains a quoted
    phrase or hashtag probably won't work.

    :param field: name of the field to query.
    :param query: the original text string.
    :returns: a sql where clause with '%s' whereever a string that needs to be
              escaped is located.
    :returns: an array of strings that are needed for the sql where clause.
    """
    sql = ['to_tsvector(\'english\', %s) @@ to_tsquery(\'english\', ' % field]
    sqlval = []
    sql.append('%s')

    quotedparts = query.replace('\x01', ' ').strip().split('"')
    quotes = {}
    for pos in xrange(1, len(quotedparts), 2):
        if len(quotedparts[pos]):
            quotekey = '\x01' + unichr(len(quotes) + 256)
            quotes[quotekey] = quotedparts[pos]
            quotedparts[pos] = quotekey
    processedQuery = ''.join(quotedparts)
    processedQuery = processedQuery.replace('+', ' ').replace(
        '&', ' ').replace(':', ' ').replace('-', '!').strip()
    parts = [part.strip() for part in re.split('([|()!\- ])', processedQuery)]
    tsq, _, include, exclude = tsqueryParse(parts, quotes)
    for key in quotes:
        tsq = tsq.replace(key, tsqueryWrapVal('&'.join((' '.join(
            re.split('- !()|&+:', quotes[key])).strip()).split())))
    sqlval.append(tsq)
    sql.append(')')
    if len(include):
        tsqueryExact(sql, include, quotes, field)
    if len(exclude):
        subsql = []
        tsqueryExact(subsql, exclude, quotes, field)
        if len(subsql):
            sql.extend([' AND NOT (true' + subsqlval + ')' for subsqlval in
                        subsql])
    return ''.join(sql), sqlval


class ViaPostgres():

    epoch = datetime.datetime.utcfromtimestamp(0)

    def __init__(self, db=None, **params):
        self.dbname = db
        self.dbparams = params.copy()
        self.dbLock = threading.RLock()
        self.db = None
        self.dbLastUsed = 0
        if db is not None:
            self.dbparams['database'] = db
        if not self.dbparams['database'] and not self.dbparams['dsn']:
            self.dbparams['dsn'] = 'parakon:taxi12r:taxi:taxi#1'
        self.useMilliseconds = False
        self.alwaysUseIdSort = True
        self.defaultSort = [('_id', 1)]
        self.maxId = None
        self.dbIdleTime = 300
        self.closeThread = threading.Thread(target=self.closeWhenIdle)
        self.closeThread.daemon = True
        self.closeThread.start()

    def adjustReturnFields(self, fields):
        """
        If the database is using seconds (not milliseconds) for dates, convert
        the output dates into milliseconds, since we expect the results to be
        used by javascript.

        :param fields: the table keys used to query Postgres.
        :return fields: the converted keys, as necessary.
        """
        if self.useMilliseconds is True:
            return fields
        newfields = []
        for field in fields:
            if (field in self.fieldTable and
                    self.fieldTable[field][0] == 'date'):
                if self.useMilliseconds:
                    newfields.append(field + ' + %d::bigint' % (
                        self.useMilliseconds * 1000))
                else:
                    newfields.append(field + ' * 1000::bigint')
            else:
                newfields.append(field)
        return newfields

    def connect(self, alwaysNew=False):
        """
        Connect to the database.

        :param alwaysNew: if False, reuse the last connection.  If True,
                          close the last connection and open a new one.  If
                          'fresh', create a new connection that the caller is
                          responsible for closing.
        :return: a database object.
        """
        global pgdb
        if not pgdb:
            # We can use either psycopg2 or pgdb.  Provided one only uses %s
            # formatting, the interface is equiavlent.  psycopg2 seems to
            # start slower than pgdb.  psycopg2 converts data to native python
            # formats substantially faster, though.  If we were to use a custom
            # results-to-json format, then I don't know which would be faster.
            # Import either library as pgdb, and that library will be used.
            # import pgdb
            import psycopg2 as pgdb
        if alwaysNew == 'fresh':
            return pgdb.connect(**self.dbparams)
        if getattr(self, 'db', None) and alwaysNew:
            try:
                self.db.close()
            except Exception:
                pass
            self.db = None
        with self.dbLock:
            db = self.db
            if db is None:
                self.db = db = pgdb.connect(**self.dbparams)
            self.dbLastUsed = time.time()
        return db

    def closeWhenIdle(self):
        """
        Periodically check if the database has been used.  If not, close the
        connection to free resources and allow easier management of the
        database while the application is running.
        """
        while True:
            with self.dbLock:
                if self.db and time.time() - self.dbLastUsed > self.dbIdleTime:
                    # The old db connection will close when no process is
                    # using it
                    self.db = None
            time.sleep(30)

    def checkMaxId(self):
        """
        Check the max ID for this table.  This can be reported with the results
        to aid in determining what percentage of the total data was retreived.
        """
        if self.maxId is None and self.queryBase == 'instagram':
            c = self.connect().cursor()
            try:
                c.execute('SELECT max(_id) FROM %s' % self.tableName)
                row = c.fetchone()
                self.maxId = int(row[0])
            except (pgdb.Error, ValueError):
                self.maxId = 0
            c.close()

    def find(self, params={}, limit=50, offset=0, sort=None, fields=None,
             queryBase=None, whereClauses=None):
        """
        Get data from a postgres database.

        :param params: a dictionary of query restrictions.  See the field
                       table(s).  For values that aren't of type 'text' or
                       'search', we also support (field)_min and (field)_max
                       parameters, which are inclusive and exclusive
                       respectively.  'search' adds a (field)_search parameter
                       which will perform a tsquery search.
        :param limit: default limit for the data.
        :param offset: default offset for the data.
        :param sort: a tuple of the form (key, direction).  Not currently
                     supported.
        :param fields: a list of fields to return, or None for all fields.
        :param queryBase: a string used to ensure we are using keys appropriate
                          to the asking query and to underlying database.
        :param whereClauses: a list of extra where clauses that are anded to
                             any other where clauses.
        :returns: a dictionary of results.
        """
        starttime = time.time()
        self.checkMaxId()
        if sort is None or self.alwaysUseIdSort:
            # shuffled order
            sort = self.defaultSort
        sql = ['SELECT']
        queryToDbKeys = dbToQueryKeys = {}
        if self.queryBase == 'instagram' and queryBase == 'message':
            queryToDbKeys, dbToQueryKeys = MsgToInstKeyTable, InstToMsgKeyTable
        if self.queryBase == 'message' and queryBase == 'instagram':
            queryToDbKeys, dbToQueryKeys = InstToMsgKeyTable, MsgToInstKeyTable
        if not fields:
            fields = [field[0] for field in self.fieldTable]
        fields = [field for field in fields if
                  queryToDbKeys.get(field, field) is not None]
        dbfields = [queryToDbKeys.get(field, field) for field in fields]
        if hasattr(self, 'adjustReturnFields'):
            sql.append(','.join(self.adjustReturnFields(dbfields)))
        else:
            sql.append(','.join(dbfields))
        sql.append('FROM %s WHERE true' % self.tableName)
        if whereClauses and len(whereClauses):
            sql.extend(['AND', ' AND '.join(whereClauses)])
        sqlval = []
        self.params_to_sql(params, sql, sqlval, dbToQueryKeys)

        self.findModifiers(sort, limit, offset, sql, queryToDbKeys)
        sql = ' '.join(sql)
        columns = {fields[col]: col for col in xrange(len(fields))}
        result = {
            'format': 'list',
            'fields': fields,
            'columns': columns
        }
        # TODO: If this fails, try to reconnect to the database
        try:
            c = self.connect().cursor()
            if self.queryBase == 'message':
                c.execute('SELECT max(_id) + 1 FROM %s' % self.tableName)
                row = c.fetchone()
                # We use this to guarantee that we don't get newer data than
                # what we first saw.
                result['nextId'] = row[0]
                sql = sql.replace(' WHERE true', ' WHERE _id <= %s' % str(
                    result['nextId']))
            if hasattr(c, 'mogrify'):
                logger.info('Query: %s', c.mogrify(sql, sqlval))
            else:
                logger.info('Query: %s', c._quoteparams(sql, sqlval))
            c.execute(sql, sqlval)
        except pgdb.Error as exc:
            logger.info('Database error %s', str(exc))
            c = self.connect(True).cursor()
            c.execute(sql, sqlval)
        logger.info('Query execution took %5.3fs', time.time() - starttime)
        result['data'] = c.fetchmany()
        if self.maxId:
            result['maxid'] = self.maxId
        logger.info('Fetching first items (%5.3fs including query execution)',
                    time.time() - starttime)
        while True:
            data = c.fetchmany()
            if data:
                result['data'].extend(data)
            else:
                break
        logger.info('Fetching data (%5.3fs including query execution)',
                    time.time() - starttime)
        c.close()
        return result

    def findModifiers(self, sort, limit, offset, sql, queryToDbKeys={}):
        """
        Add sort, limit, and offsets to the sql query.

        :param sort: the requested sort order.  This is a list of tuples,
                     where the first item of each tuple is a query key and the
                     seconds is -1 for descending or anything else for
                     ascending.
        :param limit: optional limit.
        :param offset: optional offset.
        :param sql: list of sql phrases.  Modified.
        :param queryToDbKeys: a map to convert query parameters to database
                              parameters.
        """
        if sort:
            sql.append('ORDER BY')
            sorts = []
            for sortval in sort:
                if queryToDbKeys.get(sortval[0], sortval[0]) is None:
                    continue
                if queryToDbKeys.get(sortval[0], None):
                    sortstr = queryToDbKeys[sortval[0]]
                else:
                    sortstr = '%s' % sortval[0]
                if sortval[1] == -1:
                    sortstr += ' DESC'
                sorts.append(sortstr)
            if len(sorts):
                sql.append(','.join(sorts))
            else:
                sql[-1:] = []
        if limit:
            sql.append('LIMIT %d' % limit)
        if offset:
            sql.append('OFFSET %d' % offset)

    def params_to_sql(self, params, sql, sqlval, altkeys={}):
        """
        Convert params to sql.

        :param params: a dictionary of query restrictions.
        :param sql: a list of sql statement fragments.  Modified.
        :param sqlval: a list of sql values to escape.  Modified.
        :param altkeys: a dictionary of alternate names for keys.  Each key is
                        a database key name, and the values are the query key
                        names.  This can be used to convert db parameters to
                        query parameters.
        """
        for field in self.fieldTable:
            for comp, suffix in [('=', ''), ('>=', '_min'), ('<', '_max'),
                                 ('search', '_search')]:
                if (altkeys.get(field, None) is not None and
                        altkeys[field] + suffix in params):
                    value = params[altkeys[field] + suffix]
                else:
                    if field + suffix not in params:
                        continue
                    value = params[field + suffix]
                dtype = self.fieldTable[field][0]
                if comp == 'search':
                    if dtype != 'search':
                        continue
                    if isinstance(value, (int, float, long)):
                        value = str(value)
                    subsql, subvalues = tsquerySearch(field, value)
                    sql.append('AND ' + subsql)
                    sqlval.extend(subvalues)
                elif dtype == 'date':
                    value = int((dateutil.parser.parse(value) - self.epoch)
                                .total_seconds())
                    if self.useMilliseconds is True:
                        value *= 1000
                    elif self.useMilliseconds:
                        value = (value - self.useMilliseconds) * 1000
                    sql.append('AND ' + field + comp + '%d' % value)
                elif dtype == 'int':
                    value = int(value)
                    sql.append('AND ' + field + comp + '%d' % value)
                elif dtype == 'float':
                    value = float(value)
                    sql.append('AND ' + field + comp + '%f' % value)
                else:
                    value = str(value)
                    sql.append('AND ' + field + comp + '%s')
                    sqlval.append(value)


# -------- TAXI specific classes and code --------

TaxiFieldTable = collections.OrderedDict([
    ('medallion', ('text', 'Taxi medallion')),
    ('hack_license', ('text', 'Hack license number')),
    ('vendor_id', ('text', 'Vendor ID')),
    ('store_and_fwd_flag', ('text', 'Store and forward flag')),
    ('payment_type', ('text', 'Payment type')),

    ('dropoff_datetime', ('date', 'Dropoff date')),
    ('dropoff_latitude', ('float', 'Dropoff latitude')),
    ('dropoff_longitude', ('float', 'Dropoff longitude')),
    ('passenger_count', ('int', 'Passenger count')),
    ('pickup_datetime', ('date', 'Pickup date')),
    ('pickup_latitude', ('float', 'Pickup latitude')),
    ('pickup_longitude', ('float', 'Pickup longitude')),
    ('rate_code', ('int', 'Rate code')),
    ('trip_distance', ('float', 'Trip distance (miles)')),
    ('trip_time_in_secs', ('int', 'Time time (seconds)')),

    ('fare_amount', ('float', 'Fare amount')),
    ('mta_tax', ('float', 'MTA tax')),
    ('surcharge', ('float', 'Surcharge')),
    ('tip_amount', ('float', 'Tip amount')),
    ('tolls_amount', ('float', 'Tolls')),
    ('total_amount', ('float', 'Total cost')),
])


class TaxiViaMongo():

    KeyTable = {
        'medallion': 'med',
        'hack_license': 'hack',
        'vendor_id': 'vid',
        'rate_code': 'code',
        'store_and_fwd_flag': 'fwd',
        'pickup_datetime': 'pdate',
        'dropoff_datetime': 'ddate',
        'passenger_count': 'count',
        'trip_time_in_secs': 'dur',
        'trip_distance': 'dist',
        'pickup_longitude': 'plon',
        'pickup_latitude': 'plat',
        'dropoff_longitude': 'dlon',
        'dropoff_latitude': 'dlat',
        'payment_type': 'type',
        'fare_amount': 'fare',
        'surcharge': 'sur',
        'mta_tax': 'tax',
        'tip_amount': 'tip',
        'tolls_amount': 'toll',
        'total_amount': 'total',
    }
    RevTable = {v: k for k, v in KeyTable.items()}

    def __init__(self, dbUri=None, **params):
        self.dbUri = dbUri
        db_connection = self.getDbConnection()
        self.database = db_connection.get_default_database()
        self.trips = self.database['trips']
        self.queryBase = 'taxi'

    def processParams(self, params, sort, fields):
        """
        :param params: a dictionary of query restrictions.  See the
                       TaxiFieldTable.  For values that aren't of type 'text',
                       we also support (field)_min and (field)_max parameters,
                       which are inclusive and exclusive respectively.
        :param sort: a list of tuples of the form (key, direction).
        :param fields: a list of fields to return, or None for all fields.
        """
        findParam = {}
        for field in TaxiFieldTable:
            if field in params:
                value = self.getParamValue(field, params[field])
                findParam[field] = value
            if field + '_min' in params:
                value = self.getParamValue(field, params[field + '_min'])
                findParam.setdefault(field, {})
                if isinstance(findParam[field], dict):
                    findParam[field]['$gte'] = value
            if field + '_max' in params:
                value = self.getParamValue(field, params[field + '_max'])
                findParam.setdefault(field, {})
                if isinstance(findParam[field], dict):
                    findParam[field]['$lt'] = value
        query = {}
        for key in findParam:
            query[self.KeyTable.get(key, key)] = findParam[key]
        if sort:
            sort = [(self.KeyTable.get(key, key), dir) for (key, dir) in sort]
        if fields:
            mfields = {self.KeyTable.get(key, key): 1 for key in fields}
            mfields['_id'] = 0
        return query, sort, mfields

    def find(self, params={}, limit=50, offset=0, sort=None, fields=None,
             queryBase=None, whereClauses=None):
        """
        Get data from the mongo database.  Return each row in turn as a python
        object with the default keys or the entire dataset as a list with
        metadata.

        :param params: a dictionary of query restrictions.  See the
                       TaxiFieldTable.  For values that aren't of type 'text',
                       we also support (field)_min and (field)_max parameters,
                       which are inclusive and exclusive respectively.
        :param limit: default limit for the data.
        :param offset: default offset for the data.
        :param sort: a list of tuples of the form (key, direction).
        :param fields: a list of fields to return, or None for all fields.
        :param queryBase: a string used to ensure we are using keys appropriate
                          to the asking query and to underlying database.
        :param whereClauses: a list of extra where clauses that are anded to
                             any other where clauses.
        :returns: a dictionary of results.
        """
        query, sort, fields = self.processParams(params, sort, fields)
        logger.info('Query %r', ((query, offset, limit, sort, fields), ))
        cursor = self.trips.find(spec=query, skip=offset, limit=limit,
                                 sort=sort, timeout=False, fields=fields)
        total = cursor.count()
        epoch = datetime.datetime.utcfromtimestamp(0)
        dt = datetime.datetime
        result = {'count': total, 'data': [{
            self.RevTable.get(k, k):
            v if not isinstance(v, dt) else int(
                (v - epoch).total_seconds() * 1000)
            for k, v in row.items() if k != '_id'}
            for row in cursor
        ]}
        return result

    def getDbConnection(self):
        """
        Connect to local mongo database named 'taxi' or to the specified
        database URI.

        :return client: a pymongo client.
        """
        clientOptions = {
            'connectTimeoutMS': 15000,
            # 'socketTimeoutMS': 60000,
        }
        dbUri = 'mongodb://parakon:27017/taxi'
        if self.dbUri:
            dbUri = self.dbUri
        # TODO: We should use the reconnect proxy
        return pymongo.MongoClient(dbUri, **clientOptions)

    def getParamValue(self, field, value):
        if value == '':
            return None
        dataType = TaxiFieldTable[field][0]
        if dataType == 'int':
            return int(value)
        if dataType == 'float':
            return float(value)
        if dataType == 'date':
            return dateutil.parser.parse(value)
        return value


class TaxiViaMongoCompact(TaxiViaMongo):

    KeyTable = {
        'medallion': 'm',
        'hack_license': 'h',
        'vendor_id': 'v',
        'rate_code': 'c',
        'store_and_fwd_flag': 'fw',
        'pickup_datetime': 'pd',
        'dropoff_datetime': 'dd',
        'passenger_count': 'p',
        'trip_time_in_secs': 's',
        'trip_distance': 'd',
        'pickup_longitude': 'px',
        'pickup_latitude': 'py',
        'dropoff_longitude': 'dx',
        'dropoff_latitude': 'dy',
        'payment_type': 'ty',
        'fare_amount': 'f',
        'surcharge': 'sr',
        'mta_tax': 'tx',
        'tip_amount': 'tp',
        'tolls_amount': 'tl',
        'total_amount': 't',
    }
    RevTable = {v: k for k, v in KeyTable.items()}

    epoch = datetime.datetime.utcfromtimestamp(0)

    def find(self, params={}, limit=50, offset=0, sort=None, fields=None,
             allowUnsorted=True, queryBase=None, whereClauses=None):
        """
        Get data from the mongo database.  Return each row in turn as a python
        object with the default keys or the entire dataset as a list with
        metadata.

        :param params: a dictionary of query restrictions.  See the
                       TaxiFieldTable.  For values that aren't of type 'text',
                       we also support (field)_min and (field)_max parameters,
                       which are inclusive and exclusive respectively.
        :param limit: default limit for the data.
        :param offset: default offset for the data.
        :param sort: a list of tuples of the form (key, direction).
        :param fields: a list of fields to return, or None for all fields.
        :param allowUnsorted: if true, and the entire data set will be returned
                              (rather than being restricted by limit), then
                              return the data unsorted.
        :param queryBase: a string used to ensure we are using keys appropriate
                          to the asking query and to underlying database.
        :param whereClauses: a list of extra where clauses that are anded to
                             any other where clauses.
        :returns: a dictionary of results.
        """
        query, sort, mfields = self.processParams(params, sort, fields)
        logger.info('Query %r', ((query, offset, limit, sort, mfields), ))
        cursor = None
        if not offset and sort is not None and allowUnsorted:
            cursor = self.trips.find(spec=query, skip=offset, limit=limit,
                                     sort=None, timeout=False, fields=mfields,
                                     manipulate=False, slave_okay=True,
                                     compile_re=False)
            total = cursor.count()
            if limit and total >= limit:
                cursor = None
        if not cursor:
            cursor = self.trips.find(spec=query, skip=offset, limit=limit,
                                     sort=sort, timeout=False, fields=mfields,
                                     manipulate=False, slave_okay=True,
                                     compile_re=False)
            total = cursor.count()
        if fields:
            columns = {fields[col]: col for col in xrange(len(fields))}
            mcol = [self.KeyTable.get(fields[col], fields[col])
                    for col in xrange(len(fields))]
            result = {
                'count': total,
                'format': 'list',
                'fields': fields,
                'columns': columns,
                'data': [[row[k] for k in mcol] for row in cursor]
            }
        else:
            result = {'count': total, 'data': [{
                self.RevTable.get(k, k): v for k, v in row.items()
                if k != '_id'}
                for row in cursor
            ]}
        return result

    def getParamValue(self, field, value):
        if value == '':
            return None
        dataType = TaxiFieldTable[field][0]
        if dataType == 'int':
            return int(value)
        if dataType == 'float':
            return float(value)
        if dataType == 'date':
            return int((dateutil.parser.parse(value) - self.epoch)
                       .total_seconds() * 1000)
        return value


class TaxiViaMongoRandomized(TaxiViaMongoCompact):
    def find(self, params={}, limit=50, offset=0, sort=None, fields=None,
             queryBase=None, whereClauses=None):
        if not sort:
            sort = [('_id', 1)]
        sort = [('_id', 1)]
        return TaxiViaMongoCompact.find(
            self, params, limit, offset, sort, fields, queryBase=queryBase,
            whereClauses=whereClauses)


class TaxiViaTangeloService():

    KeyTable = {
        'medallion': 'medallion_deanon',
        'hack_license': 'hack_license_deanon',
    }
    RevTable = {v: k for k, v in KeyTable.items()}

    def __init__(self, **params):
        self.url = 'http://damar.kitwarein.com:50000/taxi'
        self.queryBase = 'taxi'

    def find(self, params={}, limit=50, offset=0, sort=None, fields=None,
             queryBase=None, whereClauses=None):
        """
        Get data from the tangelo service.

        :param params: a dictionary of query restrictions.  See the
                       TaxiFieldTable.  For values that aren't of type 'text',
                       we also support (field)_min and (field)_max parameters,
                       which are inclusive and exclusive respectively.
        :param limit: default limit for the data.
        :param offset: default offset for the data.
        :param sort: a tuple of the form (key, direction).  Not currently
                     supported.
        :param fields: a list of fields to return, or None for all fields.
        :param queryBase: a string used to ensure we are using keys appropriate
                          to the asking query and to underlying database.
        :param whereClauses: a list of extra where clauses that are anded to
                             any other where clauses.
        :returns: a dictionary of results.
        """
        data = {'headers': 'true', 'offset': offset, 'limit': limit}
        for field in TaxiFieldTable:
            if field in params:
                value = params[field]
                if TaxiFieldTable[field][0] == 'date':
                    value = value.replace(' ', '_')
                data[self.KeyTable.get(field, field)] = value
            if field + '_min' in params or field + '_max' in params:
                minvalue = params.get(field + '_min', '')
                maxvalue = params.get(field + '_max', '')
                if TaxiFieldTable[field][0] == 'date':
                    minvalue = minvalue.replace(' ', '_')
                    maxvalue = maxvalue.replace(' ', '_')
                data[self.KeyTable.get(field, field)] = '%s,%s' % (
                    minvalue, maxvalue)
        # Handle sort
        # sort = [(self.KeyTable.get(key, key), dir) for (key, dir) in sort]
        if fields:
            fields = [self.KeyTable.get(key, key) for key in fields]
            data['fields'] = ','.join(fields)
        url = self.url+'?'+urllib.urlencode(data)
        logger.info('Query %r', ((url, data, sort), ))
        results = json.loads(urllib.urlopen(url).read())
        fields = [self.RevTable.get(k, k) for k in results[0]]
        columns = {fields[col]: col for col in xrange(len(fields))}
        return {'format': 'list', 'data': results[1:], 'fields': fields,
                'columns': columns}


class TaxiViaPostgres(ViaPostgres):

    def __init__(self, db=None, **params):
        ViaPostgres.__init__(self, db, **params)
        self.useMilliseconds = True
        self.fieldTable = TaxiFieldTable
        self.tableName = 'trips'
        self.queryBase = 'taxi'


class TaxiViaPostgresSeconds(TaxiViaPostgres):
    # These databases have times in epoch seconds, not epoch milliseconds

    def __init__(self, db=None, **params):
        TaxiViaPostgres.__init__(self, db, **params)
        self.useMilliseconds = False


# -------- Instagram classes and code --------

InstagramFieldTable = collections.OrderedDict([
    ('user_name',     ('text',   'User name')),
    ('user_id_num',   ('int',    'User ID')),  # Some versions use text user_id
    ('posted_date',   ('date',   'Posted date')),
    ('url',           ('text',   'Message URL')),
    ('image_url',     ('text',   'Image URL')),
    ('caption',       ('search', 'Caption')),
    ('latitude',      ('float',  'Latitude')),
    ('longitude',     ('float',  'Longitude')),
    ('location_id',   ('text',   'Location ID')),
    ('location_name', ('text',   'Location')),
    ('comment_count', ('int',    'Comment count')),
    ('comments',      ('text',   'Comments')),
    ('like_count',    ('int',    'Like count')),
    ('likes',         ('text',   'Likes')),
    ('scraped_date',  ('date',   'Scraped date')),
])


class InstagramViaPostgres(ViaPostgres):

    def __init__(self, db=None, **params):
        ViaPostgres.__init__(self, db, **params)
        self.fieldTable = InstagramFieldTable
        self.tableName = 'instagram'
        self.alwaysUseIdSort = False
        self.queryBase = 'instagram'


# -------- Message classes and code --------

MessageFieldTable = collections.OrderedDict([
    ('msg_id',            ('text',   'Message ID')),
    ('user_id',           ('text',   'User ID')),
    ('user_name',         ('text',   'User name')),
    ('msg_date',          ('date',   'Message date')),
    ('msg_date_ms',       ('float',  'Message date')),
    ('url',               ('text',   'Message URL')),
    ('image_url',         ('text',   'Image URL')),
    ('msg',               ('search', 'Message')),
    ('latitude',          ('float',  'Latitude')),
    ('longitude',         ('float',  'Longitude')),
    ('location_id',       ('text',   'Location ID')),
    ('location_name',     ('text',   'Location')),
    ('reply_to_msg_id',   ('text',   'In Reply To Message ID')),
    ('reply_to_user_id',  ('text',   'In Reply To User ID')),
    ('utc_offset',        ('int',    'User UTC Offset')),
    ('rand1',             ('int',    'Random Index 1')),
    ('rand2',             ('int',    'Random Index 2')),
    ('last_msg_id',       ('text',   'Last Message ID')),
    ('last_msg_date',     ('date',   'Last Message date')),
    ('last_latitude',     ('float',  'Last Latitude')),
    ('last_longitude',    ('float',  'Last Longitude')),
    ('ingest_date',       ('date',   'Ingest Date')),
    ('_id',               ('bigint', 'Ingest Order')),
])

MsgToInstKeyTable = {
    'msg_id': None,
    'user_id': None,
    'msg_date': 'posted_date',
    'msg_date_ms': None,
    'msg': 'caption',
    'reply_to_msg_id': None,
    'reply_to_user_id': None,
    'utc_offset': None,
    'rand1': None,
    'rand2': None,
    'last_msg_id': None,
    'last_msg_date': None,
    'last_latitude': None,
    'last_longitude': None,
    'ingest_date': 'scraped_date',
}
InstToMsgKeyTable = {v: k for k, v in MsgToInstKeyTable.items()}


class RealTimeViaPostgres(ViaPostgres):
    def __init__(self, db=None, **params):
        ViaPostgres.__init__(self, db, **params)
        self.fieldTable = MessageFieldTable
        self.useMilliseconds = False
        self.tableName = 'messages'
        self.alwaysUseIdSort = False
        self.defaultSort = [('rand1', 1), ('rand2', 1)]
        self.decoder = HTMLParser.HTMLParser()
        self.queryBase = 'message'

    def ingest(self, db, c, data):
        """
        Injest an object from Twitter.

        :param db: database object.  Use for committing the chanegs.
        :param c: database cursor: Used for adding the data.
        :param data: a data dictionary as produced by Twitter.
        :return: True if the data was ingested, false otherwise.
        """
        if 'timestamp_ms' in data:
            date = int(data['timestamp_ms'])
        else:
            date = int(calendar.timegm(dateutil.parser.parse(
                data['created_at']).utctimetuple()) * 1000)
        item = {
            'msg_id': data['id_str'],
            'user_id': data['user']['id_str'],
            'user_name': data['user']['name'],
            'msg_date': int(date / 1000),
            'msg_date_ms': date,
            'url': 't/%s/%s' % (data['user']['id_str'], data['id_str']),
            'msg': self.decoder.unescape(data['text']),
            'utc_offset': data['user']['utc_offset'],
            'ingest_date': time.time()
        }
        if ('entities' in data and 'media' in data['entities'] and
                len(data['entities']['media']) > 0 and
                'media_url_https' in data['entities']['media'][0]):
            item['image_url'] = data['entities']['media'][0][
                'media_url_https']
        if ('coordinates' in data and data['coordinates'] and
                'coordinates' in data['coordinates'] and
                len(data['coordinates']['coordinates']) >= 2):
            item['latitude'] = data['coordinates']['coordinates'][1]
            item['longitude'] = data['coordinates']['coordinates'][0]
        if ('place' in data and data['place'] and 'id' in data['place'] and
                'name' in data['place']):
            item['location_id'] = data['place']['id']
            item['location_name'] = data['place']['name']
        else:
            # if we don't have a location id or coordinates, give up
            if 'latitude' not in item:
                return False
        if ('source' in data and 'Instagram' in data['source'] and
                'entities' in data and 'urls' in data['entities'] and
                len(data['entities']['urls']) >= 1 and
                'display_url' in data['entities']['urls'][0] and
                'instagram' in data['entities']['urls'][0]['display_url']):
            item['source'] = self.decoder.unescape(
                data['entities']['urls'][0]['display_url'])
        sql = ['INSERT INTO messages (']
        sqlkeys = []
        sqlvals = []
        sqldata = []
        for key in MessageFieldTable:
            if key in item and item[key] is not None:
                sqlkeys.append(key)
                dt = MessageFieldTable[key][0]
                if dt in ('date', 'int'):
                    sqlvals.append(str(int(item[key])))
                elif dt == 'float':
                    sqlvals.append(str(item[key]))
                else:
                    sqlvals.append('%s')
                    sqldata.append(item[key])
        sql.extend(','.join(sqlkeys))
        sql.append(') VALUES (')
        sql.extend(','.join(sqlvals))
        sql.append(')')
        c.execute(''.join(sql), tuple(sqldata))
        db.commit()
        return True


# -------- General classes and code --------

def findGeneralDescription(desc, sortKey, fieldTable, defaultDbKey):
    """
    Generate a description for a find endpoint that automatically adds all the
    fields from a field table.

    :param desc: the primary description of this endpoint.
    :param sortKey: the default sortKey for the query.
    :param fieldTable: an ordered dictionary with the fields that can be used.
    :param defaultDbKey: the default database source.
    :returns: the generated Description object.
    """
    description = (
        Description(desc)
        .param('source', 'Database source (default %s).' % defaultDbKey,
               required=False)
        .param('limit', 'Result set size limit (default=50).',
               required=False, dataType='int')
        .param('offset', 'Offset into result set (default=0).',
               required=False, dataType='int')
        .param('sort', 'Field to sort the user list by (default=%s)' % (
               sortKey, ), required=False)
        .param('sortdir', '1 for ascending, -1 for descending (default=1)',
               required=False, dataType='int')
        .param('fields', 'A comma-separated list of fields to return '
               '(default is all fields).', required=False)
        .param('format', 'The format to return the data (default is '
               'list).', required=False, enum=['list', 'dict'])
        .param('wait', 'Maximum duration in seconds to wait for data '
               '(default=0).', required=False, dataType='float',
               default=0)
        .param('poll', 'Minimum interval in seconds between checking for data '
               'when waiting (default=10).', required=False, dataType='float',
               default=10)
        .param('initwait', 'When waiting, initial delay in seconds before '
               'starting to poll for more data.  This is not counted as part '
               'of the wait duration (default=0).', required=False,
               dataType='float', default=0))
    for field in sorted(fieldTable):
        (fieldType, fieldDesc) = fieldTable[field]
        dataType = fieldType
        if fieldType == 'text' or fieldType == 'search':
            dataType = 'string'
        description.param(field, fieldDesc, required=False, dataType=dataType)
        if fieldType != 'text' and fieldType != 'search':
            description.param(
                field + '_min', 'Minimum value (inclusive) of ' + fieldDesc,
                required=False, dataType=dataType)
            description.param(
                field + '_max', 'Maximum value (exclusive) of ' + fieldDesc,
                required=False, dataType=dataType)
        if fieldType == 'search':
            description.param(
                field + '_search', 'tsquery search of ' + fieldDesc,
                required=False, dataType='string')
    return description


class GeoAppResource(girder.api.rest.Resource):
    """API endpoint for geoapp data including taxi data."""

    def __init__(self):
        self.resourceName = 'geoapp'
        self.route('POST', ('ingest', ), self.ingestMessages)
        self.route('GET', ('instagram', ), self.findInstagram)
        self.route('GET', ('message', ), self.findMessage)
        self.route('PUT', ('reporttest', ), self.storeTestResults)
        self.route('PUT', ('reporttest', ':id'), self.updateTestResults)
        self.route('GET', ('taxi', ), self.findTaxi)
        self.route('GET', ('tiles', 'blank', ':wc1', ':wc2', ':wc3'),
                   self.blankTiles)
        config = girder.utility.config.getConfig()
        for attrKey, confKey in [
            ('taxiAccess', 'taxidata'),
            ('instagramAccess', 'instagramdata')
        ]:
            accessDict = {}
            for key in config.get(confKey, {}):
                db = config[confKey][key]
                if not isinstance(db, dict) or 'class' not in db:
                    continue
                if db['class'] in globals():
                    accessDict[key] = (globals()[db['class']],
                                       db.get('params', {}))
            setattr(self, attrKey, accessDict)

    def findGeneral(self, params, sortKey, fieldTable, accessList,
                    defaultDbKey, queryBase=None, whereClauses=None):
        """
        Perform a database search for a general find endpoint.

        :param params: the parameters of the endpoint call.
        :param sortKey: the default sortKey for the query.
        :param fieldTable: an ordered dictionary with the fields that can be
                           used.
        :param accessList: a dictionary of access classes used to query
                           different databases.
        :param defaultDbKey: the default database source.  Used with the
                             accessList.
        :param queryBase: the name of the base query.  This can be use to
                          allow the same database to be used from multiple
                          query points.
        :param whereClauses: a list of extra where clauses that are anded to
                             any other where clauses.
        :returns: the database response.
        """
        limit, offset, sort = self.getPagingParameters(params, sortKey)
        if sort is None and sortKey:
            sort = sortKey
        fields = None
        if 'fields' in params:
            fields = params['fields'].replace(',', ' ').strip().split()
        if not fields or not len(fields):
            fields = fieldTable.keys()
        accessObj = accessList[params.get('source', defaultDbKey)]
        if isinstance(accessObj, tuple):
            accessObj = accessObj[0](**accessObj[1])
            accessList[params.get('source', defaultDbKey)] = accessObj
        wait = params.get('wait', None)
        wait = None if not wait or wait <= 0 else float(wait)
        poll = params.get('poll', None)
        poll = 10 if not poll or poll <= 0 else float(poll)
        initwait = params.get('initwait', None)
        initwait = None if not initwait or initwait <= 0 else float(initwait)

        def resultFunc():
            if wait and initwait:
                time.sleep(initwait)
                yield ' '
            starttime = time.time()
            while True:
                result = accessObj.find(
                    params, limit, offset, sort, fields, queryBase=queryBase,
                    whereClauses=whereClauses)
                result['datacount'] = len(result.get('data', []))
                if (not wait or result['datacount'] or
                        time.time() + poll > starttime + wait):
                    break
                # Keep alive that should have no ill-effect on the json output
                yield ' '
                time.sleep(poll)
                yield ' '
            result['limit'] = limit
            result['offset'] = offset
            result['sort'] = sort
            if params.get('format', 'list') == 'list':
                if result.get('format', '') != 'list':
                    result['fields'] = fields
                    result['columns'] = {fields[col]: col
                                         for col in xrange(len(fields))}
                    if 'data' in result:
                        result['data'] = [
                            [row.get(field, None) for field in fields]
                            for row in result['data']
                        ]
                    result['format'] = 'list'
            else:
                if result.get('format', '') == 'list':
                    if 'data' in result:
                        result['data'] = [{
                            result['fields'][col]: row[col] for col
                            in xrange(len(row))} for row in result['data']]
                    result['format'] = 'dict'
                    del result['columns']
            # We could let Girder convert the results into JSON, but it is
            # marginally faster to dump the JSON ourselves, since we can
            # exclude sorting and reduce whitespace
            # return result
            yield json.dumps(
                result, check_circular=False, separators=(',', ':'),
                sort_keys=False, default=str)

        cherrypy.response.headers['Content-Type'] = 'application/json'
        return resultFunc

    def getUserAndFolder(self):
        """
        Get the geoapp user and test results folder.  If the geoapp user,
        collection, or folder do not exist, create them.

        :return: the geoapp user.
        :return: the geoapp test results folder.
        """
        user = self.model('user').findOne({'login': GeoappUser['login']})
        # if we don't have our expected user, try to create it
        if user is None:
            user = self.model('user').createUser(**GeoappUser)
        coll = self.model('collection').findOne({'name': 'geoapp'})
        if coll is None:
            coll = self.model('collection').createCollection('geoapp', user,
                                                             public=True)
        folderName = 'Test Results'
        folder = self.model('folder').findOne({
            'name': folderName,
            'parentId': coll['_id'],
            'parentCollection': 'collection'})
        if not folder:
            folder = self.model('folder').createFolder(
                coll, folderName, parentType='collection', public=True,
                creator=user)
        return user, folder

    def getMetadataFromBody(self, addRequestInfo=True):
        """
        Extract metadata from the request body and validate the keys.

        :param addRequestInfo: if true, add information about this request to
                               the metadata under the key 'requestInfo'.
        :returns: metadata dictionary.
        """
        try:
            metadata = json.load(cherrypy.request.body)
        except ValueError:
            raise RestException('Invalid JSON passed in request body.')
        for k in metadata:
            if not len(k):
                raise RestException('Key names must be at least one character '
                                    'long.')
            if '.' in k or k[0] == '$':
                raise RestException(u'The key name {} must not contain a '
                                    'period or begin with a dollar sign.'
                                    .format(k))
        if addRequestInfo:
            base = cherrypy.request.base
            altbase = cherrypy.request.headers.get('X-Forwarded-Host', '')
            if altbase:
                base = '%s://%s' % (cherrypy.request.scheme, altbase)
            metadata['requestInfo'] = {
                'base': base,
                'remote_ip': cherrypy.request.remote.ip,
            }
        return metadata

    @access.public
    def findInstagram(self, params):
        return self.findGeneral(
            params, '_id', InstagramFieldTable, self.instagramAccess,
            'postgres', 'instagram')
    findInstagram.description = findGeneralDescription(
        'Get a set of instagram data.', '_id', InstagramFieldTable, 'postgres')

    @access.public
    def findMessage(self, params):
        where = []
        if not self.boolParam('nullgeo', params, default=False):
            where.append('latitude is not NULL')
        return self.findGeneral(
            params, [('rand1', 1), ('rand2', 1)], MessageFieldTable,
            self.instagramAccess, 'rtmsg', 'message', whereClauses=where)
    findMessage.description = (
        findGeneralDescription(
            'Get a set of message data.', 'rand1', MessageFieldTable, 'rtmsg')
        .param('nullgeo', 'Include messages without latitude and longitude '
               '(default=false).', required=False, dataType='boolean',
               default=False))

    @access.public
    def findTaxi(self, params):
        return self.findGeneral(
            params, 'pickup_datetime', TaxiFieldTable, self.taxiAccess,
            'mongo', 'taxi')
    findTaxi.description = findGeneralDescription(
        'Get a set of taxi data.', 'pickup_datetime', TaxiFieldTable, 'mongo')

    @access.public
    def ingestMessages(self, params):
        starttime = time.time()
        res = {'ingested': 0}
        defaultDbKey = 'rtmsg'
        accessList = self.instagramAccess
        accessObj = accessList[params.get('source', defaultDbKey)]
        if isinstance(accessObj, tuple):
            accessObj = accessObj[0](**accessObj[1])
            accessList[params.get('source', defaultDbKey)] = accessObj
        db = accessObj.connect('fresh')
        c = db.cursor()
        for line in cherrypy.request.body:
            try:
                data = json.loads(line.decode('utf8'))
                if accessObj.ingest(db, c, data):
                    res['ingested'] += 1
                else:
                    res['skipped'] = res.get('skipped', 0) + 1
            except ValueError:
                res['badjson'] = res.get('badjson', 0) + 1
        if res['ingested'] and cherrypy.response.status == 500:
            cherrypy.response.status = 200
        res['duration'] = time.time() - starttime
        if res['duration']:
            res['rate'] = res['ingested'] / res['duration']
        logger.info('Ingest %r', res)
        return res
    ingestMessages.description = (
        Description('Accept line-by-line json data for a Twitter or Instagram '
                    'feed.')
        .notes('This expects data to be as sent by the Twitter firehose '
               'protocol.  Anything else probably won\'t work.  Duplicates '
               'are culled, user trails are computed, and images are fetched '
               'using background tasks.')
        .param('body', 'The line-by-line json for the feed.', paramType='body')
        .param('source', 'Database source (default rtmsg).', required=False)
        .errorResponse('Invalid JSON passed in request body.'))

    @access.public
    def storeTestResults(self, params):
        user, folder = self.getUserAndFolder()
        name = params.get('name', 'Results')
        item = self.model('item').createItem(name, user, folder)
        metadata = self.getMetadataFromBody()
        return self.model('item').setMetadata(item, metadata)
    storeTestResults.description = (
        Description('Submit new test results.')
        .responseClass('Item')
        .param('body', 'A JSON object containing metadata with the test '
               'results.', paramType='body')
        .param('name', 'Name for the item.', required=False)
        .errorResponse('Invalid JSON passed in request body.')
        .errorResponse('Metadata key name was invalid.'))

    @access.public
    def updateTestResults(self, id, params):
        user, folder = self.getUserAndFolder()
        item = self.model('item').load(id=id, level=AccessType.WRITE,
                                       user=user)
        metadata = self.getMetadataFromBody()
        return self.model('item').setMetadata(item, metadata)
    updateTestResults.description = (
        Description('Update existing test results.')
        .responseClass('Item')
        .param('id', 'The ID of a test result item to update',
               paramType='path', required=False)
        .param('body', 'A JSON object containing metadata with the test '
               'results.', paramType='body')
        .errorResponse('ID was invalid.')
        .errorResponse('Invalid JSON passed in request body.')
        .errorResponse('Metadata key name was invalid.'))

    @access.public
    def blankTiles(self, wc1, wc2, wc3, params):
        def resultFunc():
            yield (
                '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00'
                '\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00'
                '\x00\x0bIDAT\x18Wc``\x00\x00\x00\x03\x00\x01h&Y\r\x00\x00'
                '\x00\x00IEND\xaeB`\x82')

        cherrypy.response.headers['Content-Type'] = 'image/png'
        return resultFunc
    blankTiles.description = (
        Description('Always send a transparent 1x1 pixel PNG.')
        .param('wc1', 'Ignored', paramType='path', required=True)
        .param('wc2', 'Ignored', paramType='path', required=True)
        .param('wc3', 'Ignored', paramType='path', required=True))


def load(info):
    """
    Attach our API to the appropriate spot.

    :param info: a dictionary of server settings, of which the apiRoot value
                 is used.
    """
    info['apiRoot'].geoapp = GeoAppResource()
