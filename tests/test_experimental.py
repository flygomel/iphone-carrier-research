import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import experimental as exp
import experimental_context as ctx
import carrier
import transaction

PROFILE = {'ProductType':'iPhone99,1','ProductVersion':'99.0','BuildVersion':'99A1'}


class ExperimentalTests(unittest.TestCase):
    def test_context_never_matches_other_device_or_build(self):
        with patch.dict(os.environ,{ctx.KEY:json.dumps({**PROFILE,'device':'fixture'})}):
            self.assertTrue(ctx.allows('fixture',PROFILE))
            self.assertFalse(ctx.allows('other',PROFILE))
            self.assertFalse(ctx.allows('fixture',{**PROFILE,'BuildVersion':'different'}))
        with patch.dict(os.environ,{ctx.KEY:'{}'}):
            with self.assertRaises(ValueError): ctx.target()

    def test_default_has_no_experimental_authority(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertIsNone(ctx.target())
            self.assertFalse(ctx.allows('fixture',PROFILE))

    def test_only_official_https_firmware_urls(self):
        exp.apple_url('https://updates.cdn-apple.com/file.ipsw')
        for url in ('http://updates.cdn-apple.com/a','https://updates.cdn-apple.com.evil/a',
                    'https://user@updates.cdn-apple.com/a','https://example.com/a'):
            with self.subTest(url=url), self.assertRaises(ValueError): exp.apple_url(url)

    def test_manifest_requires_all_identity_fields(self):
        valid={'ProductVersion':'99.0','ProductBuildVersion':'99A1','SupportedProductTypes':['iPhone99,1'],
               'BuildIdentities':[{'Info':{'DeviceClass':'v99ap'}}]}
        exp.check_manifest(valid,PROFILE,'V99AP')
        for profile,board in (({**PROFILE,'BuildVersion':'99B2'},'V99AP'),(PROFILE,'V98AP'),
                              ({**PROFILE,'ProductType':'iPhone99,2'},'V99AP')):
            with self.assertRaises(ValueError): exp.check_manifest(valid,profile,board)

    def test_select_only_matching_board_and_require_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'signatures').mkdir()
            (root/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.apple.CarrierLab'}))
            for name in ('carrier.plist','signatures/common.plist','overrides_V99_V100.plist',
                         'overrides_V99_V100.der.pri','signatures/overrides_V99_V100.plist','overrides_V98.plist'):
                (root/name).write_bytes(b'fixture')
            lab=exp.board_bundle(root,'V99AP')
            self.assertEqual(len(lab),6)
            self.assertNotIn('CarrierLab.bundle/overrides_V98.plist',lab)
            with self.assertRaises(ValueError): exp.board_bundle(root,'V97AP')
            (root/'signatures/overrides_V99_V100.plist').unlink()
            with self.assertRaises(ValueError): exp.board_bundle(root,'V99AP')

    def test_change_selected_operator_only(self):
        original=({'A.bundle/f':b'a','B.bundle/f':b'b'},{'25701':'A.bundle','25704':'B.bundle'})
        lab={'CarrierLab.bundle/f':b'lab'}
        files,links=exp.candidate_for(original,lab,'25704')
        self.assertEqual(links,{'25701':'A.bundle','25704':'CarrierLab.bundle'})
        self.assertEqual(files,{**original[0],**lab})
        with self.assertRaises(ValueError): exp.candidate_for(original,lab,'00101')

    def test_foreign_carrierlab_is_never_overwritten(self):
        original=({'A.bundle/f':b'a','CarrierLab.bundle/f':b'old'},{'25701':'A.bundle'})
        with self.assertRaises(ValueError): exp.candidate_for(original,{'CarrierLab.bundle/f':b'new'},'25701')
