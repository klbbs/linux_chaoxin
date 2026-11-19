import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/install-study-backend.py'
spec=importlib.util.spec_from_file_location('study_installer',SCRIPT)
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class StudyInstallerTests(unittest.TestCase):
    def archive(self,extra=None):
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
            for name,data in [('api/base.py',b'# source'),('LICENSE',b'license')]+(extra or []):
                entry=tarfile.TarInfo('chaoxing-'+installer.COMMIT+'/'+name);entry.size=len(data)
                archive.addfile(entry,io.BytesIO(data))
        return buffer.getvalue()
    def test_archive_hash_and_source_identity(self):
        data=self.archive()
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)
            with self.assertRaises(ValueError):installer.extract_source(data,target)
            self.assertEqual(list(target.iterdir()),[])
            with patch.object(installer,'SHA256',hashlib.sha256(data).hexdigest()):installer.extract_source(data,target)
            self.assertEqual((target/'api/base.py').read_bytes(),b'# source')
            self.assertTrue((target/'LICENSE').is_file())
    def test_archive_cannot_write_outside_source(self):
        data=self.archive([('../escape',b'bad')])
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'source'
            with patch.object(installer,'SHA256',hashlib.sha256(data).hexdigest()):
                with self.assertRaises(ValueError):installer.extract_source(data,target)
            self.assertFalse((Path(temp)/'escape').exists())
    def test_plan_never_creates_home_install_directory(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            result=subprocess.run([sys.executable,str(SCRIPT)],env={**os.environ,'HOME':temp},capture_output=True,text=True,check=True)
            plan=json.loads(result.stdout)
            self.assertEqual(plan['mode'],'plan')
            self.assertEqual(plan['sha256'],installer.SHA256)
            self.assertFalse((Path(temp)/'.local').exists())
