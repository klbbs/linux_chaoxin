import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec=importlib.util.spec_from_file_location('complete_runtime',Path(__file__).parents[1]/'scripts/complete-runtime.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class CompleteRuntimeTests(unittest.TestCase):
    def test_verified_single_image_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'image.zip'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('system.img',b'image bytes')
            expected={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assertEqual(module.verify_zip(path,'system',expected),11)
            with self.assertRaises(ValueError):module.verify_zip(path,'vendor',expected)
            expected['sha256']='0'*64
            with self.assertRaises(ValueError):module.verify_zip(path,'system',expected)

    def test_extra_archive_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'image.zip'
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('system.img',b'image')
                archive.writestr('../unexpected',b'no')
            expected={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            with self.assertRaises(ValueError):module.verify_zip(path,'system',expected)
