import sys, getopt
import pymongo
from pymongo import MongoClient
import json
import datetime as dt
from dateutil.tz import *

def main(argv):
    database = ''
    months = 0
    test = 'true'
    try:
        opts, args = getopt.getopt(argv, "hrd:m:",["db=","months=","remove"])
    except getopt.GetoptError:
        printhelp()
    for opt, arg in opts:
        if opt == '-h':
            printhelp()
        elif opt in ("-d", "--db"):
            database = arg
        elif opt in ("-m", "--months"):
            months = arg
        elif opt in ("-r", "--remove"):
            test = 'false'

    if database == '':
        print('Must specify database')
        printhelp()
        return
    if months == 0:
        print('Must specify months > 0')
        printhelp()
        return

    print('executing trimdb.py for DB', database, 'for', months, 'months')
    print('connecting to mongo...')
    client = MongoClient('localhost', 27017)  # make this explicit
    db = client[database]
    collection = db['Sensors']

    period = int(months)*30*24 #X months of 30 days of 24 hours - num of hours in 3 months
    starttime = dt.datetime.now(tzutc())
    returndocs = removenodedataolderthan(collection, period, test)
    endtime = dt.datetime.now(tzutc())
    print('executed in ', endtime - starttime)

def printhelp():
    print('trimdb.py -d <database> -m <months to trim> -r (execute remove)')
    print('trimdb.py --db=<database> --months=<months to trim> --remove')


def getstart(p):
    # p should be a number specifying the delta in hours.
    nowdatetime = dt.datetime.now(tzutc())
    if p is None:
        diff = dt.timedelta(hours=24)
    else:
        diff = dt.timedelta(hours=int(p))

    start = nowdatetime - diff
    return start.timestamp()

def removenodedataolderthan(collection, period, test):
    print('removenodedataolderthan starting for', period, 'hours. C extentions in use:', pymongo.has_c())
    empty_results = {'results': '0'}
    start = getstart(period)
    total_records = 0

    qry = {'time': {'$lte': start}}
    print('query is %s ' % qry)

    if test == 'true':
        print('starting TEST query at ', dt.datetime.now())
        cursor = collection.find(qry).batch_size(1000)
        print('cursor returned at ', dt.datetime.now()) #this takes .2 millis
        for row in cursor:
            total_records=total_records+1
        print('total_records is ', total_records, ' at ', dt.datetime.now()) 
        return total_records
    elif test == 'false':
        print('starting REMOVE query at ', dt.datetime.now())
        results = collection.remove(qry)
        print('REMOVE query results ', results)
        print('REMOVE query finished at ', dt.datetime.now())

if __name__ == "__main__":
    main(sys.argv[1:])
    
