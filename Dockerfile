# syntax=docker/dockerfile:1
FROM ubuntu:22.04

# install app dependencies
#COPY ./mongodb-org-server_8.0.4_amd64.deb  /
#COPY ./mongodb-mongosh_2.3.8_amd64.deb /
RUN apt-get update && apt-get install -y python3 python3-pip
RUN apt-get install gdebi -y
RUN apt-get install -y mosquitto

#RUN echo "2\n2\n11\n" | apt install -y ./mongodb-org-server_8.0.4_amd64.deb 
#RUN apt install -y ./mongodb-mongosh_2.3.8_amd64.deb 
RUN pip3 install pipenv 
RUN pip3 install paho-mqtt python-etcd pymongo datetime timestamp requests firebase-admin
RUN pip install  flask==3.0.*
RUN ln -sf /proc/1/fd/1 /var/log/test.log


# install app
COPY ./Database.py /
COPY ./DataBroker.py /
COPY ./NOAAPublisher.py /
COPY ./NOAAHistoricalFetcher.py /
COPY ./AlertPublisher.py /
#RUN mkdir -p /etc/mosquito
COPY ./mosquitto.conf /
COPY ./startup.sh /
COPY ./sensoriot-488101-firebase-adminsdk-fbsvc-76e763b0e2.json /firebase_service_account.json

# final configuration
#ENV FLASK_APP=hello
EXPOSE 1883 1884
#CMD ["pipenv" , "run", "python3" , "DataBroker.py" , "--db", "PROD"]
#CMD ["python3" , "DataBroker.py" , "--db", "PROD"]
CMD ["./startup.sh"]
