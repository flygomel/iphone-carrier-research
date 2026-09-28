"""Explicit process-local target binding. Never a wildcard compatibility bypass."""
import json
import os
import re

KEY = 'CARRIER_EXPERIMENTAL_TARGET'
FIELDS = ('ProductType', 'ProductVersion', 'BuildVersion')


def target():
    raw = os.environ.get(KEY)
    if raw is None: return None
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {*FIELDS, 'device'}
            or not all(isinstance(v, str) and v and len(v) <= 128 for v in value.values())
            or not re.fullmatch(r'iPhone\d+,\d+', value['ProductType'])):
        raise ValueError('Invalid experimental target binding')
    return value


def allows(serial, profile):
    value = target()
    return value is not None and value['device'] == serial and all(value[k] == profile.get(k) for k in FIELDS)
