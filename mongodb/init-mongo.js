// MongoDB initialization script for SensorIoT
// This script is run once when MongoDB starts with an empty data directory.
// It creates the database, collections, indexes, and schema validation.
//
// Usage (standalone): mongosh < init-mongo.js
// Usage (docker):     mounted into /docker-entrypoint-initdb.d/

db = db.getSiblingDB('gdtechdb_prod');

// ──────────────────────────────────────────────
// Collection: Sensors  (time-series sensor data)
// ──────────────────────────────────────────────
db.createCollection('Sensors', {
  validator: {
    $jsonSchema: {
      bsonType: 'object',
      required: ['model', 'gateway_id', 'node_id', 'type', 'value', 'time'],
      properties: {
        model:      { bsonType: 'string', description: 'Device model, e.g. GDESGW1' },
        gateway_id: { bsonType: 'string', description: 'Gateway hardware ID' },
        node_id:    { bsonType: 'string', description: 'Sensor node ID' },
        type:       { bsonType: 'string', description: 'Reading type (F=Fahrenheit, H=Humidity, V=Voltage, etc.)' },
        value:      { bsonType: 'string', description: 'Raw sensor value (may contain b/v suffix)' },
        time:       { bsonType: 'double', description: 'Unix timestamp (seconds since epoch)' }
      }
    }
  },
  validationLevel: 'moderate',
  validationAction: 'warn'
});

// Indexes for common query patterns
db.Sensors.createIndex({ gateway_id: 1, node_id: 1, type: 1, time: 1 }, { name: 'idx_gw_node_type_time' });
db.Sensors.createIndex({ node_id: 1, time: -1 },                        { name: 'idx_node_time_desc' });
db.Sensors.createIndex({ time: 1 },                                      { name: 'idx_time_asc' });

print('Created collection: Sensors');

// ──────────────────────────────────────────────
// Collection: SensorsLatest  (latest reading per node/type)
// ──────────────────────────────────────────────
db.createCollection('SensorsLatest', {
  validator: {
    $jsonSchema: {
      bsonType: 'object',
      required: ['model', 'gateway_id', 'node_id', 'type', 'value', 'time'],
      properties: {
        model:      { bsonType: 'string' },
        gateway_id: { bsonType: 'string' },
        node_id:    { bsonType: 'string' },
        type:       { bsonType: 'string' },
        value:      { bsonType: 'string' },
        time:       { bsonType: 'double' }
      }
    }
  },
  validationLevel: 'moderate',
  validationAction: 'warn'
});

// Unique compound index — one document per gateway+node+type
db.SensorsLatest.createIndex(
  { gateway_id: 1, node_id: 1, type: 1 },
  { name: 'idx_gw_node_type_unique', unique: true }
);
db.SensorsLatest.createIndex({ gateway_id: 1, time: 1 }, { name: 'idx_gw_time' });

print('Created collection: SensorsLatest');

// ──────────────────────────────────────────────
// Collection: Nicknames  (node friendly names)
// ──────────────────────────────────────────────
db.createCollection('Nicknames', {
  validator: {
    $jsonSchema: {
      bsonType: 'object',
      required: ['gateway_id', 'node_id'],
      properties: {
        gateway_id: { bsonType: 'string' },
        node_id:    { bsonType: 'string' },
        shortname:  { bsonType: 'string', description: 'Short display name' },
        longname:   { bsonType: 'string', description: 'Full descriptive name' },
        seq_no:     { bsonType: 'int',    description: 'Update sequence counter' }
      }
    }
  },
  validationLevel: 'moderate',
  validationAction: 'warn'
});

db.Nicknames.createIndex(
  { gateway_id: 1, node_id: 1 },
  { name: 'idx_gw_node_unique', unique: true }
);

print('Created collection: Nicknames');

// ──────────────────────────────────────────────
// Collection: GWNicknames  (gateway friendly names)
// ──────────────────────────────────────────────
db.createCollection('GWNicknames', {
  validator: {
    $jsonSchema: {
      bsonType: 'object',
      required: ['gateway_id'],
      properties: {
        gateway_id: { bsonType: 'string' },
        longname:   { bsonType: 'string', description: 'Gateway display name' },
        seq_no:     { bsonType: 'int',    description: 'Update sequence counter' }
      }
    }
  },
  validationLevel: 'moderate',
  validationAction: 'warn'
});

db.GWNicknames.createIndex(
  { gateway_id: 1 },
  { name: 'idx_gw_unique', unique: true }
);

print('Created collection: GWNicknames');

// ──────────────────────────────────────────────
// Create the test database with same structure
// ──────────────────────────────────────────────
db = db.getSiblingDB('gdtechdb_test');

db.createCollection('Sensors');
db.Sensors.createIndex({ gateway_id: 1, node_id: 1, type: 1, time: 1 }, { name: 'idx_gw_node_type_time' });
db.Sensors.createIndex({ node_id: 1, time: -1 },                        { name: 'idx_node_time_desc' });
db.Sensors.createIndex({ time: 1 },                                      { name: 'idx_time_asc' });

db.createCollection('SensorsLatest');
db.SensorsLatest.createIndex(
  { gateway_id: 1, node_id: 1, type: 1 },
  { name: 'idx_gw_node_type_unique', unique: true }
);
db.SensorsLatest.createIndex({ gateway_id: 1, time: 1 }, { name: 'idx_gw_time' });

db.createCollection('Nicknames');
db.Nicknames.createIndex({ gateway_id: 1, node_id: 1 }, { name: 'idx_gw_node_unique', unique: true });

db.createCollection('GWNicknames');
db.GWNicknames.createIndex({ gateway_id: 1 }, { name: 'idx_gw_unique', unique: true });

print('Created test database: gdtechdb_test');
print('MongoDB initialization complete.');
