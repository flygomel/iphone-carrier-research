import plistlib
import tempfile
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import country


class CountryTests(unittest.TestCase):
    def setUp(self):
        self.data = dict(Show5GSwitch=False, CountryName='Belarus', ISOAlpha2CountryCode=['by'],
                         SupportedCountryIds=['257','com.apple.Belarus'], Unrelated={'keep':[1,2,3]})
        self.before = plistlib.dumps(self.data, fmt=plistlib.FMT_BINARY, sort_keys=False)
        self.after = country.transform(self.before,'apply')
        self.profile = dict(ProductType='iPhone99,1',ProductVersion='99.0',BuildVersion='99A1')
        self.target = country.PARENT+'/device+carrier+com.apple.Belarus+V99+100.2.plist'

    def test_single_key_change_and_exact_backup_restore(self):
        expected = dict(self.data, Show5GSwitch=True)
        self.assertEqual(plistlib.loads(self.after),expected)
        self.assertEqual(country.transform(self.after,'restore',self.before),self.before)

    def test_xml_format_preserved_and_true_is_noop(self):
        value=plistlib.dumps(self.data,fmt=plistlib.FMT_XML)
        after=country.transform(value,'apply')
        self.assertTrue(after.startswith(b'<?xml'))
        self.assertEqual(country.transform(after,'apply'),after)
        self.assertEqual(country.transform(after,'inspect'),after)

    def test_invalid_country_or_flag_never_modified(self):
        for change in [dict(Show5GSwitch=1),dict(CountryName='Other'),dict(ISOAlpha2CountryCode=['us']),dict(SupportedCountryIds=[]),dict(Show5GSwitch=None)]:
            for mode in ['inspect','apply','restore']:
                with self.assertRaises(ValueError):country.transform(plistlib.dumps(dict(self.data,**change),fmt=plistlib.FMT_BINARY) if change.get('Show5GSwitch',False) is not None else b'bad',mode)
        with self.assertRaises(ValueError):country.transform(b'bad','apply')

    def test_restore_requires_backup_and_preserves_unrelated_changes(self):
        for backup in [None,b'bad']:
            with self.assertRaises(ValueError):country.transform(self.after,'restore',backup)
        changed=plistlib.dumps(dict(self.data,Unrelated='new'))
        with self.assertRaises(ValueError):country.transform(changed,'restore',self.before)

    def test_other_model_build_operator_and_single_sim_allowed(self):
        report={**self.profile,'carriers':[dict(MCC='257',MNC='02',Slot='kTwo',CFBundleVersion='100')]}
        country.check_report(report)
        self.assertEqual(country.reference_paths(report),['com.apple.country.carrier_2.plist'])
        country.check_report({**self.profile,'carriers':[]},'restore')
        with self.assertRaises(ValueError):country.reference_paths({'carriers':[]})
        with self.assertRaises(ValueError):country.reference_paths({'carriers':[dict(MCC='257',Slot='unknown')]})

    def test_target_must_be_direct_belarus_overlay(self):
        self.assertEqual(country.validate_target(self.target),self.target)
        for target in [None,self.target+'/../other',self.target.replace('Belarus','Other'),'/System/Library/file.plist',country.PARENT+'/../'+self.target.split('/')[-1]]:
            with self.assertRaises(ValueError):country.validate_target(target)

    def test_restore_is_bound_to_device_build_and_backup_integrity(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(country.file_transport,'PRIVATE',Path(tmp)):
            run=Path(tmp)/'country-test';run.mkdir()
            state=dict(device='one',profile=self.profile,target=self.target,mode='apply',phase='complete',original_sha256=country.device.digest(self.before))
            (run/'state.json').write_text(json.dumps(state));(run/'original.plist').write_bytes(self.before)
            self.assertEqual(country.saved_original('one',self.profile),(self.target,self.before))
            for serial,profile in [('two',self.profile),('one',dict(self.profile,BuildVersion='new'))]:
                with self.assertRaises(ValueError):country.saved_original(serial,profile)
            (run/'original.plist').write_bytes(self.after)
            with self.assertRaisesRegex(ValueError,'Повреждена'):country.saved_original('one',self.profile)

class ReferenceTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_returned_without_reading_or_writing_through_link(self):
        from unittest.mock import AsyncMock, Mock
        for kind in ['S_IFLNK','S_IFREG']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                module=Mock(build_archive=Mock(return_value=b'zip'),build_books=Mock(return_value=b'books'))
                with patch.object(country.file_transport,'PRIVATE',Path(tmp)):
                    session=country.new_session(module,'device',{},'discovery',country.REFERENCES)
                session.backup_books=AsyncMock();session.native=AsyncMock();session.export_paused=AsyncMock()
                session.await_export=AsyncMock();session.return_file=AsyncMock();session.cleanup=AsyncMock()
                session.worker=Mock(returncode=None)
                afc=Mock(get_file_contents=AsyncMock(),set_file_contents=AsyncMock(),listdir=AsyncMock(return_value=['com.apple.country.carrier_1.plist']))
                metadata={'st_ifmt':kind,'LinkTarget':country.PARENT+'/device+carrier+com.apple.Belarus+V99+100.plist'}
                async def stat(afc,path):
                    return {'st_ifmt':'S_IFDIR'} if path==session.state['exported'] else metadata
                with patch.object(country.file_transport,'info',stat):
                    if kind=='S_IFLNK':
                        self.assertEqual(await session.perform(afc,'discovery'),{'com.apple.country.carrier_1.plist':metadata['LinkTarget']})
                    else:
                        with self.assertRaises(ValueError):await session.perform(afc,'discovery')
                session.return_file.assert_awaited_once();session.cleanup.assert_awaited_once()
                afc.get_file_contents.assert_not_awaited();afc.set_file_contents.assert_not_awaited()

class OverlayTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_changes_only_selected_child_and_checks_siblings(self):
        from unittest.mock import AsyncMock, Mock
        fixture=CountryTests();fixture.setUp()
        for changed_sibling in [False,True]:
            with self.subTest(changed_sibling=changed_sibling), tempfile.TemporaryDirectory() as tmp:
                module=Mock(build_archive=Mock(return_value=b'zip'),build_books=Mock(return_value=b'books'))
                with patch.object(country.file_transport,'PRIVATE',Path(tmp)):
                    s=country.new_session(module,'device',fixture.profile,'apply',fixture.target)
                for name in ['backup_books','native','export_paused','await_export','return_file','cleanup']:
                    setattr(s,name,AsyncMock())
                s.worker=Mock(returncode=None)
                s.read_file=AsyncMock(side_effect=[fixture.before,fixture.after,fixture.before])
                leaf=fixture.target.split('/')[-1]
                before=({leaf:fixture.before,'other.plist':b'untouched'},{},[''])
                after=({leaf:fixture.after,'other.plist':b'changed' if changed_sibling else b'untouched'},{},[''])
                afc=Mock(set_file_contents=AsyncMock())
                with patch.object(country.file_transport,'tree',AsyncMock(side_effect=[before,after])),patch.object(country.file_transport,'info',AsyncMock(return_value={'st_ifmt':'S_IFDIR'})):
                    if changed_sibling:
                        with self.assertRaisesRegex(ValueError,'outside'):await s.perform(afc,'apply')
                    else:
                        self.assertEqual(await s.perform(afc,'apply'),fixture.after)
                s.return_file.assert_awaited_once();s.cleanup.assert_awaited_once()
                for call in afc.set_file_contents.await_args_list:
                    self.assertEqual(call.args[0],s.state['exported']+'/'+leaf)
                module.build_archive.assert_called_once_with('/var/mobile/Library/CountryBundles',b'unused')
