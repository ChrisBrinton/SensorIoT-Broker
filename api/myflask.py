from flask import Flask
from flask import request
from pymongo import MongoClient
import json
import datetime as dt
import string

print('connecting to mongo...')
client = MongoClient('localhost', 27017)  # make this explicit
db = client['gdtechdb_prod']
coll = db['Sensors']
app = Flask(__name__)


@app.route("/", methods=['GET'])
def hello():
    r = request
    print(r)
    dt.date.today()
    return 'hello ' + request.args.get('name', '')


@app.route("/stats", methods=['GET'])
def stats():
    ct = coll.count()
    return 'total rows:' + str(ct)


def today():
    now = dt.date.today()
    start = dt.datetime(now.year, now.month, now.day, 0, 0, 0, 0)
    day = dt.timedelta(days=1)
    end = start + day
    return start.timestamp(), end.timestamp()


@app.route("/sensor/<node>", methods=['GET'])
def people(node):
    skip = request.args.get('skip', '')
    try:
        skip = int(skip)
    except ValueError as err:
        print('invalid skip parameter %s. defaulting.' % skip)
        skip = 0
    docs = getdata(node, None, skip)
    return json.dumps(docs)


def getdata(node, date, skip):
    docs = []
    # node = int(node)
    start, end = today()
    qry = {'node_id': node, '$and': [{'time': {'$gte': start}}, {'time': {'$lte': end}}]}
    # qry = {'node_id': node}
    print('query is %s' % qry)
    cursor = coll.find(qry)
    ct = 0
    for doc in cursor:
        doc['_id'] = str(doc['_id']) # serialization support
        doc['value'] = float(doc['value'].replace('b', '').replace('v','').replace("'", ""))
        ct += 1
        # skip if needed
        if ct > skip:
            docs.append(doc)
            ct = 0

    # return number of documents and document list.
    docs.insert(0, docs.__len__())
    return docs


app.run(host="0.0.0.0", port=5000)

