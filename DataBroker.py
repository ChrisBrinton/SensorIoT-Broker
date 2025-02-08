import paho.mqtt.client as mqtt
import pymongo as mongodb
import Database
import datetime
import argparse

# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):
  print("connected with result code " + str(rc))
    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
  mqttclient.subscribe("/GDESGW1/#")

# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):
  if (args.log == True):
    #only log these messages in test
    print("Topic: " + msg.topic + "\nMessage: " + str(msg.payload))

  elements = msg.topic.split('/')
  
  if (len(elements) == 6):
      db.Sensors.insert_one(
          {
            "model":elements[1],
            "gateway_id":elements[2],
            "node_id":elements[3],
            "type":elements[4],
            "value": str(msg.payload),
            "time": datetime.datetime.timestamp(datetime.datetime.now())
          })
      db.SensorsLatest.update_one(
          { "gateway_id": elements[2], "node_id": elements[3], "type": elements[4]},
          { "$set": {
            "model":elements[1],
            "gateway_id":elements[2],
            "node_id":elements[3],
            "type":elements[4],
            "value": str(msg.payload),
            "time": datetime.datetime.timestamp(datetime.datetime.now())
            }
          },
          True)
      

parser = argparse.ArgumentParser(description='DataBroker - MQTT mongodb subscriber for SensorIoT')
parser.add_argument('--db', help='Database invocation will connect to (default = TEST)', choices=['PROD','TEST'], default='TEST')
parser.add_argument('--dbconn' , help='Database connect in form <host>:<port>' , default='host.docker.internal')
parser.add_argument('--host', help='mosquitto listening host' , default = '0.0.0.0')
parser.add_argument('--port', help='mosquitto listening port' , default = '1883')
parser.add_argument('--log', help='Log messages to stdout (does not log by default)', action='store_true')
args = parser.parse_args()
if (args.dbconn != '' ) :
  print(args.dbconn)
  mongoclient = mongodb.MongoClient("mongodb://" + args.dbconn + "/")
  db = mongoclient.gdtechdb_prod
elif (args.db == 'PROD'):
  print('PROD db selected')
  mongoclient = mongodb.MongoClient("mongodb://localhost:27017/")
  db = mongoclient.gdtechdb_prod
else:
  print('TEST db selected')
  mongoclient = mongodb.MongoClient("mongodb://localhost:27017/")
  db = mongoclient.gdtechdb_test

if (args.log == True):
  print('Message logging enabled')
else:
  print('Message logging disabled')


mqttclient = mqtt.Client()
mqttclient.on_connect = on_connect
mqttclient.on_message = on_message

#mqttclient.connect("74.208.249.154", 1883, 60)
host = "127.0.0.1"
port = "1883"
if ( args.host != "" ) :
    host = args.host
    port = args.port
mqttclient.connect(host, int(port), 60)
    # Blocking call that processes network traffic, dispatches callbacks and
    # handles reconnecting.
    # Other loop*() functions are available that give a threaded interface and a
    # manual interface.
mqttclient.loop_forever()
