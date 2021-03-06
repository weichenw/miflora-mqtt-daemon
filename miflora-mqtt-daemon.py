#!/usr/bin/env python3

import ssl
import sys
import re
import json
import os.path
import argparse
import threading
from itertools import chain
from time import time, sleep, localtime, strftime
from collections import OrderedDict
from colorama import init as colorama_init
from colorama import Fore, Back, Style
from configparser import ConfigParser
from unidecode import unidecode
from miflora.miflora_poller import MiFloraPoller, MI_BATTERY, MI_CONDUCTIVITY, MI_LIGHT, MI_MOISTURE, MI_TEMPERATURE
from mithermometer.mithermometer_poller import MiThermometerPoller, MI_HUMIDITY
from btlewrap import BluepyBackend, BluetoothBackendException
from bluepy.btle import BTLEException
import paho.mqtt.client as mqtt
import sdnotify
from signal import signal, SIGPIPE, SIG_DFL

signal(SIGPIPE,SIG_DFL)

project_name = 'Xiaomi Mi Flora Plant Sensor MQTT Client/Daemon'
project_url = 'https://github.com/ThomDietrich/miflora-mqtt-daemon'

sensor_name_miflora = "Mi Flora"
sensor_type_miflora = "MiFlora"
sensor_name_mitempbt = "Mijia Bluetooth Temperature Smart Humidity"
sensor_type_mitempbt = "MiTempBt"

miflora_parameters = OrderedDict([
    (MI_LIGHT, dict(name="LightIntensity", name_pretty='Sunlight Intensity', typeformat='%d', unit='lux', device_class="illuminance")),
    (MI_TEMPERATURE, dict(name="AirTemperature", name_pretty='Air Temperature', typeformat='%.1f', unit='°C', device_class="temperature")),
    (MI_MOISTURE, dict(name="SoilMoisture", name_pretty='Soil Moisture', typeformat='%d', unit='%', device_class="humidity")),
    (MI_CONDUCTIVITY, dict(name="SoilConductivity", name_pretty='Soil Conductivity/Fertility', typeformat='%d', unit='µS/cm')),
    (MI_BATTERY, dict(name="Battery", name_pretty='Sensor Battery Level', typeformat='%d', unit='%', device_class="battery"))
])

mitempbt_parameters = OrderedDict([
    (MI_TEMPERATURE, dict(name="AirTemperature", name_pretty='Air Temperature', typeformat='%.1f', unit='°C', device_class="temperature")),
    (MI_HUMIDITY, dict(name="Humidity", name_pretty='Air Moisture', typeformat='%d', unit='%', device_class="humidity")),
    (MI_BATTERY, dict(name="Battery", name_pretty='Sensor Battery Level', typeformat='%d', unit='%', device_class="battery"))
])

if False:
    # will be caught by python 2.7 to be illegal syntax
    print('Sorry, this script requires a python3 runtime environment.', file=sys.stderr)

# Argparse
parser = argparse.ArgumentParser(description=project_name, epilog='For further details see: ' + project_url)
parser.add_argument('--gen-openhab', help='generate openHAB items based on configured sensors', action='store_true')
parser.add_argument('--config_dir', help='set directory where config.ini is located', default=sys.path[0])
parse_args = parser.parse_args()

# Intro
colorama_init()
print(Fore.GREEN + Style.BRIGHT)
print(project_name)
print('Source:', project_url)
print(Style.RESET_ALL)

# Systemd Service Notifications - https://github.com/bb4242/sdnotify
sd_notifier = sdnotify.SystemdNotifier()

# Logging function
def print_line(text, error = False, warning=False, sd_notify=False, console=True):
    timestamp = strftime('%Y-%m-%d %H:%M:%S', localtime())
    if console:
        if error:
            print(Fore.RED + Style.BRIGHT + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL, file=sys.stderr)
        elif warning:
            print(Fore.YELLOW + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL)
        else:
            print(Fore.GREEN + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL)
    timestamp_sd = strftime('%b %d %H:%M:%S', localtime())
    if sd_notify:
        sd_notifier.notify('STATUS={} - {}.'.format(timestamp_sd, unidecode(text)))

# convert device type to human-readable name
def sensor_type_to_name(sensor_type):
    return sensor_name_miflora if (sensor_type == sensor_type_miflora ) else sensor_name_mitempbt

# Identifier cleanup
def clean_identifier(name):
    clean = name.strip()
    for this, that in [[' ', '-'], ['ä', 'ae'], ['Ä', 'Ae'], ['ö', 'oe'], ['Ö', 'Oe'], ['ü', 'ue'], ['Ü', 'Ue'], ['ß', 'ss']]:
        clean = clean.replace(this, that)
    clean = unidecode(clean)
    return clean

# Eclipse Paho callbacks - http://www.eclipse.org/paho/clients/python/docs/#callbacks
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print_line('MQTT connection established', console=True, sd_notify=True)
        print()
    else:
        print_line('Connection error with result code {} - {}'.format(str(rc), mqtt.connack_string(rc)), error=True)
        #kill main thread
        os._exit(1)


def on_publish(client, userdata, mid):
    #print_line('Data successfully published.')
    pass


def sensors_to_openhab_items(sensor_type, sensors, sensor_params, reporting_mode):
    sensor_type_name = sensor_type_to_name(sensor_type)
    print_line('Generating openHAB items. Copy to your configuration and modify as needed...')
    items = list()
    items.append('// {}.items - Generated by miflora-mqtt-daemon.'.format(sensor_type.lower()))
    items.append('// Adapt to your needs! Things you probably want to modify:')
    items.append('//     Room group names, icons,')
    items.append('//     "gAll", "broker", "UnknownRoom"')
    items.append('')
    items.append('// {} specific groups'.format(sensor_type_name))
    items.append('Group g{} "All {} sensors and elements" (gAll)'.format(sensor_type, sensor_type_name))
    for param, param_properties in sensor_params.items():
        items.append('Group g{} "{} {} elements" (gAll, g{})'.format(param_properties['name'], sensor_type_name, param_properties['name_pretty'], sensor_type))
    if reporting_mode == 'mqtt-json':
        for [sensor_name, sensor] in sensors.items():
            location = sensor['location_clean'] if sensor['location_clean'] else 'UnknownRoom'
            items.append('\n// {} "{}" ({})'.format(sensor_type_name, sensor['name_pretty'], sensor['mac']))
            items.append('Group g{}{} "{} Sensor {}" (g{}, g{})'.format(location, sensor_name, sensor_type_name, sensor['name_pretty'], sensor_type, location))
            for [param, param_properties] in sensor_params.items():
                basic = 'Number {}_{}_{}'.format(location, sensor_name, param_properties['name'])
                label = '"{} {} {} [{} {}]"'.format(location, sensor['name_pretty'], param_properties['name_pretty'], param_properties['typeformat'], param_properties['unit'].replace('%', '%%'))
                details = '<text> (g{}{}, g{})'.format(location, sensor_name, param_properties['name'])
                channel = '{{mqtt="<[broker:{}/{}:state:JSONPATH($.{})]"}}'.format(base_topic, sensor_name, param)
                items.append(' '.join([basic, label, details, channel]))
        items.append('')
        print('\n'.join(items))
    #elif reporting_mode == 'mqtt-homie':
    else:
        raise IOError('Given reporting_mode not supported for the export to openHAB items')

# Init sensors from configuration files
def init_sensors(sensor_type, sensors):
    sensor_type_name = sensor_type_to_name(sensor_type)
    if  sensor_type == sensor_type_miflora:
        config_section = sensor_type_miflora
        mac_regexp = "C4:7C:8D:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}"
    elif sensor_type == sensor_type_mitempbt:
        config_section = sensor_type_mitempbt
        mac_regexp = "(4C:65:A8|58:2D:34):[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}"
    else:
        print_line('Unknown device type: {}'.format(sensor_type), error=True, sd_notify=True)
        sys.exit(1)

    for [name, mac] in config[config_section].items():
        if not re.match(mac_regexp, mac):
            print_line('The MAC address "{}" seems to be in the wrong format. Please check your configuration'.format(mac), error=True, sd_notify=True)
            sys.exit(1)

        if '@' in name:
            name_pretty, location_pretty = name.split('@')
        else:
            name_pretty, location_pretty = name, ''
        name_clean = clean_identifier(name_pretty)
        location_clean = clean_identifier(location_pretty)

        sensor = dict()
        print('Adding sensor to device list and testing connection ...')
        print('Name:          "{}"'.format(name_pretty))
        #print_line('Attempting initial connection to Mi Flora sensor "{}" ({})'.format(name_pretty, mac), console=False, sd_notify=True)

        if sensor_type == sensor_type_miflora:
            sensor_poller = MiFloraPoller(mac=mac, backend=BluepyBackend, cache_timeout=miflora_cache_timeout, retries=3, adapter=used_adapter)
        elif sensor_type == sensor_type_mitempbt:
            sensor_poller = MiThermometerPoller(mac=mac, backend=BluepyBackend, cache_timeout=mitempbt_cache_timeout, retries=3, adapter=used_adapter)

        sensor['poller'] = sensor_poller
        sensor['name_pretty'] = name_pretty
        sensor['mac'] = sensor_poller._mac
        sensor['refresh'] = miflora_sleep_period  if (sensor_type == sensor_type_miflora) else mitempbt_sleep_period
        sensor['location_clean'] = location_clean
        sensor['location_pretty'] = location_pretty
        sensor['stats'] = {"count": 0, "success": 0, "failure": 0}
        try:
            sensor_poller.fill_cache()
            sensor_poller.parameter_value(MI_BATTERY)
            sensor['firmware'] = sensor_poller.firmware_version()
        except (IOError, BluetoothBackendException, BTLEException, RuntimeError, BrokenPipeError):
            print_line('Initial connection to {} sensor "{}" ({}) failed.'.format(sensor_type_name, name_pretty, mac), error=True, sd_notify=True)
        else:
            print('Internal name: "{}"'.format(name_clean))
            print('Device name:   "{}"'.format(sensor_poller.name()))
            print('MAC address:   {}'.format(sensor_poller._mac))
            print('Firmware:      {}'.format(sensor_poller.firmware_version()))
            print_line('Initial connection to {} sensor "{}" ({}) successful'.format(sensor_type_name, name_pretty, mac), sd_notify=True)
        print()
        sensors[name_clean] = sensor

# Pool & publish information from sensors
def pool_sensors(sensor_type, sensors, parameters):
    sensor_type_name = sensor_type_to_name(sensor_type)
    for [sensor_name, sensor] in sensors.items():
        data = dict()
        attempts = 2
        sensor['poller']._cache = None
        sensor['poller']._last_read = None
        sensor['stats']['count'] = sensor['stats']['count'] + 1
        print_line('Retrieving data from {} sensor "{}" ...'.format(sensor_type_name, sensor['name_pretty']))
        while attempts != 0 and not sensor['poller']._cache:
            try:
                sensor['poller'].fill_cache()
                sensor['poller'].parameter_value(MI_BATTERY)
            except (IOError, BluetoothBackendException, BTLEException, RuntimeError, BrokenPipeError) as e:
                attempts = attempts - 1
                if attempts > 0:
                    print_line('Retrying ...', warning = True)
                    if len(str(e))>0:
                        print_line('\tDue to: {}'.format(e), error=True)
                sensor['poller']._cache = None
                sensor['poller']._last_read = None

        if not sensor['poller']._cache:
            sensor['stats']['failure'] = sensor['stats']['failure'] + 1
            print_line('Failed to retrieve data from {} sensor "{}" ({}), success rate: {:.0%}'.format(
                sensor_type_name, sensor['name_pretty'], sensor['mac'], sensor['stats']['success']/sensor['stats']['count']
                ), error = True, sd_notify = True)
            print()
            continue
        else:
            sensor['stats']['success'] = sensor['stats']['success'] + 1

        for param,_ in parameters.items():
            data[param] = sensor['poller'].parameter_value(param)
        print_line('Result: {}'.format(json.dumps(data)))

        if reporting_mode == 'mqtt-json':
            print_line('Publishing to MQTT topic "{}/{}"'.format(base_topic, sensor_name))
            mqtt_client.publish('{}/{}'.format(base_topic, sensor_name), json.dumps(data))
            sleep(0.5) # some slack for the publish roundtrip and callback function
        elif reporting_mode == 'thingsboard-json':
            print_line('Publishing to MQTT topic "{}" username "{}"'.format(base_topic, sensor_name))
            mqtt_client.username_pw_set(sensor_name)
            mqtt_client.reconnect()
            sleep(1.0)
            mqtt_client.publish('{}'.format(base_topic), json.dumps(data))
            sleep(0.5) # some slack for the publish roundtrip and callback function
        elif reporting_mode == 'homeassistant-mqtt':
            print_line('Publishing to MQTT topic "{}/sensor/{}/state"'.format(base_topic, sensor_name).lower())
            mqtt_client.publish('{}/sensor/{}/state'.format(base_topic, sensor_name).lower(), json.dumps(data), retain=True)
            sleep(0.5) # some slack for the publish roundtrip and callback function
        elif reporting_mode == 'mqtt-homie':
            print_line('Publishing data to MQTT base topic "{}/{}/{}"'.format(base_topic, device_id, sensor_name))
            for [param, value] in data.items():
                mqtt_client.publish('{}/{}/{}/{}'.format(base_topic, device_id, sensor_name, param), value, 1, retain=False)
            sleep(0.5) # some slack for the publish roundtrip and callback function
        elif reporting_mode == 'mqtt-smarthome':
            for [param, value] in data.items():
                print_line('Publishing data to MQTT topic "{}/status/{}/{}"'.format(base_topic, sensor_name, param))
                payload = dict()
                payload['val'] = value
                payload['ts'] = int(round(time() * 1000))
                mqtt_client.publish('{}/status/{}/{}'.format(base_topic, sensor_name, param), json.dumps(payload), retain=True)
            sleep(0.5)  # some slack for the publish roundtrip and callback function
        elif reporting_mode == 'wirenboard-mqtt':
            for [param, value] in data.items():
                print_line('Publishing data to MQTT topic "/devices/{}/controls/{}"'.format(sensor_name, param))
                mqtt_client.publish('/devices/{}/controls/{}'.format(sensor_name, param), value, retain=True)
            mqtt_client.publish('/devices/{}/controls/{}'.format(sensor_name, 'timestamp'), strftime('%Y-%m-%d %H:%M:%S', localtime()), retain=True)
            sleep(0.5)  # some slack for the publish roundtrip and callback function
        elif reporting_mode == 'json':
            data['timestamp'] = strftime('%Y-%m-%d %H:%M:%S', localtime())
            data['name'] = sensor_name
            data['name_pretty'] = sensor['name_pretty']
            data['mac'] = sensor['mac']
            data['firmware'] = sensor['firmware']
            print('Data for "{}": {}'.format(sensor_name, json.dumps(data)))
        else:
            raise NameError('Unexpected reporting_mode.')
        print()

class sensorPooler(threading.Thread):
   def __init__(self, sensor_type, sensors, sensor_parameters, sleep_period, hciLock):
      threading.Thread.__init__(self)
      self.sensor_type = sensor_type
      self.sensor_type_name = sensor_type_to_name(sensor_type)
      self.sensors = sensors
      self.sensor_parameters = sensor_parameters
      self.sleep_period = sleep_period
      self.hciLock = hciLock
      self.daemon = True
      self.start()

   def run(self):
        print_line('Worker for {} sensors started'.format(self.sensor_type_name), sd_notify=True)
        # Sensor data retrieving and publishing
        while True:
            with self.hciLock:
                pool_sensors(self.sensor_type, self.sensors, self.sensor_parameters)

            if daemon_enabled:
                print_line('Sleeping for {} ({} seconds) ...'.format(self.sensor_type_name, self.sleep_period))
                print()
                sleep(self.sleep_period)
            else:
                break

        print_line('Execution finished for {}'.format(self.sensor_type_name), sd_notify=True)
        print()

# Load configuration file
config_dir = parse_args.config_dir

config = ConfigParser(delimiters=('=', ))
config.optionxform = str
config.read([os.path.join(config_dir, 'config.ini.dist'), os.path.join(config_dir, 'config.ini')])

reporting_mode = config['General'].get('reporting_method', 'mqtt-json')
used_adapter = config['General'].get('adapter', 'hci0')
daemon_enabled = config['Daemon'].getboolean('enabled', True)

if reporting_mode == 'mqtt-homie':
    default_base_topic = 'homie'
elif reporting_mode == 'homeassistant-mqtt':
    default_base_topic = 'homeassistant'
elif reporting_mode == 'thingsboard-json':
    default_base_topic = 'v1/devices/me/telemetry'
elif reporting_mode == 'wirenboard-mqtt':
    default_base_topic = ''
else:
    default_base_topic = 'misensor'

base_topic = config['MQTT'].get('base_topic', default_base_topic).lower()
device_id = config['MQTT'].get('homie_device_id', 'miflora-mqtt-daemon').lower()
miflora_sleep_period = config['Daemon'].getint('period_miflora', 300)
miflora_cache_timeout = miflora_sleep_period - 1
mitempbt_sleep_period = config['Daemon'].getint('period_mitempbt', 60)
mitempbt_cache_timeout = mitempbt_sleep_period - 1

# Check configuration
if reporting_mode not in ['mqtt-json', 'mqtt-homie', 'json', 'mqtt-smarthome', 'homeassistant-mqtt', 'thingsboard-json', 'wirenboard-mqtt']:
    print_line('Configuration parameter reporting_mode set to an invalid value', error=True, sd_notify=True)
    sys.exit(1)
if not config[sensor_type_miflora] and not config[sensor_type_mitempbt]:
    print_line('No sensors found in configuration file "config.ini"', error=True, sd_notify=True)
    sys.exit(1)
if reporting_mode == 'wirenboard-mqtt' and base_topic:
    print_line('Parameter "base_topic" ignored for "reporting_method = wirenboard-mqtt"', warning=True, sd_notify=True)


print_line('Configuration accepted', console=False, sd_notify=True)

# MQTT connection
if reporting_mode in ['mqtt-json', 'mqtt-homie', 'mqtt-smarthome', 'homeassistant-mqtt', 'thingsboard-json', 'wirenboard-mqtt']:
    print_line('Connecting to MQTT broker ...')
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_publish = on_publish
    if reporting_mode == 'mqtt-json':
        mqtt_client.will_set('{}/$announce'.format(base_topic), payload='{}', retain=True)
    elif reporting_mode == 'mqtt-homie':
        mqtt_client.will_set('{}/{}/$online'.format(base_topic, device_id), payload='false', retain=True)
    elif reporting_mode == 'mqtt-smarthome':
        mqtt_client.will_set('{}/connected'.format(base_topic), payload='0', retain=True)

    if config['MQTT'].getboolean('tls', False):
        # According to the docs, setting PROTOCOL_SSLv23 "Selects the highest protocol version
        # that both the client and server support. Despite the name, this option can select
        # “TLS” protocols as well as “SSL”" - so this seems like a resonable default
        mqtt_client.tls_set(
            ca_certs=config['MQTT'].get('tls_ca_cert', None),
            keyfile=config['MQTT'].get('tls_keyfile', None),
            certfile=config['MQTT'].get('tls_certfile', None),
            tls_version=ssl.PROTOCOL_SSLv23
        )

    if config['MQTT'].get('username'):
        mqtt_client.username_pw_set(config['MQTT'].get('username'), config['MQTT'].get('password', None))
    try:
        mqtt_client.connect(config['MQTT'].get('hostname', 'localhost'),
                            port=config['MQTT'].getint('port', 1883),
                            keepalive=config['MQTT'].getint('keepalive', 60))
    except:
        print_line('MQTT connection error. Please check your settings in the configuration file "config.ini"', error=True, sd_notify=True)
        sys.exit(1)
    else:
        if reporting_mode == 'mqtt-smarthome':
            mqtt_client.publish('{}/connected'.format(base_topic), payload='1', retain=True)
        if reporting_mode != 'thingsboard-json':
            mqtt_client.loop_start()
            sleep(1.0) # some slack to establish the connection

sd_notifier.notify('READY=1')

# Initialize Mi sensors
mifloras = OrderedDict()
init_sensors(sensor_type_miflora, mifloras)
mitempbts = OrderedDict()
init_sensors(sensor_type_mitempbt, mitempbts)

# openHAB items generation
if parse_args.gen_openhab:
    sensors_to_openhab_items(sensor_type_miflora, mifloras, miflora_parameters, reporting_mode)
    sensors_to_openhab_items(sensor_type_mitempbt, mitempbts, mitempbt_parameters, reporting_mode)
    sys.exit(0)

# Discovery Announcement
if reporting_mode == 'mqtt-json':
    print_line('Announcing {}/{} devices to MQTT broker for auto-discovery ...'.format(sensor_name_miflora,sensor_name_mitempbt))
    sensors_info = dict()
    for [sensor_name, sensor] in chain(mifloras.items(),mitempbts.items()):
        sensor_info = {key: value for key, value in sensor.items() if key not in ['poller', 'stats']}
        sensor_info['topic'] = '{}/{}'.format(base_topic, sensor_name)
        sensors_info[sensor_name] = sensor_info
    mqtt_client.publish('{}/$announce'.format(base_topic), json.dumps(sensors_info), retain=True)
    sleep(0.5) # some slack for the publish roundtrip and callback function
    print()
elif reporting_mode == 'mqtt-homie':
    print_line('Announcing {}/{} devices to MQTT broker for auto-discovery ...'.format(sensor_name_miflora,sensor_name_mitempbt))
    mqtt_client.publish('{}/{}/$homie'.format(base_topic, device_id), '2.1.0-alpha', 1, True)
    mqtt_client.publish('{}/{}/$online'.format(base_topic, device_id), 'true', 1, True)
    mqtt_client.publish('{}/{}/$name'.format(base_topic, device_id), device_id, 1, True)
    #mqtt_client.publish('{}/{}/$fw/version'.format(base_topic, device_id), flora['firmware'], 1, True)

    nodes_list = ','.join([sensor_name for [sensor_name, sensor] in chain(mifloras.items(), mitempbts.items())])
    mqtt_client.publish('{}/{}/$nodes'.format(base_topic, device_id), nodes_list, 1, True)

    for [sensor_name, sensor] in mifloras.items():
        topic_path = '{}/{}/{}'.format(base_topic, device_id, sensor_name)
        mqtt_client.publish('{}/$name'.format(topic_path), sensor['name_pretty'], 1, True)
        mqtt_client.publish('{}/$type'.format(topic_path), 'miflora', 1, True)
        mqtt_client.publish('{}/$properties'.format(topic_path), 'battery,conductivity,light,moisture,temperature', 1, True)
        mqtt_client.publish('{}/battery/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/battery/$unit'.format(topic_path), 'percent', 1, True)
        mqtt_client.publish('{}/battery/$datatype'.format(topic_path), 'int', 1, True)
        mqtt_client.publish('{}/battery/$range'.format(topic_path), '0:100', 1, True)
        mqtt_client.publish('{}/conductivity/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/conductivity/$unit'.format(topic_path), 'µS/cm', 1, True)
        mqtt_client.publish('{}/conductivity/$datatype'.format(topic_path), 'int', 1, True)
        mqtt_client.publish('{}/conductivity/$range'.format(topic_path), '0:*', 1, True)
        mqtt_client.publish('{}/light/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/light/$unit'.format(topic_path), 'lux', 1, True)
        mqtt_client.publish('{}/light/$datatype'.format(topic_path), 'int', 1, True)
        mqtt_client.publish('{}/light/$range'.format(topic_path), '0:50000', 1, True)
        mqtt_client.publish('{}/moisture/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/moisture/$unit'.format(topic_path), 'percent', 1, True)
        mqtt_client.publish('{}/moisture/$datatype'.format(topic_path), 'int', 1, True)
        mqtt_client.publish('{}/moisture/$range'.format(topic_path), '0:100', 1, True)
        mqtt_client.publish('{}/temperature/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/temperature/$unit'.format(topic_path), '°C', 1, True)
        mqtt_client.publish('{}/temperature/$datatype'.format(topic_path), 'float', 1, True)
        mqtt_client.publish('{}/temperature/$range'.format(topic_path), '*', 1, True)

    for [sensor_name, sensor] in mitempbts.items():
        topic_path = '{}/{}/{}'.format(base_topic, device_id, sensor_name)
        mqtt_client.publish('{}/$name'.format(topic_path), sensor['name_pretty'], 1, True)
        mqtt_client.publish('{}/$type'.format(topic_path), 'mitempbt', 1, True)
        mqtt_client.publish('{}/$properties'.format(topic_path), 'battery,humidity,temperature', 1, True)
        mqtt_client.publish('{}/battery/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/battery/$unit'.format(topic_path), 'percent', 1, True)
        mqtt_client.publish('{}/battery/$datatype'.format(topic_path), 'int', 1, True)
        mqtt_client.publish('{}/battery/$range'.format(topic_path), '0:100', 1, True)
        mqtt_client.publish('{}/humidity/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/humidity/$unit'.format(topic_path), 'percent', 1, True)
        mqtt_client.publish('{}/humidity/$datatype'.format(topic_path), 'int', 1, True)
        mqtt_client.publish('{}/humidity/$range'.format(topic_path), '0:100', 1, True)
        mqtt_client.publish('{}/temperature/$settable'.format(topic_path), 'false', 1, True)
        mqtt_client.publish('{}/temperature/$unit'.format(topic_path), '°C', 1, True)
        mqtt_client.publish('{}/temperature/$datatype'.format(topic_path), 'float', 1, True)
        mqtt_client.publish('{}/temperature/$range'.format(topic_path), '*', 1, True)
    sleep(0.5) # some slack for the publish roundtrip and callback function
    print()
elif reporting_mode == 'homeassistant-mqtt':
    print_line('Announcing {}/{} devices to MQTT broker for auto-discovery ...'.format(sensor_name_miflora,sensor_name_mitempbt))
    for [flora_name, flora] in mifloras.items():
        topic_path = '{}/sensor/{}'.format(base_topic, flora_name)
        base_payload = {
            "state_topic": "{}/state".format(topic_path).lower()
        }
        for sensor, params in miflora_parameters.items():
            payload = dict(base_payload.items())
            payload['unit_of_measurement'] = params['unit']
            payload['value_template'] = "{{ value_json.%s }}" % (sensor, )
            payload['name'] = "{} {}".format(flora_name, sensor.title())
            if 'device_class' in params:
                payload['device_class'] = params['device_class']
            mqtt_client.publish('{}/{}_{}/config'.format(topic_path, flora_name, sensor).lower(), json.dumps(payload), 1, True)
    for [mitempbt_name, mitempbt] in mitempbts.items():
        topic_path = '{}/sensor/{}'.format(base_topic, mitempbt_name)
        base_payload = {
            "state_topic": "{}/state".format(topic_path).lower()
        }
        for sensor, params in mitempbt_parameters.items():
            payload = dict(base_payload.items())
            payload['unit_of_measurement'] = params['unit']
            payload['value_template'] = "{{ value_json.%s }}" % (sensor, )
            payload['name'] = "{} {}".format(mitempbt_name, sensor.title())
            payload['device'] = {
                    'identifiers' : ["MiTempBt{}".format(sensor['mac'].lower().replace(":", ""))],
                    'connections' : [["mac", sensor['mac'].lower()]],
                    'manufacturer' : 'Xiaomi',
                    'name' : mitempbt_name,
                    'model' : 'Mijia Temperature and Humidity Sensor (LYWSDCGQ/01ZM)',
                    'sw_version': sensor['firmware']
            }
            if 'device_class' in params:
                payload['device_class'] = params['device_class']
            mqtt_client.publish('{}/{}_{}/config'.format(topic_path, mitempbt_name, sensor).lower(), json.dumps(payload), 1, True)
elif reporting_mode == 'wirenboard-mqtt':
    print_line('Announcing {}/{} devices to MQTT broker for auto-discovery ...'.format(sensor_name_miflora, sensor_name_mitempbt))
    for [flora_name, flora] in mifloras.items():
        mqtt_client.publish('/devices/{}/meta/name'.format(flora_name), flora_name, 1, True)
        topic_path = '/devices/{}/controls'.format(flora_name)
        mqtt_client.publish('{}/battery/meta/type'.format(topic_path), 'value', 1, True)
        mqtt_client.publish('{}/battery/meta/units'.format(topic_path), '%', 1, True)
        mqtt_client.publish('{}/conductivity/meta/type'.format(topic_path), 'value', 1, True)
        mqtt_client.publish('{}/conductivity/meta/units'.format(topic_path), 'µS/cm', 1, True)
        mqtt_client.publish('{}/light/meta/type'.format(topic_path), 'value', 1, True)
        mqtt_client.publish('{}/light/meta/units'.format(topic_path), 'lux', 1, True)
        mqtt_client.publish('{}/moisture/meta/type'.format(topic_path), 'rel_humidity', 1, True)
        mqtt_client.publish('{}/temperature/meta/type'.format(topic_path), 'temperature', 1, True)
        mqtt_client.publish('{}/timestamp/meta/type'.format(topic_path), 'text', 1, True)
    sleep(0.5) # some slack for the publish roundtrip and callback function

    for [mitempbt_name, mitempbt] in mitempbts.items():
        mqtt_client.publish('/devices/{}/meta/name'.format(mitempbt_name), mitempbt_name, 1, True)
        topic_path = '/devices/{}/controls'.format(mitempbt_name)
        mqtt_client.publish('{}/battery/meta/type'.format(topic_path), 'value', 1, True)
        mqtt_client.publish('{}/battery/meta/units'.format(topic_path), '%', 1, True)
        mqtt_client.publish('{}/humidity/meta/type'.format(topic_path), 'rel_humidity', 1, True)
        mqtt_client.publish('{}/temperature/meta/type'.format(topic_path), 'temperature', 1, True)
        mqtt_client.publish('{}/timestamp/meta/type'.format(topic_path), 'text', 1, True)
    sleep(0.5) # some slack for the publish roundtrip and callback function
    print()

print_line('Initialization complete, starting MQTT publish loop', console=False, sd_notify=True)

hciLock = threading.Lock()
threads = []

if len(mifloras) != 0:
    threads.append(sensorPooler(sensor_type_miflora, mifloras, miflora_parameters, miflora_sleep_period, hciLock))

if len(mitempbts) != 0:
    threads.append(sensorPooler(sensor_type_mitempbt, mitempbts, mitempbt_parameters, mitempbt_sleep_period, hciLock))

for thread in threads:
   thread.join()

print ("Exiting Main Thread")
if reporting_mode == 'mqtt-json':
    mqtt_client.disconnect()
