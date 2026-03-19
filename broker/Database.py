import pymongo as mongodb

class Database:
    """description of class"""

    def __init__(self):

        self.mongoclient = mongodb.MongoClient("mongodb://localhost:27017/")
        self.db = self.mongoclient['gdtechdb-test']


    def Insert(self, topic, payload):
        elements = topic.split('/')
