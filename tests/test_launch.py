import unittest
from launch import selected


class LaunchResultTests(unittest.TestCase):
    def report(self):
        return {'SIMStatus':'kCTSIMSupportSIMStatusReady','carriers':[
            {'MCC':'257','MNC':'01','CFBundleIdentifier':'com.apple.CarrierLab','CFBundleVersion':'72.7.1'},
            {'MCC':'257','MNC':'04','CFBundleIdentifier':'com.apple.life_by','CFBundleVersion':'72.7'}]}

    def test_success_requires_both_expected_lines_and_version(self):
        self.assertTrue(selected(self.report(),'com.apple.CarrierLab'))
        variants=[]
        d=self.report();d['SIMStatus']='not-ready';variants.append(d)
        d=self.report();d['carriers'].pop();variants.append(d)
        d=self.report();d['carriers'][0]['CFBundleVersion']='different';variants.append(d)
        d=self.report();d['carriers'][1]['CFBundleIdentifier']='unexpected';variants.append(d)
        for d in variants:
            with self.subTest(report=d): self.assertFalse(selected(d,'com.apple.CarrierLab'))
