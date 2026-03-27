"""
Tests for DataBroker.py MQTT → MongoDB logic.

DataBroker.py executes side-effectful code at import time (argparse, MongoDB
connection, MQTT connect, loop_forever).  We must:
  1. Override sys.argv before import so argparse does not fail.
  2. Patch pymongo.MongoClient so no real DB connection is attempted.
  3. Patch paho.mqtt.client.Client so connect() and loop_forever() are no-ops.

After import, on_message() is tested directly by constructing a fake MQTT
message object and asserting the expected MongoDB calls.

NOTE: The current DataBroker.py stores topic fields at offsets [1]–[4]:
  elements[1] → "model"      (always 'GDESGW1' for the /GDESGW1/… prefix)
  elements[2] → "gateway_id"
  elements[3] → "node_id"
  elements[4] → "type"
  elements[5] → (not stored – the value ends up in msg.payload)
These tests verify the *actual* behaviour so they serve as regression guards.
"""
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ── Patch everything before importing DataBroker ─────────────────────────────
sys.argv = ['DataBroker.py', '--db', 'TEST']

_mock_mongo_client = MagicMock()
_mock_mqtt_client = MagicMock()

with patch('pymongo.MongoClient', return_value=_mock_mongo_client), \
        patch('paho.mqtt.client.Client', return_value=_mock_mqtt_client):
    import DataBroker  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _msg(topic: str, payload: bytes = b'72.5') -> MagicMock:
    """Build a minimal mock MQTT message."""
    m = MagicMock()
    m.topic = topic
    m.payload = payload
    return m


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mock call history before each test."""
    _mock_mongo_client.reset_mock()
    yield


# ── on_connect ────────────────────────────────────────────────────────────────

class TestOnConnect:
    def test_subscribes_to_gdesgw1_wildcard(self):
        DataBroker.on_connect(_mock_mqtt_client, None, None, 0)
        _mock_mqtt_client.subscribe.assert_called_once_with('/GDESGW1/#')


# ── on_message – valid topics ─────────────────────────────────────────────────

class TestOnMessageValid:
    # Topic that produces exactly 6 elements when split on '/'
    TOPIC = '/GDESGW1/model1/gw-test/node-1/F'

    def test_inserts_into_sensors(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        DataBroker.db.Sensors.insert_one.assert_called_once()

    def test_upserts_into_sensors_latest(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        DataBroker.db.SensorsLatest.update_one.assert_called_once()

    def test_sensors_insert_contains_value_field(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC, b'98.6'))
        doc = DataBroker.db.Sensors.insert_one.call_args[0][0]
        assert doc['value'] == '98.6'

    def test_sensors_insert_has_float_time(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        doc = DataBroker.db.Sensors.insert_one.call_args[0][0]
        assert isinstance(doc['time'], float)

    def test_sensors_insert_model_from_element_1(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        doc = DataBroker.db.Sensors.insert_one.call_args[0][0]
        # elements[1] of '/GDESGW1/model1/gw-test/node-1/F' is 'GDESGW1'
        assert doc['model'] == 'GDESGW1'

    def test_sensors_insert_gateway_id_from_element_2(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        doc = DataBroker.db.Sensors.insert_one.call_args[0][0]
        assert doc['gateway_id'] == 'model1'

    def test_sensors_insert_node_id_from_element_3(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        doc = DataBroker.db.Sensors.insert_one.call_args[0][0]
        assert doc['node_id'] == 'gw-test'

    def test_sensors_insert_type_from_element_4(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        doc = DataBroker.db.Sensors.insert_one.call_args[0][0]
        assert doc['type'] == 'node-1'

    def test_sensors_latest_upsert_filter(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        filter_doc = DataBroker.db.SensorsLatest.update_one.call_args[0][0]
        assert filter_doc == {
            'gateway_id': 'model1',
            'node_id': 'gw-test',
            'type': 'node-1',
        }

    def test_sensors_latest_upsert_flag_is_true(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC))
        # update_one(filter, update, upsert=True) – third positional arg
        positional_args = DataBroker.db.SensorsLatest.update_one.call_args[0]
        assert positional_args[2] is True

    def test_sensors_latest_set_doc_has_value(self):
        DataBroker.on_message(None, None, _msg(self.TOPIC, b'55.0'))
        update_doc = DataBroker.db.SensorsLatest.update_one.call_args[0][1]
        assert update_doc['$set']['value'] == '55.0'


# ── on_message – invalid topics ───────────────────────────────────────────────

class TestOnMessageInvalid:
    def test_five_part_topic_does_not_insert(self):
        DataBroker.on_message(None, None, _msg('/GDESGW1/gw/node/F'))
        DataBroker.db.Sensors.insert_one.assert_not_called()
        DataBroker.db.SensorsLatest.update_one.assert_not_called()

    def test_seven_part_topic_does_not_insert(self):
        DataBroker.on_message(None, None, _msg('/GDESGW1/a/b/c/d/e/F'))
        DataBroker.db.Sensors.insert_one.assert_not_called()
        DataBroker.db.SensorsLatest.update_one.assert_not_called()

    def test_empty_topic_does_not_insert(self):
        DataBroker.on_message(None, None, _msg(''))
        DataBroker.db.Sensors.insert_one.assert_not_called()

    def test_no_leading_slash_five_parts_does_not_insert(self):
        DataBroker.on_message(None, None, _msg('GDESGW1/model/gw/node/F'))
        DataBroker.db.Sensors.insert_one.assert_not_called()
