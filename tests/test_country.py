import hashlib
import plistlib
import unittest
from unittest.mock import patch
import country


class CountryTests(unittest.TestCase):
    def setUp(self):
        self.before = plistlib.dumps({'Show5GSwitch':False, 'Unrelated':{'keep':[1,2,3]}}, fmt=plistlib.FMT_BINARY, sort_keys=False)
        self.after = plistlib.dumps({'Show5GSwitch':True, 'Unrelated':{'keep':[1,2,3]}}, fmt=plistlib.FMT_BINARY, sort_keys=False)
        self.patches = [patch.object(country,'ORIGINAL',hashlib.sha256(self.before).hexdigest()),
                        patch.object(country,'CANDIDATE',hashlib.sha256(self.after).hexdigest())]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def test_single_key_change_and_exact_backup_restore(self):
        self.assertEqual(country.transform(self.before,'apply'),self.after)
        self.assertEqual(country.transform(self.after,'restore',self.before),self.before)
        self.assertEqual(plistlib.loads(self.after)['Unrelated'],plistlib.loads(self.before)['Unrelated'])

    def test_unknown_bytes_never_modified(self):
        for mode in ['apply','restore','inspect']:
            with self.assertRaises(ValueError):country.transform(b'unknown',mode)

    def test_restore_requires_exact_original_not_reserialization(self):
        for backup in [None,b'bad']:
            with self.assertRaises(ValueError):country.transform(self.after,'restore',backup)

    def test_already_desired_is_noop(self):
        self.assertEqual(country.transform(self.after,'apply'),self.after)
        self.assertEqual(country.transform(self.before,'restore'),self.before)
        self.assertEqual(country.transform(self.after,'inspect'),self.after)

    def test_reject_wrong_build_carrier_and_duplicate_sim(self):
        report={**country.transport.EXPECTED,'SIMStatus':'kCTSIMSupportSIMStatusReady','carriers':[
            {'MCC':'257','MNC':'01','CFBundleIdentifier':'com.apple.mobilkom_by','CFBundleVersion':'72.7.1'},
            {'MCC':'257','MNC':'04','CFBundleIdentifier':'com.apple.life_by','CFBundleVersion':'72.7'}]}
        country.check_report(report)
        with self.assertRaises(ValueError):country.check_report({**report,'BuildVersion':'different'})
        with self.assertRaises(ValueError):country.check_report({**report,'carriers':report['carriers']*2})
        report['carriers'][0]['CFBundleIdentifier']='com.apple.CarrierLab'
        with self.assertRaises(ValueError):country.check_report(report)
