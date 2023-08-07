import json
import os
from os.path import exists
import subprocess
import time
from unittest import result
import prometheus_client
import yaml

DATA = {}
METRICS = {}
LABELS = ['drive', 'type', 'model_name', 'serial_number']

def run_shell_cmd(args: list):
    
    out = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = out.communicate()

    return stdout.decode("utf-8")

def check_sata(drive: str):

    try:
        data = run_shell_cmd(['smartctl', '-d', 'sat', '-A', drive, '--json=c'])
        data_json = json.loads(data)

        if data_json['ata_smart_attributes']['table']:
            result = True
    except:
        result = False

    return result

def get_megaraid_drive():

    data = run_shell_cmd(['/opt/storcli64', '/c0/v0', 'show', 'all', 'J'])
    data_json = json.loads(data)

    result = {}

    try:
        megaraid_drive = data_json['Controllers'][0]['Response Data']['VD0 Properties']['OS Drive Name'] # megaraid drive name
        result = megaraid_drive
    except:
        result = False

    return result

def scan_drives() -> list:
    # if sata disks connected via disk shelf, smartctl will return scsi type for all disks
    # we must identity right disk types

    disks = [] # using list instead dict because disks connected via megaraid controller has same names but different disk types
    result = run_shell_cmd(['smartctl', '--scan-open', '--json=c'])
    result_json = json.loads(result)

    if 'devices' in result_json:
        devices = result_json['devices']

        megaraid_diskname = get_megaraid_drive()

        for device in devices:
            
            disk_name = device["name"]

            if megaraid_diskname and disk_name == megaraid_diskname:
                continue

            if check_sata(disk_name):
                disk_type = 'sat'
            else:
                disk_type = device["type"]

            disks.append(disk_name + '_' + disk_type)
    else:
        print("No devices found. Make sure you have enough privileges.")
    return disks

def get_devices_data(devices):

    disks = {}

    for device in devices:

        disk_name, disk_type = device.split('_')

        disk_attrs = get_device_info(disk_name, disk_type)
        disk_attrs["name"] = disk_name
        disk_attrs["type"] = disk_type

        disks[device] = disk_attrs
    
    return disks


def get_device_info(dev: str, type: str) -> dict:
    """
    Returns a dictionary of device info
    """
    results = run_shell_cmd(['smartctl', '-d', type, '-i', dev, '--json=c'])
    results = json.loads(results)

    if type.startswith('megaraid') or type == 'scsi':
        results = {
            'model_name': results.get("scsi_model_name", "Unknown"),
            'serial_number': results.get("serial_number", "Unknown")
        }
    else:
        results = {
            'model_name': results.get("model_name", "Unknown"),
            'serial_number': results.get("serial_number", "Unknown")
        }

    return results


def get_smart_status(results: dict) -> int:
    """
    Returns a 1, 0 or -1 depending on if result from
    smart status is True, False or unknown.
    """
    status = results.get("smart_status")
    return +(status.get("passed")) if status is not None else -1


def smart_sat(dev: str) -> dict:
    """
    Runs the smartctl command on a "sat" device
    and processes its attributes
    """
    results = run_shell_cmd(['smartctl', '-A', '-H', '-d', 'sat', '--json=c', dev])
    results = json.loads(results)

    attributes = {
        'smart_passed': (0, get_smart_status(results))
    }
    data = results['ata_smart_attributes']['table']
    for metric in data:
        code = metric['id']
        name = metric['name']
        value = metric['value']

        # metric['raw']['value'] contains values difficult to understand for temperatures and time up
        # that's why we added some logic to parse the string value
        value_raw = metric['raw']['string']
        try:
            # example value_raw: "33" or "43 (Min/Max 39/46)"
            value_raw = int(value_raw.split()[0])
        except:
            # example value_raw: "20071h+27m+15.375s"
            if 'h+' in value_raw:
                value_raw = int(value_raw.split('h+')[0])
            else:
                print(f"Raw value of sat metric '{name}' can't be parsed. raw_string: {value_raw} "
                      f"raw_int: {metric['raw']['value']}")
                value_raw = None

        attributes[name] = (int(code), value)
        if value_raw is not None:
            attributes[f'{name}_raw'] = (int(code), value_raw)
    return attributes


def smart_nvme(dev: str) -> dict:
    """
    Runs the smartctl command on a "nvme" device
    and processes its attributes
    """
    results = run_shell_cmd(['smartctl', '-A', '-H', '-d', 'nvme', '--json=c', dev])
    results = json.loads(results)

    attributes = {
        'smart_passed': get_smart_status(results)
    }

    # remove unnecessary data
    del results['json_format_version']
    del results['smartctl']
    del results['local_time']

    for key in results:
    
        if isinstance(results[key], dict):
            for _label, _value in results[key].items():
                if isinstance(_value, int):
                    attributes[f"{key}_{_label}"] = _value
                elif isinstance(_value, dict):
                    for _label2, _value2 in _value.items():
                        if isinstance(_value2, int):
                            attributes[f"{key}_{_label}_{_label2}"] = _value2
        elif isinstance(results[key], int):
            attributes[key] = results[key]

    # rename some attributes for sata disk compatibility
    attributes['temperature_celsius_raw'] = attributes.pop('temperature_current')
    attributes['power_cycle_count_raw'] = attributes.pop('power_cycle_count')
    attributes['power_on_hours_raw'] = attributes.pop('power_on_time_hours')

    return attributes


def smart_scsi(dev: str) -> dict:
    """
    Runs the smartctl command on a "scsi" device
    and processes its attributes
    """
    results = run_shell_cmd(['smartctl', '-a', '-d', 'scsi', '--json=c', dev])
    results = json.loads(results)

    attributes = {
        'smart_passed': get_smart_status(results)
    }
    
    # remove unnecessary data
    del results['json_format_version']
    del results['smartctl']
    del results['local_time']

    for key in results:
    
        if isinstance(results[key], dict):
            for _label, _value in results[key].items():
                if isinstance(_value, int):
                    attributes[f"{key}_{_label}"] = _value
                elif isinstance(_value, dict):
                    for _label2, _value2 in _value.items():
                        if isinstance(_value2, int):
                            attributes[f"{key}_{_label}_{_label2}"] = _value2
        elif isinstance(results[key], int):
            attributes[key] = results[key]
    
    # rename some attributes for sata disk compatibility
    attributes['temperature_celsius_raw'] = attributes.pop('temperature_current')
    attributes['power_cycle_count_raw'] = attributes.pop('scsi_start_stop_cycle_counter_accumulated_start_stop_cycles')
    attributes['power_on_hours_raw'] = attributes.pop('power_on_time_hours')
    
    return attributes


def smart_megaraid(drive_id):

    dev, type = drive_id.split('_')
    results = run_shell_cmd(['smartctl', '-a', '-d', type, '--json=c', dev])
    results = yaml.load(results, Loader=yaml.Loader)

    attributes = {
        'smart_passed': get_smart_status(results)
    }

    # remove unnecessary data
    del results['json_format_version']
    del results['smartctl']
    del results['local_time']

    for key in results:
    
        if isinstance(results[key], dict):
            for _label, _value in results[key].items():
                if isinstance(_value, int):
                    attributes[f"{key}_{_label}"] = _value
                elif isinstance(_value, dict):
                    for _label2, _value2 in _value.items():
                        if isinstance(_value2, int):
                            attributes[f"{key}_{_label}_{_label2}"] = _value2
        elif isinstance(results[key], int):
            attributes[key] = results[key]

    # rename some attributes for sata disk compatibility
    attributes['temperature_celsius_raw'] = attributes.pop('temperature_current')
    attributes['power_cycle_count_raw'] = attributes.pop('scsi_start_stop_cycle_counter_accumulated_start_stop_cycles')
    attributes['power_on_hours_raw'] = attributes.pop('power_on_time_hours')

    return attributes


def collect():
    """
    Collect all drive metrics and save them as Gauge type
    """
    global DATA, METRICS, LABELS

    for drive_id, drive_attrs in DATA.items():

        typ = drive_attrs['type']

        if typ == 'sat':
            attrs = smart_sat(drive_attrs['name'])
        elif typ == 'nvme':
            attrs = smart_nvme(drive_attrs['name'])
        elif typ == 'scsi':
            attrs = smart_scsi(drive_attrs['name'])
        elif typ.startswith('megaraid'):
            attrs = smart_megaraid(drive_id)
        else:
            continue
        
        try:
            for key, values in attrs.items():
                # Metric name in lower case
                metric = 'smartprom_' + key.replace('-', '_').replace(' ', '_').replace('.', '').replace('/', '_').lower()

                # Create metric if it does not exist
                if metric not in METRICS:
                    desc = key.replace('_', ' ')
                    code = hex(values[0]) if typ == 'sat' else hex(values)
                    print(f'{drive_id} Adding new gauge {metric} ({code})')
                    METRICS[metric] = prometheus_client.Gauge(metric, f'({code}) {desc}', LABELS)

                # Update metric
                metric_val = values[1] if typ == 'sat' else values

                METRICS[metric].labels(drive=drive_id,
                                    type=typ,
                                    model_name=drive_attrs['model_name'],
                                    serial_number=drive_attrs['serial_number']).set(metric_val)

        except Exception as e:
            print('Drive id: ', drive_id)
            print('Exception:', e)
            pass


def main():
    """
    Starts a server and exposes the metrics
    """
    global DATA

    # Validate configuration
    exporter_address = os.environ.get("SMARTCTL_EXPORTER_ADDRESS", "0.0.0.0")
    exporter_port = int(os.environ.get("SMARTCTL_EXPORTER_PORT", 9902))
    refresh_interval = int(os.environ.get("SMARTCTL_REFRESH_INTERVAL", 60))

    devices = scan_drives()
    DATA = get_devices_data(devices)

    # Start Prometheus server
    prometheus_client.start_http_server(exporter_port, exporter_address)
    print(f"Server listening in http://{exporter_address}:{exporter_port}/metrics")

    while True:
        collect()
        time.sleep(refresh_interval)


if __name__ == '__main__':

    main()
